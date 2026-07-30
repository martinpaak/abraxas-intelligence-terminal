from __future__ import annotations

import json
import hashlib
from threading import Lock
from datetime import datetime, timedelta, timezone

from backend.app.storage.risk import get_risk_profile, resolve_risk_policy, validate_order_intent
from backend.app.market.freshness import MAX_PRICE_DRIFT_PCT, PRICE_MAX_AGE_SECONDS, PROPOSAL_TTL_SECONDS
from backend.app.execution.contracts import OrderIntent
from backend.app.storage.execution import get_execution_intent, save_execution_intent, update_execution_intent
from backend.app.storage.sqlite import connect, initialize_database

ACCOUNT_ID = 1
DEFAULT_BALANCE = 10_000.0
FEE_RATE = 0.001
PAPER_EXECUTION_LOCK = Lock()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def seed_account(connection) -> None:
    now = utc_now_iso()
    connection.execute(
        """INSERT OR IGNORE INTO simulated_accounts
        (id, initial_balance, cash_balance, realized_pnl, peak_equity, created_at, updated_at)
        VALUES (?, ?, ?, 0, ?, ?, ?)""",
        (ACCOUNT_ID, DEFAULT_BALANCE, DEFAULT_BALANCE, DEFAULT_BALANCE, now, now),
    )


def latest_price(connection, symbol: str) -> float:
    return latest_price_record(connection, symbol)["price"]


def latest_price_record(connection, symbol: str) -> dict:
    row = connection.execute(
        "SELECT price, timestamp FROM market_snapshots WHERE symbol = ? ORDER BY timestamp DESC, id DESC LIMIT 1",
        (symbol,),
    ).fetchone()
    if not row:
        raise ValueError(f"No persisted market snapshot is available for {symbol}")
    return {"price": float(row["price"]), "timestamp": row["timestamp"]}


def bot_daily_realized_pnl(connection, bot_id: int) -> float:
    row = connection.execute(
        """SELECT COALESCE(SUM(ledger.realized_pnl_delta), 0) AS value
        FROM simulated_ledger AS ledger
        JOIN simulated_fills AS fill
          ON ledger.event_type = 'market_fill' AND ledger.reference_id = fill.id
        JOIN simulated_orders AS paper_order ON paper_order.id = fill.order_id
        WHERE ledger.account_id = ? AND paper_order.bot_id = ?
          AND date(ledger.created_at) = date('now')""",
        (ACCOUNT_ID, bot_id),
    ).fetchone()
    return float(row["value"])


def bot_last_loss_at(connection, bot_id: int) -> str | None:
    row = connection.execute(
        """SELECT ledger.created_at
        FROM simulated_ledger AS ledger
        JOIN simulated_fills AS fill
          ON ledger.event_type = 'market_fill' AND ledger.reference_id = fill.id
        JOIN simulated_orders AS paper_order ON paper_order.id = fill.order_id
        WHERE ledger.account_id = ? AND paper_order.bot_id = ?
          AND ledger.realized_pnl_delta < 0
        ORDER BY ledger.created_at DESC, ledger.id DESC LIMIT 1""",
        (ACCOUNT_ID, bot_id),
    ).fetchone()
    return row["created_at"] if row else None


def bot_open_exposure_notional(connection, bot_id: int) -> float:
    rows = connection.execute(
        """SELECT symbol, SUM(quantity) AS quantity
        FROM simulated_position_allocations
        WHERE account_id = ? AND bot_id = ? AND quantity > 0
        GROUP BY symbol""",
        (ACCOUNT_ID, bot_id),
    ).fetchall()
    return sum(float(row["quantity"]) * latest_price(connection, row["symbol"]) for row in rows)


def bot_runtime_state(connection, bot_id: int) -> dict:
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    connection.execute(
        """INSERT OR IGNORE INTO paper_bot_runtime_state
        (bot_id, status, reason, paused_until, created_at, updated_at)
        VALUES (?, 'active', 'Default paper runtime state', NULL, ?, ?)""",
        (bot_id, now_iso, now_iso),
    )
    row = dict(connection.execute(
        "SELECT * FROM paper_bot_runtime_state WHERE bot_id = ?",
        (bot_id,),
    ).fetchone())
    if row["status"] == "paused" and row["paused_until"]:
        paused_until = datetime.fromisoformat(str(row["paused_until"]).replace("Z", "+00:00"))
        if paused_until.tzinfo is None:
            paused_until = paused_until.replace(tzinfo=timezone.utc)
        if paused_until <= now:
            reason = "Automatic resume after pause window expired"
            connection.execute(
                """UPDATE paper_bot_runtime_state
                SET status = 'active', reason = ?, paused_until = NULL, updated_at = ?
                WHERE bot_id = ?""",
                (reason, now_iso, bot_id),
            )
            connection.execute(
                """INSERT INTO paper_bot_runtime_events
                (bot_id, event_type, previous_status, new_status, reason, paused_until, created_at)
                VALUES (?, 'auto_resumed', 'paused', 'active', ?, NULL, ?)""",
                (bot_id, reason, now_iso),
            )
            row = dict(connection.execute(
                "SELECT * FROM paper_bot_runtime_state WHERE bot_id = ?",
                (bot_id,),
            ).fetchone())
    row["entry_blocked"] = row["status"] == "paused"
    return row


