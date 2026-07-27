from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from backend.app.services.data_center_service import get_dataset_preview
from backend.app.storage import sqlite as storage_sqlite
from backend.app.storage.paper import account_snapshot, place_market_order
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


if __name__ == "__main__":
    unittest.main()
