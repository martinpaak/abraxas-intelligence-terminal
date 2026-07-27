from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone

from backend.app.storage.sqlite import connect, initialize_database


DEFAULT_LIMITS = {
    "max_position_pct": 10.0,
    "max_daily_loss_pct": 3.0,
    "max_drawdown_pct": 12.0,
    "cooldown_minutes": 30,
    "symbol_whitelist": ["BTCUSDT", "ETHUSDT"],
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed(connection) -> None:
    now = utc_now_iso()
    connection.execute(
        """
        INSERT OR IGNORE INTO risk_limits (
            id, max_position_pct, max_daily_loss_pct, max_drawdown_pct,
            cooldown_minutes, symbol_whitelist, updated_at
        ) VALUES (1, ?, ?, ?, ?, ?, ?)
        """,
        (
            DEFAULT_LIMITS["max_position_pct"],
            DEFAULT_LIMITS["max_daily_loss_pct"],
            DEFAULT_LIMITS["max_drawdown_pct"],
            DEFAULT_LIMITS["cooldown_minutes"],
            json.dumps(DEFAULT_LIMITS["symbol_whitelist"]),
            now,
        ),
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO risk_state (id, kill_switch_active, reason, updated_at)
        VALUES (1, 1, 'Safe default: execution remains locked', ?)
        """,
        (now,),
    )


def _limits(row) -> dict:
    return {
        "max_position_pct": float(row["max_position_pct"]),
        "max_daily_loss_pct": float(row["max_daily_loss_pct"]),
        "max_drawdown_pct": float(row["max_drawdown_pct"]),
        "cooldown_minutes": int(row["cooldown_minutes"]),
        "symbol_whitelist": json.loads(row["symbol_whitelist"]),
        "updated_at": row["updated_at"],
    }


def _normalize_limits(payload: dict) -> dict:
    normalized = {
        "max_position_pct": float(payload["max_position_pct"]),
        "max_daily_loss_pct": float(payload["max_daily_loss_pct"]),
        "max_drawdown_pct": float(payload["max_drawdown_pct"]),
        "cooldown_minutes": int(payload["cooldown_minutes"]),
        "symbol_whitelist": sorted({str(symbol).strip().upper() for symbol in payload["symbol_whitelist"] if str(symbol).strip()}),
    }
    for key in ("max_position_pct", "max_daily_loss_pct", "max_drawdown_pct"):
        if not 0 < normalized[key] <= 100:
            raise ValueError(f"{key} must be between 0 and 100")
    if normalized["cooldown_minutes"] < 0:
        raise ValueError("cooldown_minutes must be zero or greater")
    if not normalized["symbol_whitelist"]:
        raise ValueError("Symbol whitelist must contain at least one symbol")
    return normalized


def _policy_from_row(row) -> dict:
    policy = dict(row)
    policy["limits"] = {
        "max_position_pct": float(policy.pop("max_position_pct")),
        "max_daily_loss_pct": float(policy.pop("max_daily_loss_pct")),
        "max_drawdown_pct": float(policy.pop("max_drawdown_pct")),
        "cooldown_minutes": int(policy.pop("cooldown_minutes")),
        "symbol_whitelist": json.loads(policy.pop("symbol_whitelist")),
    }
    return policy


def _active_policy(connection, scope_type: str, scope_id: int) -> dict | None:
    row = connection.execute(
        """SELECT p.*, v.id AS version_id, v.max_position_pct, v.max_daily_loss_pct,
        v.max_drawdown_pct, v.cooldown_minutes, v.symbol_whitelist, v.notes,
        v.created_at AS version_created_at
        FROM risk_policies AS p JOIN risk_policy_versions AS v
          ON v.policy_id = p.id AND v.version = p.current_version
        WHERE p.scope_type = ? AND p.scope_id = ? AND p.status = 'active'""",
        (scope_type, scope_id),
    ).fetchone()
    return _policy_from_row(row) if row else None


def _resolve_risk_limits(connection, account_id: int | None, bot_id: int | None) -> tuple[dict, dict]:
    _seed(connection)
    global_limits = _limits(connection.execute("SELECT * FROM risk_limits WHERE id = 1").fetchone())
    effective = {key: value for key, value in global_limits.items() if key != "updated_at"}
    layers = [{
        "scope_type": "global", "scope_id": 1, "policy_id": None,
        "version": global_limits["updated_at"], "name": "Global hard limits",
        "limits": effective.copy(),
    }]
    scoped = []
    if account_id is not None:
        scoped.append(("account", account_id))
    if bot_id is not None:
        scoped.append(("bot", bot_id))
    for scope_type, scope_id in scoped:
        policy = _active_policy(connection, scope_type, scope_id)
        if not policy:
            continue
        limits = policy["limits"]
        effective["max_position_pct"] = min(effective["max_position_pct"], limits["max_position_pct"])
        effective["max_daily_loss_pct"] = min(effective["max_daily_loss_pct"], limits["max_daily_loss_pct"])
        effective["max_drawdown_pct"] = min(effective["max_drawdown_pct"], limits["max_drawdown_pct"])
        effective["cooldown_minutes"] = max(effective["cooldown_minutes"], limits["cooldown_minutes"])
        effective["symbol_whitelist"] = sorted(set(effective["symbol_whitelist"]) & set(limits["symbol_whitelist"]))
        layers.append({
            "scope_type": scope_type, "scope_id": scope_id, "policy_id": policy["id"],
            "version_id": policy["version_id"], "version": policy["current_version"],
            "name": policy["name"], "limits": limits,
        })
    fingerprint_payload = {
        "layers": [{key: layer.get(key) for key in ("scope_type", "scope_id", "policy_id", "version_id", "version")} for layer in layers],
        "effective_limits": effective,
    }
    fingerprint = hashlib.sha256(json.dumps(fingerprint_payload, sort_keys=True).encode("utf-8")).hexdigest()
    return effective, {"contract": "risk_policy_resolution_v1", "rule": "most_restrictive_wins", "layers": layers, "effective_limits": effective, "fingerprint": fingerprint}


def resolve_risk_policy(account_id: int | None = None, bot_id: int | None = None, *, connection=None) -> dict:
    """Return the effective, auditable policy without creating a validation row."""
    if connection is not None:
        _, resolution = _resolve_risk_limits(connection, account_id, bot_id)
        return resolution
    initialize_database()
    with connect() as owned_connection:
        _, resolution = _resolve_risk_limits(owned_connection, account_id, bot_id)
    return resolution


def list_risk_policies() -> dict:
    initialize_database()
    with connect() as connection:
        policies = [_policy_from_row(row) for row in connection.execute(
            """SELECT p.*, v.id AS version_id, v.max_position_pct, v.max_daily_loss_pct,
            v.max_drawdown_pct, v.cooldown_minutes, v.symbol_whitelist, v.notes,
            v.created_at AS version_created_at
            FROM risk_policies AS p JOIN risk_policy_versions AS v
              ON v.policy_id = p.id AND v.version = p.current_version
            ORDER BY p.scope_type, p.scope_id"""
        ).fetchall()]
        accounts = [dict(row) for row in connection.execute(
            "SELECT id, initial_balance, cash_balance, created_at FROM simulated_accounts ORDER BY id"
        ).fetchall()]
        bots = [dict(row) for row in connection.execute(
            "SELECT id, name, base_symbol, timeframe, status FROM bots ORDER BY id"
        ).fetchall()]
    return {"policies": policies, "targets": {"accounts": accounts, "bots": bots}, "resolution_rule": "most_restrictive_wins"}


def save_risk_policy(scope_type: str, scope_id: int, payload: dict) -> dict:
    initialize_database()
    scope_type = str(scope_type).strip().lower()
    scope_id = int(scope_id)
    if scope_type not in {"account", "bot"}:
        raise ValueError("Risk policy scope must be account or bot")
    limits = _normalize_limits(payload)
    name = str(payload.get("name") or f"{scope_type.title()} #{scope_id} policy").strip()
    notes = str(payload.get("notes") or "").strip()
    if len(name) < 3 or len(notes) < 3:
        raise ValueError("Policy name and auditable notes are required")
    now = utc_now_iso()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        target_table = "simulated_accounts" if scope_type == "account" else "bots"
        if not connection.execute(f"SELECT 1 FROM {target_table} WHERE id = ?", (scope_id,)).fetchone():
            raise ValueError(f"{scope_type.title()} {scope_id} does not exist")
        existing = connection.execute(
            "SELECT * FROM risk_policies WHERE scope_type = ? AND scope_id = ?", (scope_type, scope_id)
        ).fetchone()
        if existing:
            policy_id = int(existing["id"])
            version = int(existing["current_version"]) + 1
            connection.execute(
                "UPDATE risk_policies SET name = ?, status = 'active', current_version = ?, updated_at = ? WHERE id = ?",
                (name, version, now, policy_id),
            )
        else:
            version = 1
            policy_id = int(connection.execute(
                "INSERT INTO risk_policies (scope_type, scope_id, name, status, current_version, created_at, updated_at) VALUES (?, ?, ?, 'active', 1, ?, ?)",
                (scope_type, scope_id, name, now, now),
            ).lastrowid)
        version_id = int(connection.execute(
            """INSERT INTO risk_policy_versions (
            policy_id, version, max_position_pct, max_daily_loss_pct, max_drawdown_pct,
            cooldown_minutes, symbol_whitelist, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (policy_id, version, limits["max_position_pct"], limits["max_daily_loss_pct"],
             limits["max_drawdown_pct"], limits["cooldown_minutes"],
             json.dumps(limits["symbol_whitelist"]), notes, now),
        ).lastrowid)
        audit_payload = {"policy_id": policy_id, "version_id": version_id, "version": version, "scope_type": scope_type, "scope_id": scope_id, "name": name, "limits": limits, "notes": notes}
        connection.execute(
            "INSERT INTO risk_audit_log (event_type, payload_json, created_at) VALUES ('policy_version_created', ?, ?)",
            (json.dumps(audit_payload, sort_keys=True), now),
        )
    return list_risk_policies()


def archive_risk_policy(scope_type: str, scope_id: int, reason: str) -> dict:
    initialize_database()
    scope_type = str(scope_type).strip().lower()
    clean_reason = str(reason).strip()
    if scope_type not in {"account", "bot"} or len(clean_reason) < 3:
        raise ValueError("Valid scope and auditable reason are required")
    now = utc_now_iso()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        policy = connection.execute(
            "SELECT id FROM risk_policies WHERE scope_type = ? AND scope_id = ?", (scope_type, int(scope_id))
        ).fetchone()
        if not policy:
            raise ValueError("Risk policy not found")
        connection.execute("UPDATE risk_policies SET status = 'archived', updated_at = ? WHERE id = ?", (now, policy["id"]))
        connection.execute(
            "INSERT INTO risk_audit_log (event_type, payload_json, created_at) VALUES ('policy_archived', ?, ?)",
            (json.dumps({"policy_id": policy["id"], "scope_type": scope_type, "scope_id": int(scope_id), "reason": clean_reason}, sort_keys=True), now),
        )
    return list_risk_policies()


def get_risk_profile(audit_limit: int = 20) -> dict:
    initialize_database()
    with connect() as connection:
        _seed(connection)
        limits = _limits(connection.execute("SELECT * FROM risk_limits WHERE id = 1").fetchone())
        state = dict(connection.execute("SELECT * FROM risk_state WHERE id = 1").fetchone())
        events = [dict(row) for row in connection.execute(
            "SELECT id, event_type, payload_json, created_at FROM risk_audit_log ORDER BY id DESC LIMIT ?",
            (audit_limit,),
        ).fetchall()]
        validations = [dict(row) for row in connection.execute(
            """SELECT validation.id, validation.mode, validation.symbol, validation.account_id,
            validation.bot_id, validation.policy_fingerprint, validation.policy_resolution_json, validation.approved,
            validation.request_json, validation.decision_json, validation.created_at,
            intent.id AS execution_intent_id, intent.status AS execution_status,
            intent.result_reference
            FROM risk_validation_log AS validation
            LEFT JOIN execution_intents AS intent ON intent.risk_validation_id = validation.id
            ORDER BY validation.id DESC LIMIT ?""",
            (audit_limit,),
        ).fetchall()]
    for event in events:
        event["payload"] = json.loads(event.pop("payload_json"))
    for validation in validations:
        validation["approved"] = bool(validation["approved"])
        validation["request"] = json.loads(validation.pop("request_json"))
        validation["decision"] = json.loads(validation.pop("decision_json"))
        resolution_json = validation.pop("policy_resolution_json", None)
        validation["policy_resolution"] = json.loads(resolution_json) if resolution_json else validation["decision"].get("policy_resolution")
    policy_registry = list_risk_policies()
    return {
        "limits": limits,
        "kill_switch": {
            "active": bool(state["kill_switch_active"]),
            "reason": state["reason"],
            "updated_at": state["updated_at"],
        },
        "execution": {"paper": "risk_gated", "spot_simulation": "risk_gated", "live": "blocked"},
        "audit_log": events,
        "validation_log": validations,
        "policies": policy_registry["policies"],
        "policy_targets": policy_registry["targets"],
        "policy_resolution_rule": policy_registry["resolution_rule"],
    }


def update_risk_limits(payload: dict) -> dict:
    initialize_database()
    now = utc_now_iso()
    normalized = _normalize_limits(payload)
    symbols = normalized["symbol_whitelist"]
    with connect() as connection:
        _seed(connection)
        connection.execute(
            """
            UPDATE risk_limits SET max_position_pct = ?, max_daily_loss_pct = ?,
                max_drawdown_pct = ?, cooldown_minutes = ?, symbol_whitelist = ?, updated_at = ?
            WHERE id = 1
            """,
            (
                normalized["max_position_pct"], normalized["max_daily_loss_pct"],
                normalized["max_drawdown_pct"], normalized["cooldown_minutes"],
                json.dumps(symbols), now,
            ),
        )
        connection.execute(
            "INSERT INTO risk_audit_log (event_type, payload_json, created_at) VALUES (?, ?, ?)",
            ("limits_updated", json.dumps(normalized, sort_keys=True), now),
        )
    return get_risk_profile()


def set_kill_switch(active: bool, reason: str) -> dict:
    initialize_database()
    now = utc_now_iso()
    clean_reason = reason.strip()
    if not clean_reason:
        raise ValueError("A reason is required for every kill switch change")
    payload = {"active": active, "reason": clean_reason}
    with connect() as connection:
        _seed(connection)
        connection.execute(
            "UPDATE risk_state SET kill_switch_active = ?, reason = ?, updated_at = ? WHERE id = 1",
            (int(active), clean_reason, now),
        )
        connection.execute(
            "INSERT INTO risk_audit_log (event_type, payload_json, created_at) VALUES (?, ?, ?)",
            ("kill_switch_changed", json.dumps(payload, sort_keys=True), now),
        )
    return get_risk_profile()


def get_risk_validation(validation_id: int) -> dict:
    initialize_database()
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM risk_validation_log WHERE id = ?",
            (validation_id,),
        ).fetchone()
    if not row:
        raise ValueError("Risk validation not found")
    validation = dict(row)
    validation["approved"] = bool(validation["approved"])
    validation["request"] = json.loads(validation.pop("request_json"))
    validation["decision"] = json.loads(validation.pop("decision_json"))
    resolution_json = validation.pop("policy_resolution_json", None)
    validation["policy_resolution"] = json.loads(resolution_json) if resolution_json else validation["decision"].get("policy_resolution")
    return validation


