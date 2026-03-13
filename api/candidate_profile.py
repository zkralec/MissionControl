"""SQLite-backed candidate resume profile storage for API routes."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH_ENV = "CANDIDATE_PROFILE_DB_PATH"
FALLBACK_ENV_PATHS = (
    "TASK_RUN_HISTORY_DB_PATH",
    "AI_USAGE_DB_PATH",
    "EVENT_LOG_DB_PATH",
)
DEFAULT_DB_FILENAME = "task_run_history.sqlite3"
_MAX_RESUME_CHARS = 500_000
_PREVIEW_CHARS = 320


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


def get_candidate_profile_db_path() -> Path:
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


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(get_candidate_profile_db_path(), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS candidate_resume_profile (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            resume_name TEXT,
            resume_text TEXT NOT NULL,
            resume_sha256 TEXT NOT NULL,
            metadata_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _normalize_resume_text(resume_text: str) -> str:
    normalized = resume_text.replace("\r\n", "\n").strip()
    if not normalized:
        raise ValueError("resume_text cannot be empty")
    if len(normalized) > _MAX_RESUME_CHARS:
        raise ValueError(f"resume_text exceeds max length ({_MAX_RESUME_CHARS} chars)")
    return normalized


def _normalize_resume_name(resume_name: str | None) -> str | None:
    if resume_name is None:
        return None
    trimmed = resume_name.strip()
    if not trimmed:
        return None
    return trimmed[:255]


def _row_to_dict(row: sqlite3.Row, *, include_text: bool) -> dict[str, Any]:
    resume_text = row["resume_text"] if isinstance(row["resume_text"], str) else ""
    output: dict[str, Any] = {
        "resume_name": row["resume_name"],
        "resume_sha256": row["resume_sha256"],
        "resume_char_count": len(resume_text),
        "resume_preview": resume_text[:_PREVIEW_CHARS],
        "metadata_json": _from_json_text(row["metadata_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if include_text:
        output["resume_text"] = resume_text
    return output


def upsert_resume_profile(
    *,
    resume_text: str,
    resume_name: str | None = None,
    metadata_json: Any = None,
) -> dict[str, Any]:
    if not isinstance(resume_text, str):
        raise ValueError("resume_text must be a string")

    normalized_text = _normalize_resume_text(resume_text)
    normalized_name = _normalize_resume_name(resume_name)
    now = _utc_iso()
    resume_sha = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()

    with _connect() as conn:
        _ensure_schema(conn)
        existing = conn.execute(
            "SELECT created_at FROM candidate_resume_profile WHERE id = 1"
        ).fetchone()
        created_at = existing["created_at"] if existing is not None else now
        conn.execute(
            """
            INSERT INTO candidate_resume_profile (
                id, resume_name, resume_text, resume_sha256, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                resume_name = excluded.resume_name,
                resume_text = excluded.resume_text,
                resume_sha256 = excluded.resume_sha256,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                1,
                normalized_name,
                normalized_text,
                resume_sha,
                _to_json_text(metadata_json),
                created_at,
                now,
            ),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT resume_name, resume_text, resume_sha256, metadata_json, created_at, updated_at
            FROM candidate_resume_profile
            WHERE id = 1
            """
        ).fetchone()
    if row is None:
        raise RuntimeError("Failed to persist resume profile")
    return _row_to_dict(row, include_text=False)


def get_resume_profile(*, include_text: bool = False) -> dict[str, Any] | None:
    with _connect() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            """
            SELECT resume_name, resume_text, resume_sha256, metadata_json, created_at, updated_at
            FROM candidate_resume_profile
            WHERE id = 1
            """
        ).fetchone()
    if row is None:
        return None
    return _row_to_dict(row, include_text=include_text)


def delete_resume_profile() -> bool:
    with _connect() as conn:
        _ensure_schema(conn)
        cursor = conn.execute("DELETE FROM candidate_resume_profile WHERE id = 1")
        conn.commit()
    return int(cursor.rowcount or 0) > 0
