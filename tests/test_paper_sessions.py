from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app.services.data_center_service import get_dataset_preview
from backend.app.storage import sqlite as storage_sqlite
from backend.app.storage.bots import create_bot
from backend.app.storage.paper_sessions import change_paper_bot_session, list_paper_bot_sessions, run_paper_bot_session, start_paper_bot_session
from backend.app.storage.sqlite import connect, initialize_database


class PaperBotSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = storage_sqlite.DB_PATH
        storage_sqlite.DB_PATH = Path(self.temp_dir.name) / "paper-sessions.db"
        initialize_database()
        self.bot = create_bot({"name": "Session Bot", "base_symbol": "BTCUSDT", "timeframe": "15m"})

    def tearDown(self) -> None:
        storage_sqlite.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_session_lifecycle_is_singleton_and_auditable(self) -> None:
        bot_id = self.bot["bot"]["id"]
        version_id = self.bot["versions"][0]["id"]
        session = start_paper_bot_session(bot_id, version_id, cadence_seconds=300, execution_mode="observe")
        self.assertEqual(session["status"], "running")
        self.assertEqual(session["execution_mode"], "observe")
        with self.assertRaisesRegex(ValueError, "already has active"):
            start_paper_bot_session(bot_id, version_id)

        paused = change_paper_bot_session(session["id"], "pause", "Operator inspection")
        self.assertEqual(paused["status"], "paused")
        resumed = change_paper_bot_session(session["id"], "resume", "Inspection completed")
        self.assertEqual(resumed["status"], "running")
        stopped = change_paper_bot_session(session["id"], "stop", "Session window completed")
        self.assertEqual(stopped["status"], "stopped")
        self.assertIsNotNone(stopped["stopped_at"])

        archive = list_paper_bot_sessions(bot_id)
        self.assertEqual(archive["count"], 1)
        self.assertEqual([event["event_type"] for event in archive["events"]], ["stopped", "resumed", "paused", "started"])
        self.assertEqual(len(get_dataset_preview("paper_bot_sessions", 10)["rows"]), 1)
        self.assertEqual(len(get_dataset_preview("paper_bot_session_events", 10)["rows"]), 4)

    def test_session_run_is_idempotent_for_a_scheduled_slot(self) -> None:
        bot_id = self.bot["bot"]["id"]
        version = self.bot["versions"][0]
        session = start_paper_bot_session(bot_id, version["id"], cadence_seconds=300, execution_mode="observe")
        with connect() as connection:
            evaluation_id = connection.execute(
                """INSERT INTO strategy_signal_evaluations
                (bot_id, bot_version_id, strategy_hash, symbol, timeframe, feature_timestamp,
                 evaluation_key, signal, entry_passed, exit_passed, conflict, features_json,
                 trace_json, evaluated_at)
                VALUES (?, ?, ?, 'BTCUSDT', '15m', 1, 'session-fixture', 'hold', 0, 0, 0, '{}', '{}', ?)""",
                (bot_id, version["id"], version["strategy_hash"], session["started_at"]),
            ).lastrowid
        with patch("backend.app.services.bot_service.evaluate_saved_bot_signal", return_value={"id": evaluation_id, "signal": "hold"}):
            first = run_paper_bot_session(session["id"], session["next_run_at"])
            repeated = run_paper_bot_session(session["id"], session["next_run_at"])
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["signal"], "hold")
        self.assertTrue(repeated["recovered"])
        self.assertEqual(first["id"], repeated["id"])
        self.assertEqual(len(get_dataset_preview("paper_bot_session_runs", 10)["rows"]), 1)


if __name__ == "__main__":
    unittest.main()