def set_bot_runtime_state(bot_id: int, status: str, reason: str, pause_minutes: int | None = None) -> dict:
    initialize_database()
    bot_id = int(bot_id)
    status = str(status).strip().lower()
    clean_reason = str(reason).strip()
    if status not in {"active", "paused"}:
        raise ValueError("Paper bot runtime status must be active or paused")
    if len(clean_reason) < 3:
        raise ValueError("An auditable reason is required")
    if pause_minutes is not None and not 1 <= int(pause_minutes) <= 43_200:
        raise ValueError("Pause minutes must be between 1 and 43,200")
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    paused_until = (now + timedelta(minutes=int(pause_minutes))).isoformat() if status == "paused" and pause_minutes else None
    with connect() as connection:
        if not connection.execute("SELECT 1 FROM bots WHERE id = ?", (bot_id,)).fetchone():
            raise ValueError(f"Bot {bot_id} does not exist")
        previous = bot_runtime_state(connection, bot_id)
        connection.execute(
            """UPDATE paper_bot_runtime_state
            SET status = ?, reason = ?, paused_until = ?, updated_at = ?
            WHERE bot_id = ?""",
            (status, clean_reason, paused_until, now_iso, bot_id),
        )
        connection.execute(
            """INSERT INTO paper_bot_runtime_events
            (bot_id, event_type, previous_status, new_status, reason, paused_until, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (bot_id, "paused" if status == "paused" else "resumed", previous["status"], status, clean_reason, paused_until, now_iso),
        )
        return bot_runtime_state(connection, bot_id)


def circuit_breaker_config(connection, bot_id: int) -> dict:
    now = utc_now_iso()
    connection.execute(
        """INSERT OR IGNORE INTO paper_bot_circuit_breakers
        (bot_id, enabled, max_consecutive_losses, max_rejections, rejection_window_minutes,
         max_drawdown_pct, pause_minutes, created_at, updated_at)
        VALUES (?, 1, 3, 5, 15, 10, 60, ?, ?)""",
        (bot_id, now, now),
    )
    row = dict(connection.execute(
        "SELECT * FROM paper_bot_circuit_breakers WHERE bot_id = ?",
        (bot_id,),
    ).fetchone())
    row["enabled"] = bool(row["enabled"])
    return row


def set_bot_circuit_breaker(bot_id: int, payload: dict) -> dict:
    initialize_database()
    bot_id = int(bot_id)
    values = {
        "enabled": 1 if bool(payload.get("enabled", True)) else 0,
        "max_consecutive_losses": int(payload.get("max_consecutive_losses", 3)),
        "max_rejections": int(payload.get("max_rejections", 5)),
        "rejection_window_minutes": int(payload.get("rejection_window_minutes", 15)),
        "max_drawdown_pct": float(payload.get("max_drawdown_pct", 10)),
        "pause_minutes": int(payload.get("pause_minutes", 60)),
    }
    if not 1 <= values["max_consecutive_losses"] <= 100:
        raise ValueError("Consecutive loss threshold must be between 1 and 100")
    if not 1 <= values["max_rejections"] <= 1000:
        raise ValueError("Rejection threshold must be between 1 and 1,000")
    if not 1 <= values["rejection_window_minutes"] <= 1440:
        raise ValueError("Rejection window must be between 1 and 1,440 minutes")
    if not 0 < values["max_drawdown_pct"] <= 100:
        raise ValueError("Drawdown threshold must be greater than 0 and at most 100 percent")
    if not 1 <= values["pause_minutes"] <= 43_200:
        raise ValueError("Circuit pause must be between 1 and 43,200 minutes")
    now = utc_now_iso()
    with connect() as connection:
        if not connection.execute("SELECT 1 FROM bots WHERE id = ?", (bot_id,)).fetchone():
            raise ValueError(f"Bot {bot_id} does not exist")
        circuit_breaker_config(connection, bot_id)
        connection.execute(
            """UPDATE paper_bot_circuit_breakers
            SET enabled = ?, max_consecutive_losses = ?, max_rejections = ?,
                rejection_window_minutes = ?, max_drawdown_pct = ?, pause_minutes = ?, updated_at = ?
            WHERE bot_id = ?""",
            (*values.values(), now, bot_id),
        )
        return circuit_breaker_config(connection, bot_id)


def consecutive_bot_losses(connection, bot_id: int) -> int:
    rows = connection.execute(
        """SELECT ledger.realized_pnl_delta
        FROM simulated_ledger AS ledger
        JOIN simulated_fills AS fill
          ON ledger.event_type = 'market_fill' AND ledger.reference_id = fill.id
        JOIN simulated_orders AS paper_order ON paper_order.id = fill.order_id
        WHERE paper_order.bot_id = ? AND ledger.realized_pnl_delta != 0
        ORDER BY ledger.created_at DESC, ledger.id DESC LIMIT 100""",
        (bot_id,),
    ).fetchall()
    count = 0
    for row in rows:
        if float(row["realized_pnl_delta"]) >= 0:
            break
        count += 1
    return count


def persist_bot_equity_snapshot(connection, bot_id: int, pnl: float, open_value: float, capital_basis: float) -> dict:
    equity = max(0.0, capital_basis + pnl)
    previous_peak = connection.execute(
        """SELECT MAX(peak_equity) AS value FROM paper_bot_equity_snapshots
        WHERE bot_id = ? AND ABS(capital_basis - ?) < 0.000001""",
        (bot_id, capital_basis),
    ).fetchone()["value"]
    peak = max(float(previous_peak or capital_basis), equity)
    drawdown_pct = max(0.0, ((peak - equity) / peak) * 100) if peak else 0.0
    evidence = {
        "equity": round(equity, 8),
        "peak_equity": round(peak, 8),
        "pnl": round(pnl, 8),
        "open_value": round(open_value, 8),
        "capital_basis": round(capital_basis, 8),
    }
    fingerprint = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    connection.execute(
        """INSERT OR IGNORE INTO paper_bot_equity_snapshots
        (bot_id, equity, peak_equity, drawdown_pct, pnl, open_value, capital_basis,
         source_fingerprint, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (bot_id, equity, peak, drawdown_pct, pnl, open_value, capital_basis, fingerprint, utc_now_iso()),
    )
    return {**evidence, "drawdown_pct": round(drawdown_pct, 8), "source_fingerprint": fingerprint}


def evaluate_bot_circuit_breaker(connection, bot_id: int, equity_state: dict | None = None) -> dict:
    config = circuit_breaker_config(connection, bot_id)
    runtime = bot_runtime_state(connection, bot_id)
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=config["rejection_window_minutes"])).isoformat()
    rejection_row = connection.execute(
        """SELECT COUNT(*) AS value, MAX(id) AS latest_id FROM simulated_orders
        WHERE bot_id = ? AND status = 'rejected' AND created_at >= ?
          AND LOWER(COALESCE(rejection_reason, '')) NOT LIKE '%runtime paused%'""",
        (bot_id, cutoff),
    ).fetchone()
    rejection_count = int(rejection_row["value"])
    latest_loss_row = connection.execute(
        """SELECT ledger.id FROM simulated_ledger AS ledger
        JOIN simulated_fills AS fill
          ON ledger.event_type = 'market_fill' AND ledger.reference_id = fill.id
        JOIN simulated_orders AS paper_order ON paper_order.id = fill.order_id
        WHERE paper_order.bot_id = ? AND ledger.realized_pnl_delta < 0
        ORDER BY ledger.created_at DESC, ledger.id DESC LIMIT 1""",
        (bot_id,),
    ).fetchone()
    loss_count = consecutive_bot_losses(connection, bot_id)
    trigger = None
    observed = 0
    threshold = 0
    evidence_id = None
    drawdown_pct = float((equity_state or {}).get("drawdown_pct") or 0)
    if config["enabled"] and drawdown_pct >= config["max_drawdown_pct"]:
        trigger, observed, threshold = "max_drawdown", drawdown_pct, config["max_drawdown_pct"]
        evidence_id = (equity_state or {}).get("source_fingerprint")
    elif config["enabled"] and loss_count >= config["max_consecutive_losses"]:
        trigger, observed, threshold, evidence_id = "consecutive_losses", loss_count, config["max_consecutive_losses"], latest_loss_row["id"]
    elif config["enabled"] and rejection_count >= config["max_rejections"]:
        trigger, observed, threshold, evidence_id = "rejection_burst", rejection_count, config["max_rejections"], rejection_row["latest_id"]
    last_event = connection.execute(
        """SELECT evidence_json FROM paper_bot_circuit_events
        WHERE bot_id = ? AND trigger_code = ? ORDER BY id DESC LIMIT 1""",
        (bot_id, trigger),
    ).fetchone() if trigger else None
    previous_evidence_id = None
    if last_event:
        try:
            previous_evidence_id = json.loads(last_event["evidence_json"]).get("evidence_id")
        except (TypeError, json.JSONDecodeError):
            previous_evidence_id = None
    is_new_evidence = evidence_id is not None and evidence_id != previous_evidence_id
    if trigger and is_new_evidence and runtime["status"] == "active":
        now = datetime.now(timezone.utc)
        paused_until = (now + timedelta(minutes=config["pause_minutes"])).isoformat()
        reason = f"Automatic circuit breaker: {trigger} ({observed}/{threshold})"
        connection.execute(
            """UPDATE paper_bot_runtime_state
            SET status = 'paused', reason = ?, paused_until = ?, updated_at = ? WHERE bot_id = ?""",
            (reason, paused_until, now.isoformat(), bot_id),
        )
        connection.execute(
            """INSERT INTO paper_bot_runtime_events
            (bot_id, event_type, previous_status, new_status, reason, paused_until, created_at)
            VALUES (?, 'paused', 'active', 'paused', ?, ?, ?)""",
            (bot_id, reason, paused_until, now.isoformat()),
        )
        connection.execute(
            """INSERT INTO paper_bot_circuit_events
            (bot_id, trigger_code, observed_value, threshold_value, evidence_json, action, created_at)
            VALUES (?, ?, ?, ?, ?, 'paused', ?)""",
            (bot_id, trigger, observed, threshold, json.dumps({
                "consecutive_losses": loss_count,
                "rejections_in_window": rejection_count,
                "rejection_window_minutes": config["rejection_window_minutes"],
                "pause_minutes": config["pause_minutes"],
                "evidence_id": evidence_id,
                "drawdown_pct": drawdown_pct,
            }, sort_keys=True), now.isoformat()),
        )
        runtime = bot_runtime_state(connection, bot_id)
    return {
        "config": config,
        "status": "tripped" if runtime["status"] == "paused" and str(runtime["reason"]).startswith("Automatic circuit breaker") else "armed" if config["enabled"] else "disabled",
        "consecutive_losses": loss_count,
        "rejections_in_window": rejection_count,
        "drawdown_pct": drawdown_pct,
        "runtime": runtime,
    }


