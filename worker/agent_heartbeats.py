"""SQLite-backed agent heartbeat tracking for worker heartbeat writes."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

DB_PATH_ENV = "AGENT_HEARTBEAT_DB_PATH"
FALLBACK_ENV_PATHS = (
    "EVENT_LOG_DB_PATH",
    "AI_USAGE_DB_PATH",
    "TASK_RUN_HISTORY_DB_PATH",
)
DEFAULT_DB_FILENAME = "task_run_history.sqlite3"


def get_agent_heartbeat_db_path() -> Path:
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
    conn = sqlite3.connect(get_agent_heartbeat_db_path(), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_heartbeats (
            agent_name TEXT PRIMARY KEY,
            last_seen_at TEXT NOT NULL,
            status TEXT NOT NULL,
            metadata_json TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_heartbeats_last_seen_at ON agent_heartbeats(last_seen_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_heartbeats_status ON agent_heartbeats(status)"
    )
    conn.commit()


def upsert_agent_heartbeat(
    *,
    agent_name: str,
    status: str,
    metadata_json: Any = None,
    last_seen_at: datetime | None = None,
) -> None:
    with _connect() as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO agent_heartbeats (agent_name, last_seen_at, status, metadata_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(agent_name)
            DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                status = excluded.status,
                metadata_json = excluded.metadata_json
            """,
            (
                agent_name,
                _to_iso(last_seen_at),
                status,
                _serialize_metadata(metadata_json),
            ),
        )
        conn.commit()


def set_agent_heartbeat_status(
    *,
    agent_name: str,
    status: str,
    metadata_json: Any = None,
) -> bool:
    with _connect() as conn:
        _ensure_schema(conn)
        merged_metadata = metadata_json
        if isinstance(metadata_json, dict):
            existing_row = conn.execute(
                """
                SELECT metadata_json
                FROM agent_heartbeats
                WHERE agent_name = ?
                LIMIT 1
                """,
                (agent_name,),
            ).fetchone()
            if existing_row is not None:
                existing_metadata = _deserialize_metadata(existing_row["metadata_json"])
                if isinstance(existing_metadata, dict):
                    current = dict(existing_metadata)
                    current.update(metadata_json)
                    merged_metadata = current
        cursor = conn.execute(
            """
            UPDATE agent_heartbeats
            SET status = ?, metadata_json = ?
            WHERE agent_name = ?
            """,
            (status, _serialize_metadata(merged_metadata), agent_name),
        )
        conn.commit()
    return cursor.rowcount > 0


def get_agent_heartbeat(agent_name: str) -> dict[str, Any] | None:
    with _connect() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            """
            SELECT agent_name, last_seen_at, status, metadata_json
            FROM agent_heartbeats
            WHERE agent_name = ?
            LIMIT 1
            """,
            (agent_name,),
        ).fetchone()

    if row is None:
        return None
    return {
        "agent_name": row["agent_name"],
        "last_seen_at": row["last_seen_at"],
        "status": row["status"],
        "metadata_json": _deserialize_metadata(row["metadata_json"]),
    }


def list_recent_agent_heartbeats(limit: int = 100) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 1000))
    with _connect() as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT agent_name, last_seen_at, status, metadata_json
            FROM agent_heartbeats
            ORDER BY last_seen_at DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()

    result: list[dict[str, Any]] = []
    for row in rows:
        result.append(
            {
                "agent_name": row["agent_name"],
                "last_seen_at": row["last_seen_at"],
                "status": row["status"],
                "metadata_json": _deserialize_metadata(row["metadata_json"]),
            }
        )
    return result


def list_stale_agent_heartbeats(
    *,
    stale_after_seconds: int,
    now: datetime | None = None,
    limit: int = 1000,
    agent_names: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 5000))
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=max(int(stale_after_seconds), 1))
    cutoff_iso = _to_iso(cutoff)
    tracked_names = {str(name).strip() for name in (agent_names or []) if str(name).strip()}
    if tracked_names:
        safe_limit = max(safe_limit, len(tracked_names))

    with _connect() as conn:
        _ensure_schema(conn)
        if tracked_names:
            placeholders = ",".join(["?"] * len(tracked_names))
            sql = (
                """
                SELECT agent_name, last_seen_at, status, metadata_json
                FROM agent_heartbeats
                WHERE last_seen_at < ? AND agent_name IN (
                """
                + placeholders
                + """
                )
                ORDER BY last_seen_at ASC
                LIMIT ?
                """
            )
            args: list[Any] = [cutoff_iso]
            args.extend(sorted(tracked_names))
            args.append(safe_limit)
            rows = conn.execute(sql, tuple(args)).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT agent_name, last_seen_at, status, metadata_json
                FROM agent_heartbeats
                WHERE last_seen_at < ?
                ORDER BY last_seen_at ASC
                LIMIT ?
                """,
                (cutoff_iso, safe_limit),
            ).fetchall()

    result: list[dict[str, Any]] = []
    for row in rows:
        result.append(
            {
                "agent_name": row["agent_name"],
                "last_seen_at": row["last_seen_at"],
                "status": row["status"],
                "metadata_json": _deserialize_metadata(row["metadata_json"]),
            }
        )
    return result


def delete_old_agent_heartbeats(
    *,
    older_than_seconds: int,
    now: datetime | None = None,
    keep_agent_names: set[str] | None = None,
) -> int:
    safe_seconds = max(int(older_than_seconds), 1)
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=safe_seconds)
    cutoff_iso = _to_iso(cutoff)

    with _connect() as conn:
        _ensure_schema(conn)
        keep = {name.strip() for name in (keep_agent_names or set()) if str(name).strip()}
        if keep:
            placeholders = ",".join(["?"] * len(keep))
            sql = (
                "DELETE FROM agent_heartbeats "
                f"WHERE last_seen_at < ? AND agent_name NOT IN ({placeholders})"
            )
            args: list[Any] = [cutoff_iso]
            args.extend(sorted(keep))
            cursor = conn.execute(sql, tuple(args))
        else:
            cursor = conn.execute(
                "DELETE FROM agent_heartbeats WHERE last_seen_at < ?",
                (cutoff_iso,),
            )
        conn.commit()
        return int(cursor.rowcount or 0)
