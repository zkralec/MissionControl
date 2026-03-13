"""Deterministic daily AI operations report generator and storage."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from ai_usage_log import get_ai_usage_summary
from event_log import list_events_in_window
from system_metrics import get_latest_system_metrics

DB_PATH_ENV = "DAILY_OPS_REPORT_DB_PATH"
FALLBACK_ENV_PATHS = (
    "AGENT_HEARTBEAT_DB_PATH",
    "SYSTEM_METRICS_DB_PATH",
    "EVENT_LOG_DB_PATH",
    "AI_USAGE_DB_PATH",
    "TASK_RUN_HISTORY_DB_PATH",
)
DEFAULT_DB_FILENAME = "task_run_history.sqlite3"
_ENGINE = None


def _get_engine():
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        return None
    _ENGINE = create_engine(database_url, pool_pre_ping=True)
    return _ENGINE


def get_daily_ops_report_db_path() -> Path:
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


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(get_daily_ops_report_db_path(), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_ops_reports (
            report_date TEXT PRIMARY KEY,
            generated_at TEXT NOT NULL,
            report_text TEXT NOT NULL,
            report_json TEXT,
            notification_status TEXT,
            notified_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_daily_ops_reports_generated_at ON daily_ops_reports(generated_at DESC)"
    )
    conn.commit()


def get_daily_ops_report(report_date: date | str) -> dict[str, Any] | None:
    report_date_str = report_date.isoformat() if isinstance(report_date, date) else str(report_date)
    with _connect() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            """
            SELECT report_date, generated_at, report_text, report_json, notification_status, notified_at
            FROM daily_ops_reports
            WHERE report_date = ?
            LIMIT 1
            """,
            (report_date_str,),
        ).fetchone()

    if row is None:
        return None

    parsed_json: Any = None
    if row["report_json"]:
        try:
            parsed_json = json.loads(row["report_json"])
        except json.JSONDecodeError:
            parsed_json = None

    return {
        "report_date": row["report_date"],
        "generated_at": row["generated_at"],
        "report_text": row["report_text"],
        "report_json": parsed_json,
        "notification_status": row["notification_status"],
        "notified_at": row["notified_at"],
    }


def upsert_daily_ops_report(
    *,
    report_date: date | str,
    report_text: str,
    report_json: dict[str, Any],
    notification_status: str | None = None,
) -> None:
    report_date_str = report_date.isoformat() if isinstance(report_date, date) else str(report_date)
    generated_at = _to_iso()
    report_json_text = json.dumps(report_json, separators=(",", ":"), ensure_ascii=True, default=str)

    with _connect() as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO daily_ops_reports (
                report_date, generated_at, report_text, report_json, notification_status, notified_at
            ) VALUES (?, ?, ?, ?, ?, NULL)
            ON CONFLICT(report_date)
            DO UPDATE SET
                generated_at = excluded.generated_at,
                report_text = excluded.report_text,
                report_json = excluded.report_json,
                notification_status = excluded.notification_status
            """,
            (
                report_date_str,
                generated_at,
                report_text,
                report_json_text,
                notification_status,
            ),
        )
        conn.commit()


