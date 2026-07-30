from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

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
        runs = connection.execute(
            "SELECT * FROM paper_bot_session_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return {"sessions": [session_row(row) for row in rows], "runs": [dict(row) for row in runs], "events": [dict(row) for row in events], "count": len(rows), "live_execution": "blocked"}


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


def next_schedule(scheduled_for: str, cadence_seconds: int, now: datetime) -> str:
    candidate = datetime.fromisoformat(str(scheduled_for).replace("Z", "+00:00"))
    if candidate.tzinfo is None:
        candidate = candidate.replace(tzinfo=timezone.utc)
    step = timedelta(seconds=int(cadence_seconds))
    candidate += step
    while candidate <= now:
        candidate += step
    return candidate.isoformat()


def finish_session_run(run_id: int, session: dict, status: str, result: dict, evaluation_id: int | None = None, proposal_id: int | None = None, order_id: int | None = None) -> dict:
    now = utc_now()
    next_run_at = next_schedule(session["next_run_at"], int(session["cadence_seconds"]), now)
    with connect() as connection:
        connection.execute(
            """UPDATE paper_bot_session_runs SET status = ?, signal = ?, signal_evaluation_id = ?,
               proposal_id = ?, simulated_order_id = ?, result_json = ?, completed_at = ?
               WHERE id = ? AND status = 'running'""",
            (status, result.get("signal"), evaluation_id, proposal_id, order_id, json.dumps(result, sort_keys=True), now.isoformat(), run_id),
        )
        connection.execute(
            """UPDATE paper_bot_sessions SET next_run_at = ?, last_run_at = ?,
               last_signal_evaluation_id = ?, last_proposal_id = ?, last_error = ?,
               updated_at = ? WHERE id = ?""",
            (next_run_at, now.isoformat(), evaluation_id, proposal_id, result.get("reason") if status == "skipped" else None, now.isoformat(), session["id"]),
        )
        row = connection.execute("SELECT * FROM paper_bot_session_runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row)


def fail_session_run(run_id: int, session: dict, error: str) -> dict:
    now = utc_now()
    with connect() as connection:
        connection.execute(
            """UPDATE paper_bot_session_runs SET status = 'error', result_json = ?, completed_at = ?
            WHERE id = ? AND status = 'running'""",
            (json.dumps({"error": error}, sort_keys=True), now.isoformat(), run_id),
        )
        connection.execute(
            """UPDATE paper_bot_sessions SET status = 'error', last_run_at = ?,
               last_error = ?, updated_at = ? WHERE id = ?""",
            (now.isoformat(), error, now.isoformat(), session["id"]),
        )
        add_event(connection, session["id"], session["bot_id"], "error", error, "running", "error", {"run_id": run_id})
        row = connection.execute("SELECT * FROM paper_bot_session_runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row)


def run_paper_bot_session(session_id: int, scheduled_for: str | None = None) -> dict:
    initialize_database()
    now = utc_now()
    with connect() as connection:
        row = connection.execute("SELECT * FROM paper_bot_sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            raise ValueError("Paper bot session not found")
        session = dict(row)
        if session["status"] != "running":
            raise ValueError("Only a running paper bot session can execute")
        slot = scheduled_for or session["next_run_at"]
        cursor = connection.execute(
            """INSERT OR IGNORE INTO paper_bot_session_runs
            (session_id, bot_id, bot_version_id, scheduled_for, status, result_json, started_at)
            VALUES (?, ?, ?, ?, 'running', '{}', ?)""",
            (session["id"], session["bot_id"], session["bot_version_id"], slot, now.isoformat()),
        )
        if cursor.rowcount != 1:
            existing = connection.execute(
                "SELECT * FROM paper_bot_session_runs WHERE session_id = ? AND scheduled_for = ?",
                (session["id"], slot),
            ).fetchone()
            return {**dict(existing), "recovered": True}
        run_id = int(cursor.lastrowid)

    try:
        from backend.app.services.bot_service import (
            create_saved_bot_paper_proposal,
            evaluate_saved_bot_signal,
            submit_saved_bot_paper_proposal,
        )
        from backend.app.storage.paper import bot_runtime_state

        with connect() as connection:
            runtime = bot_runtime_state(connection, int(session["bot_id"]))
        if runtime["entry_blocked"]:
            reason = f"Bot runtime blocked: {runtime['reason']}"
            with connect() as connection:
                connection.execute(
                    "UPDATE paper_bot_sessions SET status = 'blocked', last_error = ?, updated_at = ? WHERE id = ?",
                    (reason, utc_now().isoformat(), session["id"]),
                )
                add_event(connection, session["id"], session["bot_id"], "blocked", reason, "running", "blocked", {"run_id": run_id})
            return finish_session_run(run_id, session, "skipped", {"signal": "blocked", "reason": reason})

        evaluation = evaluate_saved_bot_signal(int(session["bot_id"]), int(session["bot_version_id"]))
        signal = evaluation["signal"]
        result = {"signal": signal, "evaluation_id": evaluation["id"], "execution_mode": session["execution_mode"], "live_execution": "blocked"}
        proposal = None
        paper_result = None
        if signal in {"entry_candidate", "exit_candidate"}:
            proposal = create_saved_bot_paper_proposal(int(session["bot_id"]), int(evaluation["id"]))
            result["proposal_id"] = proposal["id"]
            if session["execution_mode"] == "auto_paper":
                submitted = submit_saved_bot_paper_proposal(int(session["bot_id"]), int(proposal["id"]))
                paper_result = submitted["paper_result"]
                result["paper_status"] = paper_result.get("status")
                result["simulated_order_id"] = paper_result.get("order_id")
        return finish_session_run(
            run_id, session, "completed", result, int(evaluation["id"]),
            int(proposal["id"]) if proposal else None,
            int(paper_result["order_id"]) if paper_result and paper_result.get("order_id") else None,
        )
    except Exception as exc:
        return fail_session_run(run_id, session, str(exc))


def run_due_paper_bot_sessions(limit: int = 20) -> dict:
    initialize_database()
    now = utc_now().isoformat()
    with connect() as connection:
        due = [
            dict(row) for row in connection.execute(
                """SELECT * FROM paper_bot_sessions
                WHERE status = 'running' AND next_run_at <= ?
                ORDER BY next_run_at, id LIMIT ?""",
                (now, max(1, min(int(limit), 100))),
            ).fetchall()
        ]
    results = [run_paper_bot_session(session["id"], session["next_run_at"]) for session in due]
    return {
        "due": len(due),
        "completed": sum(item["status"] == "completed" for item in results),
        "skipped": sum(item["status"] == "skipped" for item in results),
        "errors": sum(item["status"] == "error" for item in results),
        "runs": results,
        "execution_environment": "paper",
        "live_execution": "blocked",
    }