def bot_performance(connection, session_started_at: str, account_equity: float) -> list[dict]:
    bots = [dict(row) for row in connection.execute(
        "SELECT id, name, base_symbol, timeframe, risk_profile, status FROM bots ORDER BY id DESC"
    ).fetchall()]
    results = []
    for bot in bots:
        orders = [dict(row) for row in connection.execute(
            "SELECT * FROM simulated_orders WHERE bot_id = ? AND created_at >= ? ORDER BY id",
            (bot["id"], session_started_at),
        ).fetchall()]
        fills = [dict(row) for row in connection.execute(
            """SELECT f.* FROM simulated_fills f JOIN simulated_orders o ON o.id = f.order_id
            WHERE o.bot_id = ? AND f.filled_at >= ? ORDER BY f.id""",
            (bot["id"], session_started_at),
        ).fetchall()]
        quantities: dict[str, float] = {}
        cash_flow = 0.0
        deployed = 0.0
        fees = 0.0
        for fill in fills:
            symbol = fill["symbol"]
            quantity = float(fill["quantity"])
            notional = quantity * float(fill["price"])
            fee = float(fill["fee"])
            fees += fee
            if fill["side"] == "buy":
                quantities[symbol] = quantities.get(symbol, 0.0) + quantity
                cash_flow -= notional + fee
                deployed += notional + fee
            else:
                quantities[symbol] = quantities.get(symbol, 0.0) - quantity
                cash_flow += notional - fee
        open_value = 0.0
        open_positions = []
        for symbol, quantity in quantities.items():
            if quantity <= 1e-12:
                continue
            price = latest_price(connection, symbol)
            value = quantity * price
            open_value += value
            open_positions.append({"symbol": symbol, "quantity": quantity, "market_price": price, "market_value": value})
        pnl = cash_flow + open_value
        roi_pct = (pnl / deployed) * 100 if deployed else 0.0
        daily_realized_pnl = bot_daily_realized_pnl(connection, bot["id"])
        policy_resolution = resolve_risk_policy(ACCOUNT_ID, bot["id"], connection=connection)
        daily_loss_limit_pct = float(policy_resolution["effective_limits"]["max_daily_loss_pct"])
        daily_loss_budget = account_equity * daily_loss_limit_pct / 100
        daily_loss_used = max(0.0, -daily_realized_pnl)
        daily_loss_usage_pct = (daily_loss_used / daily_loss_budget) * 100 if daily_loss_budget else 100.0
        capital_limit_pct = float(policy_resolution["effective_limits"]["max_position_pct"])
        capital_budget = account_equity * capital_limit_pct / 100
        capital_used = open_value
        capital_usage_pct = (capital_used / capital_budget) * 100 if capital_budget else 100.0
        highest_usage_pct = max(daily_loss_usage_pct, capital_usage_pct)
        equity_state = persist_bot_equity_snapshot(connection, bot["id"], pnl, open_value, capital_budget)
        circuit_breaker = evaluate_bot_circuit_breaker(connection, bot["id"], equity_state)
        runtime = circuit_breaker["runtime"]
        risk_status = "blocked" if runtime["entry_blocked"] or highest_usage_pct >= 100 else "warning" if highest_usage_pct >= 80 else "clear"
        results.append({
            **bot,
            "paper_status": "activity" if orders else "no_activity",
            "roi_pct": roi_pct,
            "pnl": pnl,
            "deployed_capital": deployed,
            "open_value": open_value,
            "fees": fees,
            "filled_orders": sum(order["status"] == "filled" for order in orders),
            "rejected_orders": sum(order["status"] == "rejected" for order in orders),
            "started_at": orders[0]["created_at"] if orders else None,
            "last_activity_at": orders[-1]["created_at"] if orders else None,
            "open_positions": open_positions,
            "runtime": runtime,
            "circuit_breaker": circuit_breaker,
            "equity_state": equity_state,
            "risk_budget": {
                "status": risk_status,
                "daily_realized_pnl": daily_realized_pnl,
                "daily_loss_limit_pct": daily_loss_limit_pct,
                "daily_loss_budget": daily_loss_budget,
                "daily_loss_used": daily_loss_used,
                "daily_loss_remaining": max(0.0, daily_loss_budget - daily_loss_used),
                "daily_loss_usage_pct": daily_loss_usage_pct,
                "capital_limit_pct": capital_limit_pct,
                "capital_budget": capital_budget,
                "capital_used": capital_used,
                "capital_remaining": max(0.0, capital_budget - capital_used),
                "capital_usage_pct": capital_usage_pct,
                "policy_scope": policy_resolution["layers"][-1]["scope_type"],
                "policy_fingerprint": policy_resolution["fingerprint"],
            },
        })
    return results