def mark_daily_ops_report_notification(
    *,
    report_date: date | str,
    notification_status: str,
    notified_at: datetime | None = None,
) -> None:
    report_date_str = report_date.isoformat() if isinstance(report_date, date) else str(report_date)
    with _connect() as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            UPDATE daily_ops_reports
            SET notification_status = ?, notified_at = ?
            WHERE report_date = ?
            """,
            (notification_status, _to_iso(notified_at), report_date_str),
        )
        conn.commit()


def list_recent_daily_ops_reports(limit: int = 30) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 365))
    with _connect() as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT report_date, generated_at, report_text, report_json, notification_status, notified_at
            FROM daily_ops_reports
            ORDER BY report_date DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()

    results: list[dict[str, Any]] = []
    for row in rows:
        parsed_json: Any = None
        if row["report_json"]:
            try:
                parsed_json = json.loads(row["report_json"])
            except json.JSONDecodeError:
                parsed_json = None
        results.append(
            {
                "report_date": row["report_date"],
                "generated_at": row["generated_at"],
                "report_text": row["report_text"],
                "report_json": parsed_json,
                "notification_status": row["notification_status"],
                "notified_at": row["notified_at"],
            }
        )
    return results


def _day_window(report_date: date) -> tuple[datetime, datetime]:
    start = datetime(report_date.year, report_date.month, report_date.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def _query_run_window_summary(start: datetime, end: datetime) -> dict[str, int]:
    engine = _get_engine()
    if engine is not None:
        try:
            with engine.begin() as conn:
                total = conn.execute(
                    text(
                        """
                        SELECT COUNT(*) AS cnt
                        FROM runs
                        WHERE started_at >= :start_ts AND started_at < :end_ts
                        """
                    ),
                    {"start_ts": start, "end_ts": end},
                ).scalar() or 0
                completed = conn.execute(
                    text(
                        """
                        SELECT COUNT(*) AS cnt
                        FROM runs
                        WHERE started_at >= :start_ts
                          AND started_at < :end_ts
                          AND status = 'success'
                        """
                    ),
                    {"start_ts": start, "end_ts": end},
                ).scalar() or 0
                failed = conn.execute(
                    text(
                        """
                        SELECT COUNT(*) AS cnt
                        FROM runs
                        WHERE started_at >= :start_ts
                          AND started_at < :end_ts
                          AND status = 'failed'
                        """
                    ),
                    {"start_ts": start, "end_ts": end},
                ).scalar() or 0
            return {
                "total": int(total),
                "completed": int(completed),
                "failed": int(failed),
            }
        except SQLAlchemyError:
            pass

    start_iso = _to_iso(start)
    end_iso = _to_iso(end)
    with _connect() as conn:
        _ensure_schema(conn)
        try:
            total = conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM task_runs
                WHERE COALESCE(started_at, created_at) >= ?
                  AND COALESCE(started_at, created_at) < ?
                """,
                (start_iso, end_iso),
            ).fetchone()
            completed = conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM task_runs
                WHERE COALESCE(started_at, created_at) >= ?
                  AND COALESCE(started_at, created_at) < ?
                  AND status = 'succeeded'
                """,
                (start_iso, end_iso),
            ).fetchone()
            failed = conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM task_runs
                WHERE COALESCE(started_at, created_at) >= ?
                  AND COALESCE(started_at, created_at) < ?
                  AND status = 'failed'
                """,
                (start_iso, end_iso),
            ).fetchone()
        except sqlite3.OperationalError:
            return {"total": 0, "completed": 0, "failed": 0}
    return {
        "total": int((total or {"cnt": 0})["cnt"] or 0),
        "completed": int((completed or {"cnt": 0})["cnt"] or 0),
        "failed": int((failed or {"cnt": 0})["cnt"] or 0),
    }