def validate_order_intent(payload: dict, *, persist: bool = True) -> dict:
    initialize_database()
    now = datetime.now(timezone.utc)
    symbol = str(payload["symbol"]).strip().upper()
    mode = str(payload["mode"]).strip().lower()
    side = str(payload["side"]).strip().lower()
    account_equity = float(payload["account_equity"])
    requested_notional = float(payload["requested_notional"])
    account_id = int(payload["account_id"]) if payload.get("account_id") is not None else None
    bot_id = int(payload["bot_id"]) if payload.get("bot_id") is not None else None
    current_exposure_notional = max(0.0, float(payload.get("current_exposure_notional") or 0))
    account_exposure_notional = max(0.0, float(payload.get("account_current_exposure_notional", current_exposure_notional) or 0))
    bot_exposure_notional = max(0.0, float(payload.get("bot_current_exposure_notional", current_exposure_notional) or 0)) if bot_id is not None else None
    daily_pnl = float(payload["daily_pnl"])
    account_daily_pnl = float(payload.get("account_daily_pnl", daily_pnl))
    bot_daily_pnl = float(payload.get("bot_daily_pnl", daily_pnl)) if bot_id is not None else None
    current_drawdown_pct = float(payload["current_drawdown_pct"])

    with connect() as connection:
        _seed(connection)
        account_limits, account_policy_resolution = _resolve_risk_limits(connection, account_id, None)
        limits, policy_resolution = _resolve_risk_limits(connection, account_id, bot_id)
        state = dict(connection.execute("SELECT * FROM risk_state WHERE id = 1").fetchone())

        reduces_exposure = bool(payload.get("reduces_exposure"))
        projected_account_exposure = max(0.0, account_exposure_notional - requested_notional) if reduces_exposure else account_exposure_notional + requested_notional
        projected_bot_exposure = (max(0.0, bot_exposure_notional - requested_notional) if reduces_exposure else bot_exposure_notional + requested_notional) if bot_exposure_notional is not None else None
        account_current_position_pct = (account_exposure_notional / account_equity) * 100
        account_position_pct = (projected_account_exposure / account_equity) * 100
        bot_current_position_pct = (bot_exposure_notional / account_equity) * 100 if bot_exposure_notional is not None else None
        bot_position_pct = (projected_bot_exposure / account_equity) * 100 if projected_bot_exposure is not None else None
        current_position_pct = bot_current_position_pct if bot_id is not None else account_current_position_pct
        position_pct = bot_position_pct if bot_id is not None else account_position_pct
        projected_exposure_notional = projected_bot_exposure if bot_id is not None else projected_account_exposure
        account_daily_loss_pct = max(0.0, (-account_daily_pnl / account_equity) * 100)
        bot_daily_loss_pct = max(0.0, (-bot_daily_pnl / account_equity) * 100) if bot_daily_pnl is not None else None
        reasons = []
        checks = []

        def check(code: str, passed: bool, detail: str) -> None:
            checks.append({"code": code, "passed": passed, "detail": detail})
            if not passed:
                reasons.append(detail)

        kill_allowed = reduces_exposure or not bool(state["kill_switch_active"])
        check("kill_switch", kill_allowed, "Close-only reduction allowed while kill switch is active" if reduces_exposure and bool(state["kill_switch_active"]) else ("Kill switch inactive" if not bool(state["kill_switch_active"]) else "Kill switch is active"))
        mode_allowed = mode in {"validation", "paper", "spot"}
        check("mode", mode_allowed, f"{mode.title()} mode allowed" if mode_allowed else "Live mode is blocked")
        check("side", side == "long", "Long intent supported" if side == "long" else "Only long intents are supported in this phase")
        symbol_allowed = reduces_exposure or symbol in limits["symbol_whitelist"]
        check(
            "symbol_whitelist",
            symbol_allowed,
            "Close-only reduction bypasses symbol whitelist"
            if reduces_exposure and symbol not in limits["symbol_whitelist"]
            else (f"{symbol} is authorized" if symbol_allowed else f"{symbol} is outside the symbol whitelist"),
        )
        position_allowed = reduces_exposure or account_position_pct <= account_limits["max_position_pct"]
        position_detail = f"Account exposure reduces from {account_current_position_pct:.2f}% to {account_position_pct:.2f}%" if reduces_exposure else (f"Projected exposure {account_position_pct:.2f}% within account limit" if position_allowed else f"Projected exposure {account_position_pct:.2f}% exceeds account limit {account_limits['max_position_pct']:.2f}%")
        check("max_position", position_allowed, position_detail)
        if bot_id is not None:
            bot_position_allowed = reduces_exposure or bot_position_pct <= limits["max_position_pct"]
            bot_position_detail = f"Bot exposure reduces from {bot_current_position_pct:.2f}% to {bot_position_pct:.2f}%" if reduces_exposure else (f"Bot projected exposure {bot_position_pct:.2f}% within limit" if bot_position_allowed else f"Bot projected exposure {bot_position_pct:.2f}% exceeds {limits['max_position_pct']:.2f}%")
            check("bot_max_position", bot_position_allowed, bot_position_detail)
        daily_allowed = reduces_exposure or account_daily_loss_pct < account_limits["max_daily_loss_pct"]
        drawdown_allowed = reduces_exposure or current_drawdown_pct < limits["max_drawdown_pct"]
        check("max_daily_loss", daily_allowed, "Close-only reduction bypasses entry loss limit" if reduces_exposure else (f"Account daily loss {account_daily_loss_pct:.2f}% within limit" if daily_allowed else f"Account daily loss {account_daily_loss_pct:.2f}% reached limit {account_limits['max_daily_loss_pct']:.2f}%"))
        if bot_id is not None:
            bot_daily_allowed = reduces_exposure or bot_daily_loss_pct < limits["max_daily_loss_pct"]
            check("bot_max_daily_loss", bot_daily_allowed, "Close-only reduction bypasses bot entry loss limit" if reduces_exposure else (f"Bot daily loss {bot_daily_loss_pct:.2f}% within limit" if bot_daily_allowed else f"Bot daily loss {bot_daily_loss_pct:.2f}% reached limit {limits['max_daily_loss_pct']:.2f}%"))
        check("max_drawdown", drawdown_allowed, "Close-only reduction bypasses entry drawdown limit" if reduces_exposure else (f"Drawdown {current_drawdown_pct:.2f}% within limit" if drawdown_allowed else f"Drawdown {current_drawdown_pct:.2f}% reached limit {limits['max_drawdown_pct']:.2f}%"))

        last_loss_at = payload.get("last_loss_at")
        cooldown_remaining = 0
        if last_loss_at:
            loss_time = datetime.fromisoformat(str(last_loss_at).replace("Z", "+00:00"))
            if loss_time.tzinfo is None:
                loss_time = loss_time.replace(tzinfo=timezone.utc)
            elapsed_minutes = max(0, int((now - loss_time.astimezone(timezone.utc)).total_seconds() // 60))
            cooldown_remaining = max(0, limits["cooldown_minutes"] - elapsed_minutes)
        cooldown_allowed = reduces_exposure or cooldown_remaining == 0
        check("cooldown", cooldown_allowed, "Close-only reduction bypasses entry cooldown" if reduces_exposure else ("Cooldown clear" if cooldown_allowed else f"Cooldown has {cooldown_remaining} minutes remaining"))

        approved = not reasons
        decision = {
            "approved": approved,
            "decision": "approved" if approved else "rejected",
            "reasons": reasons,
            "checks": checks,
            "metrics": {
                "position_pct": round(position_pct, 4),
                "current_position_pct": round(current_position_pct, 4),
                "projected_exposure_notional": round(projected_exposure_notional, 8),
                "account_position_pct": round(account_position_pct, 4),
                "account_current_position_pct": round(account_current_position_pct, 4),
                "projected_account_exposure_notional": round(projected_account_exposure, 8),
                "bot_position_pct": round(bot_position_pct, 4) if bot_position_pct is not None else None,
                "bot_current_position_pct": round(bot_current_position_pct, 4) if bot_current_position_pct is not None else None,
                "projected_bot_exposure_notional": round(projected_bot_exposure, 8) if projected_bot_exposure is not None else None,
                "daily_loss_pct": round(account_daily_loss_pct, 4),
                "account_daily_loss_pct": round(account_daily_loss_pct, 4),
                "bot_daily_loss_pct": round(bot_daily_loss_pct, 4) if bot_daily_loss_pct is not None else None,
                "current_drawdown_pct": round(current_drawdown_pct, 4),
                "cooldown_remaining_minutes": cooldown_remaining,
                "close_only": reduces_exposure,
            },
            "limits_version": policy_resolution["fingerprint"],
            "policy_fingerprint": policy_resolution["fingerprint"],
            "policy_resolution": policy_resolution,
            "account_policy_resolution": account_policy_resolution,
            "evaluated_at": now.isoformat(),
            "execution_performed": False,
        }
        normalized_request = {**payload, "symbol": symbol, "mode": mode, "side": side}
        if persist:
            cursor = connection.execute(
                """
                INSERT INTO risk_validation_log (
                    mode, symbol, account_id, bot_id, policy_fingerprint, policy_resolution_json,
                    approved, request_json, decision_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (mode, symbol, account_id, bot_id, policy_resolution["fingerprint"],
                 json.dumps(policy_resolution, sort_keys=True), int(approved),
                 json.dumps(normalized_request, sort_keys=True), json.dumps(decision, sort_keys=True), now.isoformat()),
            )
            decision["validation_id"] = cursor.lastrowid
        else:
            decision["validation_id"] = None
            decision["persistence"] = "preview_only"
    return decision