def attributed_position_quantity(connection, symbol: str, bot_id: int | None, session_started_at: str) -> float:
    if bot_id is None:
        bot_clause = "o.bot_id IS NULL"
        parameters = (symbol, session_started_at)
    else:
        bot_clause = "o.bot_id = ?"
        parameters = (symbol, session_started_at, bot_id)
    rows = connection.execute(
        f"""SELECT f.side, f.quantity FROM simulated_fills f
        JOIN simulated_orders o ON o.id = f.order_id
        WHERE f.symbol = ? AND f.filled_at >= ? AND {bot_clause}""",
        parameters,
    ).fetchall()
    return sum(float(row["quantity"]) if row["side"] == "buy" else -float(row["quantity"]) for row in rows)


def allocation_owner_key(bot_id: int | None, bot_version_id: int | None, strategy_hash: str | None) -> str:
    if bot_id is None:
        return "manual"
    return f"bot:{bot_id}:version:{bot_version_id or 'legacy'}:hash:{strategy_hash or 'legacy'}"


def position_allocation(connection, symbol: str, bot_id: int | None, bot_version_id: int | None, strategy_hash: str | None) -> dict | None:
    owner_key = allocation_owner_key(bot_id, bot_version_id, strategy_hash)
    row = connection.execute(
        "SELECT * FROM simulated_position_allocations WHERE account_id = ? AND symbol = ? AND owner_key = ?",
        (ACCOUNT_ID, symbol, owner_key),
    ).fetchone()
    return dict(row) if row else None


def get_position_allocation(symbol: str, bot_id: int, bot_version_id: int, strategy_hash: str) -> dict | None:
    initialize_database()
    with connect() as connection:
        return position_allocation(connection, symbol.upper(), bot_id, bot_version_id, strategy_hash)


def update_position_protection(allocation_id: int, stop_loss_price: float | None, take_profit_price: float | None, trailing_distance_pct: float | None) -> dict:
    initialize_database()
    now = utc_now_iso()
    with connect() as connection:
        allocation = connection.execute("SELECT * FROM simulated_position_allocations WHERE id = ? AND quantity > 0", (allocation_id,)).fetchone()
        if not allocation:
            raise ValueError("Open paper allocation not found")
        average = float(allocation["average_price"])
        if stop_loss_price is not None and not 0 < stop_loss_price < average:
            raise ValueError("Stop loss must be below the allocation average price")
        if take_profit_price is not None and take_profit_price <= average:
            raise ValueError("Take profit must be above the allocation average price")
        if trailing_distance_pct is not None and not 0 < trailing_distance_pct <= 50:
            raise ValueError("Trailing distance must be between 0 and 50 percent")
        connection.execute("""INSERT INTO paper_position_protections (allocation_id, stop_loss_price, take_profit_price, trailing_distance_pct, highest_price, updated_at)
            VALUES (?, ?, ?, ?, NULL, ?) ON CONFLICT(allocation_id) DO UPDATE SET stop_loss_price=excluded.stop_loss_price,
            take_profit_price=excluded.take_profit_price, trailing_distance_pct=excluded.trailing_distance_pct, updated_at=excluded.updated_at""",
            (allocation_id, stop_loss_price, take_profit_price, trailing_distance_pct, now))
        row = connection.execute("SELECT * FROM paper_position_protections WHERE allocation_id = ?", (allocation_id,)).fetchone()
        return dict(row)


