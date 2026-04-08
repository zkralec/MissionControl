"""SQLite-backed state for durable application draft identity checks."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


DB_PATH_ENV = "APPLICATION_DRAFT_STATE_DB_PATH"
FALLBACK_ENV_PATHS = (
    "TASK_RUN_HISTORY_DB_PATH",
    "AI_USAGE_DB_PATH",
    "EVENT_LOG_DB_PATH",
)
DEFAULT_DB_FILENAME = "task_run_history.sqlite3"


def get_application_draft_state_db_path() -> Path:
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
    conn = sqlite3.connect(get_application_draft_state_db_path(), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS application_draft_state (
            identity_key TEXT PRIMARY KEY,
            application_url TEXT NOT NULL,
            company TEXT NOT NULL,
            job_id TEXT,
            last_task_id TEXT,
            last_run_id TEXT,
            last_pipeline_id TEXT,
            draft_status TEXT,
            source_status TEXT,
            review_status TEXT,
            awaiting_review INTEGER NOT NULL DEFAULT 0,
            submitted INTEGER NOT NULL DEFAULT 0,
            failure_category TEXT,
            blocking_reason TEXT,
            state_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_application_draft_state_updated_at ON application_draft_state(updated_at DESC)"
    )
    conn.commit()


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalize_url(raw_url: str | None) -> str:
    if not isinstance(raw_url, str):
        return ""
    value = raw_url.strip()
    if not value:
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return value.lower()
    query_items = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k and not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}
    ]
    return urlunsplit(
        (
            (parts.scheme or "https").lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/") or "/",
            urlencode(sorted(query_items)),
            "",
        )
    )


def build_application_identity(application_target: dict[str, Any]) -> dict[str, Any]:
    application_url = _normalize_url(
        str(application_target.get("application_url") or application_target.get("source_url") or "").strip()
    )
    company = str(application_target.get("company") or "").strip().lower()
    job_id = str(application_target.get("job_id") or "").strip().lower() or None
    if not application_url or not company:
        raise ValueError("Application draft identity requires both application_url and company.")
    base = f"{application_url}|{company}|{job_id or '-'}"
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:20]
    return {
        "identity_key": f"jobapply:draft:{digest}",
        "application_url": application_url,
        "company": company,
        "job_id": job_id,
    }


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    state_json = row["state_json"]
    parsed_state: Any = None
    if state_json:
        try:
            parsed_state = json.loads(state_json)
        except json.JSONDecodeError:
            parsed_state = None
    return {
        "identity_key": row["identity_key"],
        "application_url": row["application_url"],
        "company": row["company"],
        "job_id": row["job_id"],
        "last_task_id": row["last_task_id"],
        "last_run_id": row["last_run_id"],
        "last_pipeline_id": row["last_pipeline_id"],
        "draft_status": row["draft_status"],
        "source_status": row["source_status"],
        "review_status": row["review_status"],
        "awaiting_review": bool(row["awaiting_review"]),
        "submitted": bool(row["submitted"]),
        "failure_category": row["failure_category"],
        "blocking_reason": row["blocking_reason"],
        "state_json": parsed_state,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_application_draft_state(identity_key: str) -> dict[str, Any] | None:
    with _connect() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            """
            SELECT identity_key, application_url, company, job_id, last_task_id, last_run_id,
                   last_pipeline_id, draft_status, source_status, review_status, awaiting_review,
                   submitted, failure_category, blocking_reason, state_json, created_at, updated_at
            FROM application_draft_state
            WHERE identity_key = ?
            LIMIT 1
            """,
            (identity_key,),
        ).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def claim_application_draft_identity(
    identity: dict[str, Any],
    *,
    task_id: str,
    run_id: str,
    pipeline_id: str,
    force: bool = False,
) -> tuple[bool, dict[str, Any] | None]:
    identity_key = str(identity.get("identity_key") or "").strip()
    if not identity_key:
        raise ValueError("identity_key is required")
    now = _utc_iso()
    with _connect() as conn:
        _ensure_schema(conn)
        existing = conn.execute(
            """
            SELECT identity_key, application_url, company, job_id, last_task_id, last_run_id,
                   last_pipeline_id, draft_status, source_status, review_status, awaiting_review,
                   submitted, failure_category, blocking_reason, state_json, created_at, updated_at
            FROM application_draft_state
            WHERE identity_key = ?
            LIMIT 1
            """,
            (identity_key,),
        ).fetchone()
        if existing is not None and not force:
            return False, _row_to_dict(existing)
        created_at = existing["created_at"] if existing is not None else now
        conn.execute(
            """
            INSERT INTO application_draft_state(
                identity_key, application_url, company, job_id, last_task_id, last_run_id,
                last_pipeline_id, draft_status, source_status, review_status, awaiting_review,
                submitted, failure_category, blocking_reason, state_json, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(identity_key) DO UPDATE SET
                application_url=excluded.application_url,
                company=excluded.company,
                job_id=excluded.job_id,
                last_task_id=excluded.last_task_id,
                last_run_id=excluded.last_run_id,
                last_pipeline_id=excluded.last_pipeline_id,
                draft_status=excluded.draft_status,
                source_status=excluded.source_status,
                review_status=excluded.review_status,
                awaiting_review=excluded.awaiting_review,
                submitted=excluded.submitted,
                failure_category=excluded.failure_category,
                blocking_reason=excluded.blocking_reason,
                state_json=excluded.state_json,
                updated_at=excluded.updated_at
            """,
            (
                identity_key,
                str(identity.get("application_url") or ""),
                str(identity.get("company") or ""),
                identity.get("job_id"),
                task_id,
                run_id,
                pipeline_id,
                "in_progress",
                "in_progress",
                "blocked",
                0,
                0,
                None,
                None,
                json.dumps({"phase": "claimed"}, ensure_ascii=True),
                created_at,
                now,
            ),
        )
        conn.commit()
    return True, None


def record_application_draft_result(
    identity: dict[str, Any],
    *,
    task_id: str,
    run_id: str,
    pipeline_id: str,
    draft_status: str,
    source_status: str,
    review_status: str,
    awaiting_review: bool,
    submitted: bool,
    failure_category: str | None,
    blocking_reason: str | None,
    state_json: dict[str, Any] | None = None,
) -> None:
    identity_key = str(identity.get("identity_key") or "").strip()
    if not identity_key:
        raise ValueError("identity_key is required")
    now = _utc_iso()
    with _connect() as conn:
        _ensure_schema(conn)
        existing = conn.execute(
            "SELECT created_at FROM application_draft_state WHERE identity_key = ? LIMIT 1",
            (identity_key,),
        ).fetchone()
        created_at = existing["created_at"] if existing is not None else now
        conn.execute(
            """
            INSERT INTO application_draft_state(
                identity_key, application_url, company, job_id, last_task_id, last_run_id,
                last_pipeline_id, draft_status, source_status, review_status, awaiting_review,
                submitted, failure_category, blocking_reason, state_json, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(identity_key) DO UPDATE SET
                application_url=excluded.application_url,
                company=excluded.company,
                job_id=excluded.job_id,
                last_task_id=excluded.last_task_id,
                last_run_id=excluded.last_run_id,
                last_pipeline_id=excluded.last_pipeline_id,
                draft_status=excluded.draft_status,
                source_status=excluded.source_status,
                review_status=excluded.review_status,
                awaiting_review=excluded.awaiting_review,
                submitted=excluded.submitted,
                failure_category=excluded.failure_category,
                blocking_reason=excluded.blocking_reason,
                state_json=excluded.state_json,
                updated_at=excluded.updated_at
            """,
            (
                identity_key,
                str(identity.get("application_url") or ""),
                str(identity.get("company") or ""),
                identity.get("job_id"),
                task_id,
                run_id,
                pipeline_id,
                draft_status,
                source_status,
                review_status,
                1 if awaiting_review else 0,
                1 if submitted else 0,
                failure_category,
                blocking_reason,
                json.dumps(state_json or {}, ensure_ascii=True),
                created_at,
                now,
            ),
        )
        conn.commit()
