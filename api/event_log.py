"""Structured SQLite event logging for Mission Control API/scheduler."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH_ENV = "EVENT_LOG_DB_PATH"
FALLBACK_ENV_PATHS = ("AI_USAGE_DB_PATH", "TASK_RUN_HISTORY_DB_PATH")
DEFAULT_DB_FILENAME = "task_run_history.sqlite3"


def get_event_log_db_path() -> Path:
    raw_path = os.getenv(DB_PATH_ENV)
    if not raw_path:
        for env_name in FALLBACK_ENV_PATHS:
            raw_path = os.getenv(env_name)
            if raw_path:
                break
    if not raw_path:
        raw_path = DEFAULT_DB_FILENAME

    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _to_iso(ts: datetime | None = None) -> str:
    value = ts or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _serialize_metadata(metadata_json: Any) -> str | None:
    if metadata_json is None:
        return None
    return json.dumps(metadata_json, ensure_ascii=True, separators=(",", ":"), default=str)


def _deserialize_metadata(raw: str | None) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(get_event_log_db_path(), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            source TEXT NOT NULL,
            level TEXT NOT NULL,
            message TEXT NOT NULL,
            metadata_json TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_events_created_at ON events(created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_events_event_type ON events(event_type)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_events_source ON events(source)")
    conn.commit()


def log_event(
    *,
    event_type: str,
    source: str,
    level: str,
    message: str,
    metadata_json: Any = None,
    created_at: datetime | None = None,
) -> str:
    event_id = str(uuid.uuid4())
    with _connect() as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO events (
                id, event_type, source, level, message, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                event_type,
                source,
                level,
                message,
                _serialize_metadata(metadata_json),
                _to_iso(created_at),
            ),
        )
        conn.commit()
    return event_id


def list_recent_events(limit: int = 100) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 1000))
    with _connect() as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT id, event_type, source, level, message, metadata_json, created_at
            FROM events
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()

    results: list[dict[str, Any]] = []
    for row in rows:
        results.append(
            {
                "id": row["id"],
                "event_type": row["event_type"],
                "source": row["source"],
                "level": row["level"],
                "message": row["message"],
                "metadata_json": _deserialize_metadata(row["metadata_json"]),
                "created_at": row["created_at"],
            }
        )
    return results


def list_events_in_window(
    start: datetime,
    end: datetime,
    *,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 20000))
    with _connect() as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT id, event_type, source, level, message, metadata_json, created_at
            FROM events
            WHERE created_at >= ? AND created_at < ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (_to_iso(start), _to_iso(end), safe_limit),
        ).fetchall()

    results: list[dict[str, Any]] = []
    for row in rows:
        results.append(
            {
                "id": row["id"],
                "event_type": row["event_type"],
                "source": row["source"],
                "level": row["level"],
                "message": row["message"],
                "metadata_json": _deserialize_metadata(row["metadata_json"]),
                "created_at": row["created_at"],
            }
        )
    return results