def account_snapshot() -> dict:
    initialize_database()
    now = utc_now_iso()
    with connect() as connection:
        seed_account(connection)
        account = dict(connection.execute("SELECT * FROM simulated_accounts WHERE id = ?", (ACCOUNT_ID,)).fetchone())
        daily_realized = float(connection.execute(
            "SELECT COALESCE(SUM(realized_pnl_delta), 0) AS value FROM simulated_ledger WHERE account_id = ? AND date(created_at) = date('now')",
            (ACCOUNT_ID,),
        ).fetchone()["value"])
        positions = [dict(row) for row in connection.execute(
            "SELECT * FROM simulated_positions WHERE account_id = ? AND quantity > 0 ORDER BY symbol",
            (ACCOUNT_ID,),
        ).fetchall()]
        market_value = 0.0
        unrealized_pnl = 0.0
        for position in positions:
            price = latest_price(connection, position["symbol"])
            position["market_price"] = price
            position["market_value"] = position["quantity"] * price
            position["unrealized_pnl"] = position["quantity"] * (price - position["average_price"])
            market_value += position["market_value"]
            unrealized_pnl += position["unrealized_pnl"]
        equity = float(account["cash_balance"]) + market_value
        peak = max(float(account["peak_equity"]), equity)
        if peak != float(account["peak_equity"]):
            connection.execute("UPDATE simulated_accounts SET peak_equity = ?, updated_at = ? WHERE id = ?", (peak, utc_now_iso(), ACCOUNT_ID))
        drawdown = ((equity / peak) - 1) * 100 if peak else 0.0
        orders = [dict(row) for row in connection.execute(
            "SELECT * FROM simulated_orders WHERE created_at >= ? ORDER BY id DESC LIMIT 30", (account["created_at"],)
        ).fetchall()]
        fills = [dict(row) for row in connection.execute(
            "SELECT * FROM simulated_fills WHERE filled_at >= ? ORDER BY id DESC LIMIT 30", (account["created_at"],)
        ).fetchall()]
        allocations = [dict(row) for row in connection.execute(
            "SELECT * FROM simulated_position_allocations WHERE account_id = ? ORDER BY updated_at DESC", (ACCOUNT_ID,)
        ).fetchall()]
        for allocation in allocations:
            allocation["stop_loss_price"] = None
            allocation["take_profit_price"] = None
            protection = connection.execute("SELECT stop_loss_price, take_profit_price, trailing_distance_pct, highest_price, updated_at FROM paper_position_protections WHERE allocation_id = ?", (allocation["id"],)).fetchone()
            if protection:
                allocation.update(dict(protection))
                mark_row = connection.execute("SELECT price FROM market_snapshots WHERE symbol = ? ORDER BY timestamp DESC, id DESC LIMIT 1", (allocation["symbol"],)).fetchone()
                mark = float(mark_row["price"]) if mark_row else None
                if mark and allocation.get("trailing_distance_pct"):
                    high = max(float(protection["highest_price"] or 0), mark)
                    trailing_stop = high * (1 - float(protection["trailing_distance_pct"]) / 100)
                    if high > float(protection["highest_price"] or 0) or not allocation.get("stop_loss_price") or trailing_stop > float(allocation["stop_loss_price"]):
                        connection.execute("UPDATE paper_position_protections SET highest_price = ?, stop_loss_price = MAX(COALESCE(stop_loss_price, 0), ?), updated_at = ? WHERE allocation_id = ?", (high, trailing_stop, now, allocation["id"]))
                        allocation["highest_price"] = high
                        allocation["stop_loss_price"] = max(float(allocation.get("stop_loss_price") or 0), trailing_stop)
            if allocation.get("bot_version_id") and allocation.get("average_price"):
                version = connection.execute("SELECT strategy_json FROM bot_versions WHERE id = ?", (allocation["bot_version_id"],)).fetchone()
                if version:
                    try:
                        risk = json.loads(version["strategy_json"]).get("risk", {})
                        average = float(allocation["average_price"])
                        if allocation.get("stop_loss_price") is None:
                            allocation["stop_loss_price"] = round(average * (1 - abs(float(risk.get("stop_loss_pct", 0))) / 100), 8)
                        if allocation.get("take_profit_price") is None:
                            allocation["take_profit_price"] = round(average * (1 + abs(float(risk.get("take_profit_pct", 0))) / 100), 8)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        pass
        proposals = [dict(row) for row in connection.execute(
            "SELECT * FROM paper_order_proposals ORDER BY id DESC LIMIT 30"
        ).fetchall()]
        ledger = [dict(row) for row in connection.execute(
            "SELECT * FROM simulated_ledger WHERE account_id = ? AND created_at >= ? ORDER BY id DESC LIMIT 50",
            (ACCOUNT_ID, account["created_at"]),
        ).fetchall()]
        execution_intents = [dict(row) for row in connection.execute(
            """SELECT id, environment, adapter, symbol, action, order_type, quantity,
            bot_id, bot_version_id, strategy_hash, signal_evaluation_id, proposal_id,
            status, risk_validation_id, result_reference, created_at, updated_at
            FROM execution_intents
            WHERE environment = 'paper' AND created_at >= ?
            ORDER BY created_at DESC LIMIT 30""",
            (account["created_at"],),
        ).fetchall()]
        performance = bot_performance(connection, account["created_at"], equity)
        runtime_events = [dict(row) for row in connection.execute(
            """SELECT * FROM paper_bot_runtime_events
            ORDER BY id DESC LIMIT 50"""
        ).fetchall()]
        circuit_events = [dict(row) for row in connection.execute(
            """SELECT * FROM paper_bot_circuit_events
            WHERE created_at >= ? ORDER BY id DESC LIMIT 100""",
            (account["created_at"],),
        ).fetchall()]
    risk_profile = get_risk_profile(audit_limit=1)
    protections = {
        "risk_required": True,
        "kill_switch_active": risk_profile["kill_switch"]["active"],
        "account_policy": next((policy for policy in risk_profile["policies"] if policy["scope_type"] == "account" and policy["scope_id"] == ACCOUNT_ID and policy["status"] == "active"), None),
        "bot_policy_count": sum(policy["scope_type"] == "bot" and policy["status"] == "active" for policy in risk_profile["policies"]),
        "policy_resolution_rule": risk_profile["policy_resolution_rule"],
        "price_max_age_seconds": PRICE_MAX_AGE_SECONDS,
        "proposal_ttl_seconds": PROPOSAL_TTL_SECONDS,
        "max_price_drift_pct": MAX_PRICE_DRIFT_PCT,
        "execution_serialization": "single_process_lock",
        "live_execution": "blocked",
    }
    return {"account": account, "equity": equity, "market_value": market_value, "unrealized_pnl": unrealized_pnl, "daily_realized_pnl": daily_realized, "drawdown_pct": abs(drawdown), "positions": positions, "allocations": allocations, "proposals": proposals, "orders": orders, "fills": fills, "ledger": ledger, "execution_intents": execution_intents, "bot_performance": performance, "bot_runtime_events": runtime_events, "bot_circuit_events": circuit_events, "protections": protections, "mode": "paper", "live_execution": "blocked"}


