"""SQLite-backed task run history for worker execution auditing."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB_FILENAME = "task_run_history.sqlite3"
DB_PATH_ENV = "TASK_RUN_HISTORY_DB_PATH"
logger = logging.getLogger(__name__)


def get_task_run_history_db_path() -> Path:
    raw_path = os.getenv(DB_PATH_ENV, DEFAULT_DB_FILENAME).strip() or DEFAULT_DB_FILENAME
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _utc_iso(ts: datetime | None = None) -> str:
    value = ts or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _to_json_text(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), default=str)


def _from_json_text(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "task_name": row["task_name"],
        "status": row["status"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "duration_ms": row["duration_ms"],
        "input_json": _from_json_text(row["input_json"]),
        "output_json": _from_json_text(row["output_json"]),
        "error_text": row["error_text"],
        "worker_name": row["worker_name"],
        "created_at": row["created_at"],
    }


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(get_task_run_history_db_path(), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _is_malformed_db_error(exc: Exception) -> bool:
    return "database disk image is malformed" in str(exc).lower()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_runs (
            id TEXT PRIMARY KEY,
            task_name TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending','running','succeeded','failed')),
            started_at TEXT,
            ended_at TEXT,
            duration_ms INTEGER,
            input_json TEXT,
            output_json TEXT,
            error_text TEXT,
            worker_name TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_task_runs_created_at ON task_runs(created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_task_runs_status ON task_runs(status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_task_runs_task_name ON task_runs(task_name)"
    )
    conn.commit()


def create_task_run(
    task_name: str,
    *,
    input_json: Any = None,
    worker_name: str | None = None,
    status: str = "running",
    started_at: datetime | None = None,
) -> str:
    task_run_id = str(uuid.uuid4())
    started = _utc_iso(started_at)
    created = _utc_iso()
    with _connect() as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO task_runs (
                id, task_name, status, started_at, ended_at, duration_ms,
                input_json, output_json, error_text, worker_name, created_at
            ) VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL, NULL, ?, ?)
            """,
            (
                task_run_id,
                task_name,
                status,
                started,
                _to_json_text(input_json),
                worker_name,
                created,
            ),
        )
        conn.commit()
    return task_run_id


def complete_task_run(
    task_run_id: str,
    *,
    output_json: Any = None,
    duration_ms: int | None = None,
    ended_at: datetime | None = None,
) -> None:
    with _connect() as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            UPDATE task_runs
            SET status = ?, ended_at = ?, duration_ms = ?, output_json = ?, error_text = NULL
            WHERE id = ?
            """,
            (
                "succeeded",
                _utc_iso(ended_at),
                duration_ms,
                _to_json_text(output_json),
                task_run_id,
            ),
        )
        conn.commit()


def fail_task_run(
    task_run_id: str,
    *,
    error_text: str,
    output_json: Any = None,
    duration_ms: int | None = None,
    ended_at: datetime | None = None,
) -> None:
    with _connect() as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            UPDATE task_runs
            SET status = ?, ended_at = ?, duration_ms = ?, output_json = ?, error_text = ?
            WHERE id = ?
            """,
            (
                "failed",
                _utc_iso(ended_at),
                duration_ms,
                _to_json_text(output_json),
                error_text,
                task_run_id,
            ),
        )
        conn.commit()


def list_recent_task_runs(limit: int = 50) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 500))
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT
                    id, task_name, status, started_at, ended_at, duration_ms,
                    input_json, output_json, error_text, worker_name, created_at
                FROM task_runs
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
    except sqlite3.DatabaseError as exc:
        if not _is_malformed_db_error(exc):
            raise
        logger.warning(
            "task_run_history_list_recent_failed_malformed_db",
            extra={"limit": safe_limit, "error": str(exc)},
        )
        return []
    return [_row_to_dict(row) for row in rows]


def get_task_run(task_run_id: str) -> dict[str, Any] | None:
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT
                    id, task_name, status, started_at, ended_at, duration_ms,
                    input_json, output_json, error_text, worker_name, created_at
                FROM task_runs
                WHERE id = ?
                LIMIT 1
                """,
                (task_run_id,),
            ).fetchone()
    except sqlite3.DatabaseError as exc:
        if not _is_malformed_db_error(exc):
            raise
        logger.warning(
            "task_run_history_get_failed_malformed_db",
            extra={"task_run_id": task_run_id, "error": str(exc)},
        )
        return None
    if row is None:
        return None
    return _row_to_dict(row)


def list_task_runs_in_window(
    start: datetime,
    end: datetime,
    *,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 20000))
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT
                    id, task_name, status, started_at, ended_at, duration_ms,
                    input_json, output_json, error_text, worker_name, created_at
                FROM task_runs
                WHERE created_at >= ? AND created_at < ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (_utc_iso(start), _utc_iso(end), safe_limit),
            ).fetchall()
    except sqlite3.DatabaseError as exc:
        if not _is_malformed_db_error(exc):
            raise
        logger.warning(
            "task_run_history_window_failed_malformed_db",
            extra={"start": _utc_iso(start), "end": _utc_iso(end), "limit": safe_limit, "error": str(exc)},
        )
        return []
    return [_row_to_dict(row) for row in rows]
