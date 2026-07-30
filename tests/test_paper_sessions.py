from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.app.services.data_center_service import get_dataset_preview
from backend.app.storage import sqlite as storage_sqlite
from backend.app.storage.bots import create_bot
from backend.app.storage.paper_sessions import change_paper_bot_session, list_paper_bot_sessions, start_paper_bot_session
from backend.app.storage.sqlite import initialize_database


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


if __name__ == "__main__":
    unittest.main()