def reconcile_paper_runtime(stale_after_seconds: int = 60) -> dict:
    initialize_database()
    now = datetime.now(timezone.utc)
    stale_before = (now.timestamp() - stale_after_seconds)
    summary = {"orders_reconciled": 0, "leases_released": 0, "proposals_repaired": 0, "execution_performed": False}
    with connect() as connection:
        proposals = [dict(row) for row in connection.execute(
            "SELECT * FROM paper_order_proposals WHERE status IN ('pending', 'submitted') ORDER BY id"
        ).fetchall()]
        for proposal in proposals:
            intent = connection.execute("SELECT * FROM execution_intents WHERE proposal_id = ?", (proposal["id"],)).fetchone()
            order = connection.execute("SELECT * FROM simulated_orders WHERE proposal_id = ?", (proposal["id"],)).fetchone()
            if order:
                order = dict(order)
                fill = connection.execute("SELECT id FROM simulated_fills WHERE order_id = ?", (order["id"],)).fetchone()
                intent_status = "filled" if fill else "rejected"
                reference = f"simulated_fill:{fill['id']}" if fill else f"simulated_order:{order['id']}"
                if intent:
                    update_execution_intent(intent["id"], intent_status, reference, order["risk_validation_id"], connection=connection)
                connection.execute(
                    """UPDATE paper_order_proposals SET status = 'submitted', execution_intent_id = ?,
                       risk_validation_id = ?, result_reference = ?, submitted_at = COALESCE(submitted_at, ?),
                       claim_token = NULL, claimed_at = NULL, last_error = NULL, updated_at = ? WHERE id = ?""",
                    (intent["id"] if intent else None, order["risk_validation_id"], reference, now.isoformat(), now.isoformat(), proposal["id"]),
                )
                summary["orders_reconciled"] += 1
                if proposal["status"] != "submitted" or not proposal.get("result_reference"):
                    summary["proposals_repaired"] += 1
                continue
            if proposal["status"] == "pending" and proposal.get("claimed_at"):
                claimed = datetime.fromisoformat(str(proposal["claimed_at"]).replace("Z", "+00:00"))
                if claimed.tzinfo is None:
                    claimed = claimed.replace(tzinfo=timezone.utc)
                if claimed.timestamp() < stale_before:
                    connection.execute(
                        "UPDATE paper_order_proposals SET claim_token = NULL, claimed_at = NULL, last_error = ?, updated_at = ? WHERE id = ?",
                        ("Stale lease released by reconciler", now.isoformat(), proposal["id"]),
                    )
                    summary["leases_released"] += 1
    return summary


def place_market_order(payload: dict) -> dict:
    initialize_database()
    with connect() as connection:
        seed_account(connection)
    intent = OrderIntent.paper_market(payload)
    existing = get_execution_intent(intent.id)
    if existing and existing["status"] != "created":
        return recovered_execution_result(existing)
    save_execution_intent(intent)
    try:
        return _execute_market_intent(intent)
    except Exception:
        update_execution_intent(intent.id, "failed")
        raise


def recovered_execution_result(intent: dict) -> dict:
    reference = str(intent.get("result_reference") or "")
    order_id = int(reference.split(":", 1)[1]) if reference.startswith("simulated_order:") else None
    fill_id = int(reference.split(":", 1)[1]) if reference.startswith("simulated_fill:") else None
    with connect() as connection:
        if fill_id and not order_id:
            row = connection.execute("SELECT order_id FROM simulated_fills WHERE id = ?", (fill_id,)).fetchone()
            order_id = int(row["order_id"]) if row else None
        order = connection.execute(
            "SELECT status, rejection_reason FROM simulated_orders WHERE id = ?", (order_id,)
        ).fetchone() if order_id else None
    return {
        "intent_id": intent["id"], "order_id": order_id, "fill_id": fill_id,
        "status": order["status"] if order else intent["status"],
        "reason": order["rejection_reason"] if order else None,
        "risk": {"validation_id": intent.get("risk_validation_id")},
        "execution_performed": bool(fill_id), "recovered": True,
    }


def _execute_market_intent(intent: OrderIntent) -> dict:
    with PAPER_EXECUTION_LOCK:
        return _execute_market_intent_serialized(intent)


