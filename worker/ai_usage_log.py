"""SQLite-backed AI usage logging and query helpers."""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from core.operational_day import current_operational_day_window_utc

DB_PATH_ENV = "AI_USAGE_DB_PATH"
FALLBACK_DB_PATH_ENV = "TASK_RUN_HISTORY_DB_PATH"
DEFAULT_DB_FILENAME = "task_run_history.sqlite3"


def get_ai_usage_db_path() -> Path:
    raw_path = (
        os.getenv(DB_PATH_ENV)
        or os.getenv(FALLBACK_DB_PATH_ENV)
        or DEFAULT_DB_FILENAME
    )
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(value: datetime | None = None) -> str:
    ts = value or _utc_now()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat()


def _to_float_or_none(value: Decimal | float | int | str | None) -> float | None:
    if value is None:
        return None
    return float(Decimal(str(value)))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(get_ai_usage_db_path(), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_usage (
            id TEXT PRIMARY KEY,
            task_run_id TEXT,
            agent_name TEXT NOT NULL,
            model TEXT NOT NULL,
            tokens_in INTEGER,
            tokens_out INTEGER,
            total_tokens INTEGER,
            cost_usd REAL,
            latency_ms INTEGER,
            status TEXT NOT NULL,
            error_text TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_ai_usage_created_at ON ai_usage(created_at DESC)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_ai_usage_model ON ai_usage(model)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_ai_usage_status ON ai_usage(status)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_ai_usage_task_run_id ON ai_usage(task_run_id)"
    )
    conn.commit()


def log_ai_usage(
    *,
    agent_name: str,
    model: str,
    task_run_id: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    total_tokens: int | None = None,
    cost_usd: Decimal | float | int | str | None = None,
    latency_ms: int | None = None,
    status: str = "succeeded",
    error_text: str | None = None,
    created_at: datetime | None = None,
) -> str:
    usage_id = str(uuid.uuid4())
    resolved_total_tokens = total_tokens
    if resolved_total_tokens is None and tokens_in is not None and tokens_out is not None:
        resolved_total_tokens = int(tokens_in) + int(tokens_out)

    with _connect() as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO ai_usage (
                id, task_run_id, agent_name, model,
                tokens_in, tokens_out, total_tokens,
                cost_usd, latency_ms, status, error_text, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                usage_id,
                task_run_id,
                agent_name,
                model,
                tokens_in,
                tokens_out,
                resolved_total_tokens,
                _to_float_or_none(cost_usd),
                latency_ms,
                status,
                error_text,
                _to_iso(created_at),
            ),
        )
        conn.commit()
    return usage_id


def _rows_between(start: datetime, end: datetime) -> list[dict[str, Any]]:
    with _connect() as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT
                id, task_run_id, agent_name, model, tokens_in, tokens_out,
                total_tokens, cost_usd, latency_ms, status, error_text, created_at
            FROM ai_usage
            WHERE created_at >= ? AND created_at < ?
            ORDER BY created_at DESC
            """,
            (_to_iso(start), _to_iso(end)),
        ).fetchall()
    return [dict(row) for row in rows]


def list_ai_usage_today() -> list[dict[str, Any]]:
    start, end = current_operational_day_window_utc(_utc_now())
    return _rows_between(start, end)


def get_ai_usage_summary(start: datetime, end: datetime) -> dict[str, Any]:
    with _connect() as conn:
        _ensure_schema(conn)
        totals = conn.execute(
            """
            SELECT
                COUNT(*) AS requests_total,
                SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded_total,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_total,
                SUM(tokens_in) AS tokens_in_total,
                SUM(tokens_out) AS tokens_out_total,
                SUM(total_tokens) AS total_tokens_sum,
                SUM(cost_usd) AS cost_usd_total,
                AVG(latency_ms) AS latency_ms_avg
            FROM ai_usage
            WHERE created_at >= ? AND created_at < ?
            """,
            (_to_iso(start), _to_iso(end)),
        ).fetchone()

        by_model_rows = conn.execute(
            """
            SELECT
                model,
                COUNT(*) AS requests_total,
                SUM(total_tokens) AS total_tokens_sum,
                SUM(cost_usd) AS cost_usd_total
            FROM ai_usage
            WHERE created_at >= ? AND created_at < ?
            GROUP BY model
            ORDER BY requests_total DESC, model ASC
            """,
            (_to_iso(start), _to_iso(end)),
        ).fetchall()

    return {
        "start": _to_iso(start),
        "end": _to_iso(end),
        "requests_total": int(totals["requests_total"] or 0),
        "succeeded_total": int(totals["succeeded_total"] or 0),
        "failed_total": int(totals["failed_total"] or 0),
        "tokens_in_total": totals["tokens_in_total"],
        "tokens_out_total": totals["tokens_out_total"],
        "total_tokens_sum": totals["total_tokens_sum"],
        "cost_usd_total": totals["cost_usd_total"],
        "latency_ms_avg": totals["latency_ms_avg"],
        "by_model": [dict(row) for row in by_model_rows],
    }
