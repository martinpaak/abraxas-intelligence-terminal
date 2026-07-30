from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from backend.app.services.data_center_service import get_dataset_preview
from backend.app.storage import sqlite as storage_sqlite
from backend.app.storage.paper import account_snapshot, evaluate_bot_circuit_breaker, persist_bot_equity_snapshot, place_market_order, set_bot_circuit_breaker, set_bot_runtime_state
from backend.app.storage.risk import (
    archive_risk_policy,
    list_risk_policies,
    save_risk_policy,
    set_kill_switch,
    update_risk_limits,
    validate_order_intent,
)
from backend.app.storage.sqlite import connect, initialize_database


class RiskPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = storage_sqlite.DB_PATH
        storage_sqlite.DB_PATH = Path(self.temp_dir.name) / "risk-policies.db"
        initialize_database()
        now = datetime.now(timezone.utc).isoformat()
        with connect() as connection:
            connection.execute(
                """INSERT INTO market_snapshots (timestamp, symbol, price, change_24h, volume_24h,
                fear_greed_value, fear_greed_label, risk_level, abraxas_reading)
                VALUES (?, 'BTCUSDT', 50000, 0, 1000000, 50, 'Neutral', 'NORMAL', 'fixture')""",
                (now,),
            )
            connection.execute(
                """INSERT INTO bots (id, name, description, status, mode, base_symbol, timeframe,
                risk_profile, created_at, updated_at)
                VALUES (1, 'Risk Bot', 'fixture', 'active', 'paper', 'BTCUSDT', '15m', 'strict', ?, ?)""",
                (now, now),
            )
        account_snapshot()
        update_risk_limits({
            "max_position_pct": 100,
            "max_daily_loss_pct": 100,
            "max_drawdown_pct": 100,
            "cooldown_minutes": 0,
            "symbol_whitelist": ["BTCUSDT", "ETHUSDT"],
        })
        set_kill_switch(False, "Risk policy test")

    def tearDown(self) -> None:
        storage_sqlite.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    @staticmethod
    def policy(max_position: float, symbols: list[str], notes: str = "test policy") -> dict:
        return {
            "name": "Scoped test policy",
            "notes": notes,
            "max_position_pct": max_position,
            "max_daily_loss_pct": 50,
            "max_drawdown_pct": 50,
            "cooldown_minutes": 5,
            "symbol_whitelist": symbols,
        }

    def test_most_restrictive_account_and_bot_layers_are_persisted(self) -> None:
        save_risk_policy("account", 1, self.policy(20, ["BTCUSDT", "ETHUSDT"]))
        save_risk_policy("bot", 1, self.policy(5, ["BTCUSDT"]))
        decision = validate_order_intent({
            "mode": "paper", "symbol": "BTCUSDT", "side": "long",
            "requested_notional": 600, "current_exposure_notional": 0,
            "account_equity": 10_000, "daily_pnl": 0, "current_drawdown_pct": 0,
            "account_id": 1, "bot_id": 1,
        })
        self.assertFalse(decision["approved"])
        resolution = decision["policy_resolution"]
        self.assertEqual([layer["scope_type"] for layer in resolution["layers"]], ["global", "account", "bot"])
        self.assertEqual(resolution["effective_limits"]["max_position_pct"], 5)
        self.assertEqual(resolution["effective_limits"]["symbol_whitelist"], ["BTCUSDT"])
        self.assertEqual(len(resolution["fingerprint"]), 64)
        with connect() as connection:
            row = connection.execute("SELECT account_id, bot_id, policy_fingerprint, policy_resolution_json FROM risk_validation_log WHERE id = ?", (decision["validation_id"],)).fetchone()
        self.assertEqual(row["account_id"], 1)
        self.assertEqual(row["bot_id"], 1)
        self.assertEqual(row["policy_fingerprint"], resolution["fingerprint"])
        self.assertTrue(row["policy_resolution_json"])

    def test_policy_versions_are_immutable_and_archive_restores_parent_limits(self) -> None:
        save_risk_policy("account", 1, self.policy(25, ["BTCUSDT", "ETHUSDT"], "version one"))
        save_risk_policy("account", 1, self.policy(20, ["BTCUSDT"], "version two"))
        registry = list_risk_policies()
        account_policy = registry["policies"][0]
        self.assertEqual(account_policy["current_version"], 2)
        with connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM risk_policy_versions WHERE policy_id = ?", (account_policy["id"],)).fetchone()[0], 2)
        archive_risk_policy("account", 1, "return to global test limits")
        decision = validate_order_intent({
            "mode": "paper", "symbol": "ETHUSDT", "side": "long",
            "requested_notional": 5000, "account_equity": 10_000,
            "daily_pnl": 0, "current_drawdown_pct": 0, "account_id": 1,
        }, persist=False)
        self.assertTrue(decision["approved"])
        self.assertEqual(len(decision["policy_resolution"]["layers"]), 1)

    def test_paper_order_consumes_bot_policy_and_tables_reach_data_center(self) -> None:
        save_risk_policy("bot", 1, self.policy(0.5, ["BTCUSDT"], "paper execution limit"))
        result = place_market_order({"symbol": "BTCUSDT", "side": "buy", "quantity": 0.002, "bot_id": 1})
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["risk"]["policy_resolution"]["effective_limits"]["max_position_pct"], 0.5)
        self.assertEqual(result["risk"]["policy_resolution"]["layers"][-1]["scope_type"], "bot")
        self.assertEqual(len(get_dataset_preview("risk_policies", 10)["rows"]), 1)
        self.assertEqual(len(get_dataset_preview("risk_policy_versions", 10)["rows"]), 1)

    def test_bot_daily_loss_budget_is_attributed_and_blocks_new_entries(self) -> None:
        policy = self.policy(100, ["BTCUSDT"], "one percent bot daily loss budget")
        policy["max_daily_loss_pct"] = 1
        policy["cooldown_minutes"] = 0
        save_risk_policy("bot", 1, policy)

        opened = place_market_order({"symbol": "BTCUSDT", "side": "buy", "quantity": 0.02, "bot_id": 1})
        self.assertEqual(opened["status"], "filled")
        now = datetime.now(timezone.utc).isoformat()
        with connect() as connection:
            connection.execute(
                """INSERT INTO market_snapshots (timestamp, symbol, price, change_24h, volume_24h,
                fear_greed_value, fear_greed_label, risk_level, abraxas_reading)
                VALUES (?, 'BTCUSDT', 45000, -10, 1000000, 30, 'Fear', 'ELEVATED', 'loss fixture')""",
                (now,),
            )

        closed = place_market_order({"symbol": "BTCUSDT", "side": "sell", "quantity": 0.02, "bot_id": 1})
        self.assertEqual(closed["status"], "filled")
        snapshot = account_snapshot()
        runtime = next(item["risk_budget"] for item in snapshot["bot_performance"] if item["id"] == 1)
        self.assertEqual(runtime["status"], "blocked")
        self.assertGreater(runtime["daily_loss_usage_pct"], 100)
        self.assertEqual(runtime["policy_scope"], "bot")

        rejected = place_market_order({"symbol": "BTCUSDT", "side": "buy", "quantity": 0.001, "bot_id": 1})
        self.assertEqual(rejected["status"], "rejected")
        bot_check = next(check for check in rejected["risk"]["checks"] if check["code"] == "bot_max_daily_loss")
        account_check = next(check for check in rejected["risk"]["checks"] if check["code"] == "max_daily_loss")
        self.assertFalse(bot_check["passed"])
        self.assertTrue(account_check["passed"])

    def test_bot_position_budget_is_isolated_from_other_bots(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with connect() as connection:
            connection.execute(
                """INSERT INTO bots (id, name, description, status, mode, base_symbol, timeframe,
                risk_profile, created_at, updated_at)
                VALUES (2, 'Second Risk Bot', 'fixture', 'active', 'paper', 'BTCUSDT', '15m', 'strict', ?, ?)""",
                (now, now),
            )
        save_risk_policy("bot", 1, self.policy(5, ["BTCUSDT"], "isolated capital for bot one"))
        save_risk_policy("bot", 2, self.policy(5, ["BTCUSDT"], "isolated capital for bot two"))

        bot_one = place_market_order({"symbol": "BTCUSDT", "side": "buy", "quantity": 0.008, "bot_id": 1})
        bot_two = place_market_order({"symbol": "BTCUSDT", "side": "buy", "quantity": 0.008, "bot_id": 2})
        self.assertEqual(bot_one["status"], "filled")
        self.assertEqual(bot_two["status"], "filled")

        snapshot = account_snapshot()
        runtime = next(item["risk_budget"] for item in snapshot["bot_performance"] if item["id"] == 1)
        self.assertEqual(runtime["status"], "warning")
        self.assertAlmostEqual(runtime["capital_used"], 400)
        self.assertGreaterEqual(runtime["capital_usage_pct"], 80)

        rejected = place_market_order({"symbol": "BTCUSDT", "side": "buy", "quantity": 0.003, "bot_id": 1})
        self.assertEqual(rejected["status"], "rejected")
        bot_check = next(check for check in rejected["risk"]["checks"] if check["code"] == "bot_max_position")
        account_check = next(check for check in rejected["risk"]["checks"] if check["code"] == "max_position")
        self.assertFalse(bot_check["passed"])
        self.assertTrue(account_check["passed"])
        self.assertGreater(rejected["risk"]["metrics"]["bot_position_pct"], 5)
        self.assertLess(rejected["risk"]["metrics"]["account_position_pct"], 10)

    def test_paused_bot_blocks_entries_but_allows_close_and_is_auditable(self) -> None:
        opened = place_market_order({"symbol": "BTCUSDT", "side": "buy", "quantity": 0.001, "bot_id": 1})
        self.assertEqual(opened["status"], "filled")
        paused = set_bot_runtime_state(1, "paused", "Operator review after anomaly", pause_minutes=60)
        self.assertEqual(paused["status"], "paused")
        self.assertTrue(paused["entry_blocked"])

        rejected = place_market_order({"symbol": "BTCUSDT", "side": "buy", "quantity": 0.001, "bot_id": 1})
        self.assertEqual(rejected["status"], "rejected")
        runtime_check = next(check for check in rejected["risk"]["checks"] if check["code"] == "bot_runtime")
        self.assertFalse(runtime_check["passed"])

        closed = place_market_order({"symbol": "BTCUSDT", "side": "sell", "quantity": 0.001, "bot_id": 1})
        self.assertEqual(closed["status"], "filled")
        close_check = next(check for check in closed["risk"]["checks"] if check["code"] == "bot_runtime")
        self.assertTrue(close_check["passed"])

        resumed = set_bot_runtime_state(1, "active", "Operator review completed")
        self.assertEqual(resumed["status"], "active")
        reopened = place_market_order({"symbol": "BTCUSDT", "side": "buy", "quantity": 0.001, "bot_id": 1})
        self.assertEqual(reopened["status"], "filled")
        snapshot = account_snapshot()
        runtime = next(item["runtime"] for item in snapshot["bot_performance"] if item["id"] == 1)
        self.assertEqual(runtime["status"], "active")
        self.assertEqual([event["event_type"] for event in snapshot["bot_runtime_events"][:2]], ["resumed", "paused"])
        self.assertEqual(len(get_dataset_preview("paper_bot_runtime_state", 10)["rows"]), 1)
        self.assertEqual(len(get_dataset_preview("paper_bot_runtime_events", 10)["rows"]), 2)

    def test_rejection_burst_trips_auditable_bot_circuit_breaker(self) -> None:
        configured = set_bot_circuit_breaker(1, {
            "enabled": True,
            "max_consecutive_losses": 3,
            "max_rejections": 2,
            "rejection_window_minutes": 15,
            "pause_minutes": 30,
        })
        self.assertEqual(configured["max_rejections"], 2)
        for _ in range(2):
            rejected = place_market_order({"symbol": "BTCUSDT", "side": "sell", "quantity": 1, "bot_id": 1})
            self.assertEqual(rejected["status"], "rejected")

        snapshot = account_snapshot()
        bot = next(item for item in snapshot["bot_performance"] if item["id"] == 1)
        self.assertEqual(bot["circuit_breaker"]["status"], "tripped")
        self.assertEqual(bot["circuit_breaker"]["rejections_in_window"], 2)
        self.assertEqual(bot["runtime"]["status"], "paused")
        self.assertIn("rejection_burst", bot["runtime"]["reason"])

        blocked = place_market_order({"symbol": "BTCUSDT", "side": "buy", "quantity": 0.001, "bot_id": 1})
        self.assertEqual(blocked["status"], "rejected")
        runtime_check = next(check for check in blocked["risk"]["checks"] if check["code"] == "bot_runtime")
        self.assertFalse(runtime_check["passed"])
        self.assertEqual(snapshot["bot_circuit_events"][0]["trigger_code"], "rejection_burst")
        self.assertEqual(len(get_dataset_preview("paper_bot_circuit_breakers", 10)["rows"]), 1)
        self.assertEqual(len(get_dataset_preview("paper_bot_circuit_events", 10)["rows"]), 1)
        set_bot_runtime_state(1, "active", "Operator reviewed the rejection burst")
        rearmed = account_snapshot()
        bot = next(item for item in rearmed["bot_performance"] if item["id"] == 1)
        self.assertEqual(bot["runtime"]["status"], "active")
        self.assertEqual(bot["circuit_breaker"]["status"], "armed")
        self.assertEqual(len(rearmed["bot_circuit_events"]), 1)

    def test_bot_drawdown_curve_is_persisted_and_trips_breaker(self) -> None:
        set_bot_circuit_breaker(1, {
            "enabled": True,
            "max_consecutive_losses": 10,
            "max_rejections": 50,
            "rejection_window_minutes": 15,
            "max_drawdown_pct": 10,
            "pause_minutes": 45,
        })
        with connect() as connection:
            baseline = persist_bot_equity_snapshot(connection, 1, 0, 0, 1000)
            self.assertEqual(baseline["drawdown_pct"], 0)
            declined = persist_bot_equity_snapshot(connection, 1, -120, 0, 1000)
            decision = evaluate_bot_circuit_breaker(connection, 1, declined)
            self.assertEqual(decision["status"], "tripped")
            self.assertAlmostEqual(decision["drawdown_pct"], 12)
            self.assertEqual(decision["runtime"]["status"], "paused")
        rows = get_dataset_preview("paper_bot_equity_snapshots", 10)["rows"]
        comparable_rows = [row for row in rows if float(row["capital_basis"]) == 1000]
        self.assertGreaterEqual(len(comparable_rows), 2)
        self.assertTrue(any(abs(float(row["drawdown_pct"]) - 12) < 0.0001 for row in comparable_rows))
        events = get_dataset_preview("paper_bot_circuit_events", 10)["rows"]
        self.assertEqual(events[0]["trigger_code"], "max_drawdown")


if __name__ == "__main__":
    unittest.main()