def _execute_market_intent_serialized(intent: OrderIntent) -> dict:
    initialize_database()
    if intent.proposal_id:
        with connect() as connection:
            existing_order = connection.execute(
                "SELECT * FROM simulated_orders WHERE proposal_id = ?", (intent.proposal_id,)
            ).fetchone()
            if existing_order:
                order = dict(existing_order)
                fill = connection.execute("SELECT id FROM simulated_fills WHERE order_id = ?", (order["id"],)).fetchone()
                terminal_status = "filled" if order["status"] == "filled" else "rejected"
                reference = f"simulated_fill:{fill['id']}" if fill else f"simulated_order:{order['id']}"
                update_execution_intent(intent.id, terminal_status, reference, order["risk_validation_id"], connection=connection)
                return {
                    "intent_id": intent.id, "order_id": order["id"], "fill_id": fill["id"] if fill else None,
                    "status": order["status"], "reason": order["rejection_reason"],
                    "risk": {"validation_id": order["risk_validation_id"]},
                    "execution_performed": bool(fill), "recovered": True,
                }
    symbol = intent.symbol
    side = intent.action
    quantity = intent.quantity
    bot_id = intent.bot_id
    now = intent.created_at
    snapshot = account_snapshot()
    with connect() as connection:
        seed_account(connection)
        if bot_id is not None and not connection.execute("SELECT 1 FROM bots WHERE id = ?", (bot_id,)).fetchone():
            raise ValueError(f"Bot {bot_id} does not exist")
        price = latest_price(connection, symbol)
        bot_exposure_notional = bot_open_exposure_notional(connection, bot_id) if bot_id is not None else None
        runtime = bot_runtime_state(connection, bot_id) if bot_id is not None else None
    notional = price * quantity
    existing_position = next((item for item in snapshot["positions"] if item["symbol"] == symbol), None)
    existing_exposure_notional = float(existing_position["market_value"]) if existing_position else 0.0
    with connect() as connection:
        last_loss_row = connection.execute(
            """SELECT created_at FROM simulated_ledger
               WHERE account_id = ? AND realized_pnl_delta < 0
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (ACCOUNT_ID,),
        ).fetchone()
        bot_daily_pnl = bot_daily_realized_pnl(connection, bot_id) if bot_id is not None else None
        last_loss_at = bot_last_loss_at(connection, bot_id) if bot_id is not None else (last_loss_row["created_at"] if last_loss_row else None)
    decision = validate_order_intent({
        "mode": "paper", "symbol": symbol, "side": "long", "requested_notional": notional,
        "account_id": ACCOUNT_ID, "bot_id": bot_id, "source": "paper_market_execution",
        "account_equity": snapshot["equity"], "daily_pnl": snapshot["daily_realized_pnl"],
        "account_daily_pnl": snapshot["daily_realized_pnl"], "bot_daily_pnl": bot_daily_pnl,
        "current_drawdown_pct": snapshot["drawdown_pct"],
        "current_exposure_notional": existing_exposure_notional,
        "account_current_exposure_notional": existing_exposure_notional,
        "bot_current_exposure_notional": bot_exposure_notional,
        "bot_runtime_status": runtime["status"] if runtime else None,
        "bot_runtime_reason": runtime["reason"] if runtime else None,
        "bot_paused_until": runtime["paused_until"] if runtime else None,
        "last_loss_at": last_loss_at,
        "reduces_exposure": side == "sell",
    })
    with connect() as connection:
        seed_account(connection)
        account = dict(connection.execute("SELECT * FROM simulated_accounts WHERE id = ?", (ACCOUNT_ID,)).fetchone())
        position_row = connection.execute("SELECT * FROM simulated_positions WHERE account_id = ? AND symbol = ?", (ACCOUNT_ID, symbol)).fetchone()
        position = dict(position_row) if position_row else None
        allocation = position_allocation(connection, symbol, bot_id, intent.bot_version_id, intent.strategy_hash)
        attributed_quantity = float(allocation["quantity"]) if allocation else attributed_position_quantity(connection, symbol, bot_id, account["created_at"])
        rejection = None
        fee = notional * FEE_RATE
        if not decision["approved"]:
            rejection = "; ".join(decision["reasons"])
        elif side == "buy" and notional + fee > float(account["cash_balance"]):
            rejection = "Insufficient simulated cash balance"
        elif side == "sell" and (not position or float(position["quantity"]) < quantity):
            rejection = "Insufficient simulated position quantity"
        elif side == "sell" and attributed_quantity + 1e-12 < quantity:
            owner = f"bot {bot_id}" if bot_id is not None else "manual paper desk"
            rejection = f"Insufficient position quantity attributed to {owner}"
        status = "rejected" if rejection else "filled"
        cursor = connection.execute(
            """INSERT INTO simulated_orders
            (account_id, bot_id, bot_version_id, strategy_hash, signal_evaluation_id, proposal_id,
             symbol, side, quantity, status, reference_price, fill_price, fee,
             risk_validation_id, rejection_reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ACCOUNT_ID, bot_id, intent.bot_version_id, intent.strategy_hash, intent.signal_evaluation_id,
             intent.proposal_id, symbol, side, quantity, status, price, price if not rejection else None,
             fee if not rejection else None, decision["validation_id"], rejection, now),
        )
        order_id = cursor.lastrowid
        if rejection:
            update_execution_intent(
                intent.id,
                "rejected",
                f"simulated_order:{order_id}",
                decision["validation_id"],
                connection=connection,
            )
            return {"intent_id": intent.id, "order_id": order_id, "status": status, "reason": rejection, "risk": decision, "execution_performed": False}

        realized_delta = 0.0
        if side == "buy":
            old_qty = float(position["quantity"]) if position else 0.0
            old_avg = float(position["average_price"]) if position else 0.0
            new_qty = old_qty + quantity
            new_avg = ((old_qty * old_avg) + notional) / new_qty
            connection.execute(
                """INSERT INTO simulated_positions (account_id, symbol, quantity, average_price, realized_pnl, updated_at)
                VALUES (?, ?, ?, ?, 0, ?) ON CONFLICT(account_id, symbol) DO UPDATE SET
                quantity = excluded.quantity, average_price = excluded.average_price, updated_at = excluded.updated_at""",
                (ACCOUNT_ID, symbol, new_qty, new_avg, now),
            )
            owner_key = allocation_owner_key(bot_id, intent.bot_version_id, intent.strategy_hash)
            old_alloc_qty = float(allocation["quantity"]) if allocation else 0.0
            old_alloc_avg = float(allocation["average_price"]) if allocation else 0.0
            new_alloc_qty = old_alloc_qty + quantity
            new_alloc_avg = ((old_alloc_qty * old_alloc_avg) + notional) / new_alloc_qty
            connection.execute(
                """INSERT INTO simulated_position_allocations (
                   account_id, symbol, owner_key, bot_id, bot_version_id, strategy_hash,
                   quantity, average_price, entry_fee_remaining, realized_pnl, revision, opened_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?)
                   ON CONFLICT(account_id, symbol, owner_key) DO UPDATE SET
                   quantity = excluded.quantity, average_price = excluded.average_price,
                   entry_fee_remaining = simulated_position_allocations.entry_fee_remaining + excluded.entry_fee_remaining,
                   revision = simulated_position_allocations.revision + 1, updated_at = excluded.updated_at""",
                (ACCOUNT_ID, symbol, owner_key, bot_id, intent.bot_version_id, intent.strategy_hash,
                 new_alloc_qty, new_alloc_avg, fee, now, now),
            )
            cash_delta = -(notional + fee)
        else:
            allocation_avg = float(allocation["average_price"]) if allocation else float(position["average_price"])
            entry_fee_alloc = (float(allocation["entry_fee_remaining"]) * quantity / float(allocation["quantity"])) if allocation and float(allocation["quantity"]) else 0.0
            realized_delta = quantity * (price - allocation_avg) - fee - entry_fee_alloc
            new_qty = float(position["quantity"]) - quantity
            remaining_cost = (float(position["quantity"]) * float(position["average_price"])) - (quantity * allocation_avg)
            new_global_avg = remaining_cost / new_qty if new_qty > 1e-12 else 0.0
            connection.execute("UPDATE simulated_positions SET quantity = ?, average_price = ?, realized_pnl = realized_pnl + ?, updated_at = ? WHERE id = ?", (new_qty, new_global_avg, realized_delta, now, position["id"]))
            if allocation:
                connection.execute(
                    """UPDATE simulated_position_allocations SET quantity = quantity - ?,
                       entry_fee_remaining = MAX(0, entry_fee_remaining - ?),
                       realized_pnl = realized_pnl + ?, revision = revision + 1, updated_at = ? WHERE id = ?""",
                    (quantity, entry_fee_alloc, realized_delta, now, allocation["id"]),
                )
            cash_delta = notional - fee
        new_cash = float(account["cash_balance"]) + cash_delta
        connection.execute("UPDATE simulated_accounts SET cash_balance = ?, realized_pnl = realized_pnl + ?, updated_at = ? WHERE id = ?", (new_cash, realized_delta, now, ACCOUNT_ID))
        fill_id = connection.execute("INSERT INTO simulated_fills (order_id, account_id, symbol, side, quantity, price, fee, filled_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (order_id, ACCOUNT_ID, symbol, side, quantity, price, fee, now)).lastrowid
        connection.execute("INSERT INTO simulated_ledger (account_id, event_type, reference_id, symbol, cash_delta, realized_pnl_delta, cash_balance, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (ACCOUNT_ID, "market_fill", fill_id, symbol, cash_delta, realized_delta, new_cash, json.dumps({"order_id": order_id, "side": side, "quantity": quantity, "price": price, "fee": fee, "trigger_reason": intent.trigger_reason}), now))
        update_execution_intent(intent.id, "filled", f"simulated_fill:{fill_id}", decision["validation_id"], connection=connection)
    return {"intent_id": intent.id, "order_id": order_id, "fill_id": fill_id, "status": status, "risk": decision, "execution_performed": True, "account": account_snapshot()}


def evaluate_open_protections(symbols: list[str] | None = None) -> dict:
    """Evaluate persisted paper protections against the latest local marks."""
    initialize_database()
    triggered = []
    with connect() as connection:
        rows = connection.execute(
            """SELECT a.*, p.stop_loss_price, p.take_profit_price, p.trailing_distance_pct, p.highest_price
               FROM simulated_position_allocations a JOIN paper_position_protections p ON p.allocation_id = a.id
               WHERE a.account_id = ? AND a.quantity > 0""", (ACCOUNT_ID,)
        ).fetchall()
        for row in rows:
            allocation = dict(row)
            if symbols and allocation["symbol"] not in symbols:
                continue
            mark_row = connection.execute("SELECT price FROM market_snapshots WHERE symbol = ? ORDER BY timestamp DESC, id DESC LIMIT 1", (allocation["symbol"],)).fetchone()
            if not mark_row:
                continue
            mark = float(mark_row["price"])
            stop = float(allocation["stop_loss_price"]) if allocation["stop_loss_price"] is not None else None
            take = float(allocation["take_profit_price"]) if allocation["take_profit_price"] is not None else None
            reason = "take_profit" if take is not None and mark >= take else "trailing_stop" if allocation["trailing_distance_pct"] and stop is not None and allocation["highest_price"] and mark <= stop and stop > float(allocation["average_price"]) else "stop_loss" if stop is not None and mark <= stop else None
            if not reason:
                continue
            triggered.append(place_market_order({
                "symbol": allocation["symbol"], "side": "sell", "quantity": allocation["quantity"],
                "bot_id": allocation["bot_id"], "bot_version_id": allocation["bot_version_id"],
                "strategy_hash": allocation["strategy_hash"], "trigger_reason": reason,
            }))
    return {"evaluated": len(rows), "triggered": len(triggered), "results": triggered}


def reset_account(initial_balance: float, reason: str) -> dict:
    initialize_database()
    now = utc_now_iso()
    with connect() as connection:
        seed_account(connection)
        connection.execute("DELETE FROM simulated_positions WHERE account_id = ?", (ACCOUNT_ID,))
        connection.execute("DELETE FROM simulated_position_allocations WHERE account_id = ?", (ACCOUNT_ID,))
        connection.execute(
            """UPDATE paper_order_proposals SET status = 'dismissed', reason = reason || ' Account reset invalidated proposal.',
               claim_token = NULL, claimed_at = NULL, updated_at = ? WHERE status = 'pending'""",
            (now,),
        )
        connection.execute("UPDATE simulated_accounts SET initial_balance = ?, cash_balance = ?, realized_pnl = 0, peak_equity = ?, created_at = ?, updated_at = ? WHERE id = ?", (initial_balance, initial_balance, initial_balance, now, now, ACCOUNT_ID))
        connection.execute("INSERT INTO simulated_ledger (account_id, event_type, reference_id, symbol, cash_delta, realized_pnl_delta, cash_balance, payload_json, created_at) VALUES (?, 'account_reset', NULL, NULL, 0, 0, ?, ?, ?)", (ACCOUNT_ID, initial_balance, json.dumps({"reason": reason, "initial_balance": initial_balance}), now))
    return account_snapshot()
