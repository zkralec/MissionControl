"""Read helpers for SQLite-backed AI usage logs."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

try:
    from operational_day import current_operational_day_window_utc
except ModuleNotFoundError:  # pragma: no cover - package import fallback
    from api.operational_day import current_operational_day_window_utc

DB_PATH_ENV = "AI_USAGE_DB_PATH"
FALLBACK_ENV_PATHS = ("TASK_RUN_HISTORY_DB_PATH",)
DEFAULT_DB_FILENAME = "task_run_history.sqlite3"
COST_SCALE = Decimal("0.00000001")


def get_ai_usage_db_path() -> Path:
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


def _to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(get_ai_usage_db_path(), timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _quantize_cost(value: Any) -> float | None:
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(COST_SCALE, rounding=ROUND_HALF_UP))


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
    conn.execute("CREATE INDEX IF NOT EXISTS ix_ai_usage_created_at ON ai_usage(created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_ai_usage_model ON ai_usage(model)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_ai_usage_status ON ai_usage(status)")
    conn.commit()


def list_recent_ai_usage(limit: int = 100) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 1000))
    with _connect() as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT
                id, task_run_id, agent_name, model, tokens_in, tokens_out,
                total_tokens, cost_usd, latency_ms, status, error_text, created_at
            FROM ai_usage
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_ai_usage_today(
    limit: int = 100,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 1000))
    default_start, default_end = current_operational_day_window_utc()
    resolved_start = start or default_start
    resolved_end = end or default_end
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
            LIMIT ?
            """,
            (_to_iso(resolved_start), _to_iso(resolved_end), safe_limit),
        ).fetchall()
    return [dict(row) for row in rows]


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

        by_model = conn.execute(
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
        "cost_usd_total": _quantize_cost(totals["cost_usd_total"]),
        "latency_ms_avg": totals["latency_ms_avg"],
        "by_model": [
            {
                **dict(row),
                "cost_usd_total": _quantize_cost(row["cost_usd_total"]),
            }
            for row in by_model
        ],
    }