def _query_most_active_tasks(start: datetime, end: datetime, limit: int = 5) -> list[dict[str, Any]]:
    engine = _get_engine()
    if engine is not None:
        try:
            with engine.begin() as conn:
                rows = conn.execute(
                    text(
                        """
                        SELECT t.task_type AS task_type, COUNT(r.id) AS run_count
                        FROM runs r
                        JOIN tasks t ON t.id = r.task_id
                        WHERE r.started_at >= :start_ts
                          AND r.started_at < :end_ts
                        GROUP BY t.task_type
                        ORDER BY COUNT(r.id) DESC, t.task_type ASC
                        LIMIT :limit_n
                        """
                    ),
                    {"start_ts": start, "end_ts": end, "limit_n": int(limit)},
                ).fetchall()
            return [{"task_type": row.task_type, "run_count": int(row.run_count or 0)} for row in rows]
        except SQLAlchemyError:
            pass

    start_iso = _to_iso(start)
    end_iso = _to_iso(end)
    with _connect() as conn:
        _ensure_schema(conn)
        try:
            rows = conn.execute(
                """
                SELECT task_name AS task_type, COUNT(id) AS run_count
                FROM task_runs
                WHERE COALESCE(started_at, created_at) >= ?
                  AND COALESCE(started_at, created_at) < ?
                GROUP BY task_name
                ORDER BY COUNT(id) DESC, task_name ASC
                LIMIT ?
                """,
                (start_iso, end_iso, max(1, int(limit))),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    return [{"task_type": row["task_type"], "run_count": int(row["run_count"] or 0)} for row in rows]


def _query_notable_events(start: datetime, end: datetime, limit: int = 6) -> list[dict[str, Any]]:
    rows = list_events_in_window(start, end, limit=500)
    notable: list[dict[str, Any]] = []
    for row in rows:
        level = str(row.get("level") or "").lower()
        if level not in {"warning", "warn", "error"}:
            continue
        notable.append(
            {
                "created_at": row.get("created_at"),
                "level": row.get("level"),
                "event_type": row.get("event_type"),
                "message": row.get("message"),
            }
        )
        if len(notable) >= limit:
            break
    return notable


def _coerce_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _format_system_health(latest_system: dict[str, Any] | None) -> str:
    if not latest_system:
        return "Latest system health: unavailable"

    load_avg = latest_system.get("load_avg_json")
    if isinstance(load_avg, list) and load_avg:
        load_text = " / ".join(f"{_coerce_float(v):.2f}" for v in load_avg)
    else:
        load_text = "-"

    return (
        "Latest system health: "
        f"cpu={_coerce_float(latest_system.get('cpu_percent')):.1f}% "
        f"mem={_coerce_float(latest_system.get('memory_percent')):.1f}% "
        f"disk={_coerce_float(latest_system.get('disk_percent')):.1f}% "
        f"load={load_text} "
        f"(captured {latest_system.get('created_at')})"
    )


def _build_recommendation(
    *,
    today_completed: int,
    today_failed: int,
    today_total: int,
    yesterday_failed: int,
    today_cost_usd: float,
    yesterday_cost_usd: float,
) -> tuple[str, list[str]]:
    flags: list[str] = []

    no_success = today_total > 0 and today_completed == 0
    failure_spike = False
    if today_failed >= 5:
        if yesterday_failed <= 0:
            failure_spike = True
        else:
            failure_spike = today_failed >= max(int(yesterday_failed * 1.5), yesterday_failed + 3)

    if yesterday_cost_usd <= 0:
        cost_spike = today_cost_usd >= 1.0
    else:
        cost_spike = today_cost_usd >= (yesterday_cost_usd * 1.5) and (today_cost_usd - yesterday_cost_usd) >= 0.5

    if no_success:
        flags.append("no_successful_runs")
        return (
            "Recommendation: No successful runs were recorded. Investigate worker errors and upstream dependencies immediately.",
            flags,
        )

    if failure_spike:
        flags.append("failure_spike")
        return (
            "Recommendation: Failure volume spiked versus the previous day. Review warning/error events and recent config changes.",
            flags,
        )

    if cost_spike:
        flags.append("cost_spike")
        return (
            "Recommendation: AI cost spiked versus the previous day. Review high-volume tasks/models and tighten budget guardrails.",
            flags,
        )

    return (
        "Recommendation: Operations are stable. Continue monitoring normal health, cost, and failure trends.",
        flags,
    )


def generate_daily_ai_ops_report(report_date: date) -> dict[str, Any]:
    start, end = _day_window(report_date)
    prev_start, prev_end = _day_window(report_date - timedelta(days=1))

    run_summary = _query_run_window_summary(start, end)
    previous_run_summary = _query_run_window_summary(prev_start, prev_end)
    most_active_tasks = _query_most_active_tasks(start, end, limit=5)

    ai_summary = get_ai_usage_summary(start, end)
    prev_ai_summary = get_ai_usage_summary(prev_start, prev_end)

    latest_system = get_latest_system_metrics()
    notable_events = _query_notable_events(start, end, limit=6)

    today_cost_usd = _coerce_float(ai_summary.get("cost_usd_total"))
    yesterday_cost_usd = _coerce_float(prev_ai_summary.get("cost_usd_total"))

    recommendation, recommendation_flags = _build_recommendation(
        today_completed=run_summary["completed"],
        today_failed=run_summary["failed"],
        today_total=run_summary["total"],
        yesterday_failed=previous_run_summary["failed"],
        today_cost_usd=today_cost_usd,
        yesterday_cost_usd=yesterday_cost_usd,
    )

    if most_active_tasks:
        top_tasks_text = ", ".join(
            f"{row['task_type']} ({row['run_count']})" for row in most_active_tasks
        )
    else:
        top_tasks_text = "none"

    if notable_events:
        notable_lines = [
            f"- [{evt.get('level')}] {evt.get('event_type')}: {evt.get('message')}"
            for evt in notable_events
        ]
    else:
        notable_lines = ["- none"]

    report_lines = [
        f"Mission Control Daily AI Ops Report ({report_date.isoformat()} UTC)",
        (
            "Tasks: "
            f"completed={run_summary['completed']} "
            f"failed={run_summary['failed']} "
            f"total_runs={run_summary['total']}"
        ),
        f"Most active tasks: {top_tasks_text}",
        (
            "AI usage: "
            f"tokens={int(ai_summary.get('total_tokens_sum') or 0)} "
            f"estimated_cost_usd=${today_cost_usd:.6f} "
            f"requests={int(ai_summary.get('requests_total') or 0)}"
        ),
        _format_system_health(latest_system),
        "Notable warnings/errors:",
        *notable_lines,
        recommendation,
    ]

    report_text = "\n".join(report_lines)

    report_json = {
        "report_date": report_date.isoformat(),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "tasks": {
            "completed": run_summary["completed"],
            "failed": run_summary["failed"],
            "total_runs": run_summary["total"],
            "most_active": most_active_tasks,
        },
        "ai_usage": {
            "requests_total": int(ai_summary.get("requests_total") or 0),
            "tokens_total": int(ai_summary.get("total_tokens_sum") or 0),
            "estimated_cost_usd": round(today_cost_usd, 6),
        },
        "system_health_latest": latest_system,
        "notable_events": notable_events,
        "recommendation": recommendation,
        "recommendation_flags": recommendation_flags,
        "comparisons": {
            "previous_day_failed": previous_run_summary["failed"],
            "previous_day_cost_usd": round(yesterday_cost_usd, 6),
        },
    }

    return {
        "report_date": report_date.isoformat(),
        "report_text": report_text,
        "report_json": report_json,
        "severity": (
            "urgent"
            if "no_successful_runs" in recommendation_flags
            else "warn"
            if recommendation_flags
            else "info"
        ),
    }
