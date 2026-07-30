from __future__ import annotations

import json
from datetime import datetime, timezone

from backend.app.storage.sqlite import connect, initialize_database


ACTIVE_STATUSES = ("running", "paused", "blocked", "error")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def session_row(row) -> dict:
    return dict(row)


def add_event(connection, session_id: int, bot_id: int, event_type: str, reason: str, previous_status: str | None, new_status: str | None, payload: dict | None = None) -> None:
    connection.execute(
        """INSERT INTO paper_bot_session_events
        (session_id, bot_id, event_type, previous_status, new_status, reason, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (session_id, bot_id, event_type, previous_status, new_status, reason, json.dumps(payload or {}, sort_keys=True), utc_now().isoformat()),
    )


def start_paper_bot_session(bot_id: int, version_id: int | None = None, cadence_seconds: int = 300, execution_mode: str = "observe") -> dict:
    initialize_database()
    bot_id = int(bot_id)
    cadence_seconds = int(cadence_seconds)
    if not 60 <= cadence_seconds <= 86_400:
        raise ValueError("Session cadence must be between 60 and 86,400 seconds")
    if execution_mode not in {"observe", "auto_paper"}:
        raise ValueError("Session execution mode must be observe or auto_paper")
    now = utc_now()
    with connect() as connection:
        bot = connection.execute("SELECT id FROM bots WHERE id = ?", (bot_id,)).fetchone()
        if not bot:
            raise ValueError("Bot not found")
        version = connection.execute(
            """SELECT id, validation_status, contract_json FROM bot_versions
            WHERE bot_id = ? AND (? IS NULL OR id = ?)
            ORDER BY version DESC LIMIT 1""",
            (bot_id, version_id, version_id),
        ).fetchone()
        if not version:
            raise ValueError("Bot version not found")
        if version["validation_status"] != "valid":
            raise ValueError("Only a valid bot version can start a paper session")
        contract = json.loads(version["contract_json"] or "{}")
        if not (contract.get("capabilities") or {}).get("paper_proposal"):
            raise ValueError("Bot version does not support paper proposals")
        active = connection.execute(
            """SELECT id FROM paper_bot_sessions WHERE bot_id = ?
            AND status IN ('running', 'paused', 'blocked', 'error')""",
            (bot_id,),
        ).fetchone()
        if active:
            raise ValueError(f"Bot already has active paper session #{active['id']}")
        next_run = now.isoformat()
        cursor = connection.execute(
            """INSERT INTO paper_bot_sessions
            (bot_id, bot_version_id, status, execution_mode, cadence_seconds, next_run_at,
             started_at, created_at, updated_at)
            VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?)""",
            (bot_id, version["id"], execution_mode, cadence_seconds, next_run, next_run, next_run, next_run),
        )
        session_id = int(cursor.lastrowid)
        add_event(connection, session_id, bot_id, "started", "Paper bot session started", None, "running", {
            "bot_version_id": int(version["id"]),
            "cadence_seconds": cadence_seconds,
            "execution_mode": execution_mode,
            "live_execution": "blocked",
        })
        row = connection.execute("SELECT * FROM paper_bot_sessions WHERE id = ?", (session_id,)).fetchone()
    return session_row(row)


def list_paper_bot_sessions(bot_id: int | None = None, limit: int = 100) -> dict:
    initialize_database()
    with connect() as connection:
        if bot_id is None:
            rows = connection.execute(
                "SELECT * FROM paper_bot_sessions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM paper_bot_sessions WHERE bot_id = ? ORDER BY id DESC LIMIT ?",
                (bot_id, limit),
            ).fetchall()
        events = connection.execute(
            "SELECT * FROM paper_bot_session_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return {"sessions": [session_row(row) for row in rows], "events": [dict(row) for row in events], "count": len(rows), "live_execution": "blocked"}


def change_paper_bot_session(session_id: int, action: str, reason: str) -> dict:
    initialize_database()
    action = str(action).strip().lower()
    clean_reason = str(reason).strip()
    if action not in {"pause", "resume", "stop"}:
        raise ValueError("Session action must be pause, resume or stop")
    if len(clean_reason) < 3:
        raise ValueError("An auditable session reason is required")
    now = utc_now()
    with connect() as connection:
        row = connection.execute("SELECT * FROM paper_bot_sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            raise ValueError("Paper bot session not found")
        session = dict(row)
        previous = session["status"]
        if action == "pause":
            if previous != "running":
                raise ValueError("Only a running session can be paused")
            new_status, event_type, stopped_at, next_run_at = "paused", "paused", None, session["next_run_at"]
        elif action == "resume":
            if previous not in {"paused", "blocked", "error"}:
                raise ValueError("Only a paused, blocked or error session can be resumed")
            new_status, event_type, stopped_at, next_run_at = "running", "resumed", None, now.isoformat()
        else:
            if previous == "stopped":
                return session
            new_status, event_type, stopped_at, next_run_at = "stopped", "stopped", now.isoformat(), session["next_run_at"]
        connection.execute(
            """UPDATE paper_bot_sessions SET status = ?, next_run_at = ?, stopped_at = ?,
               last_error = NULL, updated_at = ? WHERE id = ?""",
            (new_status, next_run_at, stopped_at, now.isoformat(), session_id),
        )
        add_event(connection, session_id, int(session["bot_id"]), event_type, clean_reason, previous, new_status)
        updated = connection.execute("SELECT * FROM paper_bot_sessions WHERE id = ?", (session_id,)).fetchone()
    return session_row(updated)
