"""Periodic system metrics collection backed by SQLite."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover - optional dependency path
    psutil = None


DB_PATH_ENV = "SYSTEM_METRICS_DB_PATH"
FALLBACK_ENV_PATHS = (
    "EVENT_LOG_DB_PATH",
    "AI_USAGE_DB_PATH",
    "TASK_RUN_HISTORY_DB_PATH",
)
DEFAULT_DB_FILENAME = "task_run_history.sqlite3"


def get_system_metrics_db_path() -> Path:
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
    conn = sqlite3.connect(get_system_metrics_db_path(), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS system_metrics (
            id TEXT PRIMARY KEY,
            cpu_percent REAL,
            memory_percent REAL,
            disk_percent REAL,
            load_avg_json TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_system_metrics_created_at ON system_metrics(created_at DESC)"
    )
    conn.commit()


def _load_avg() -> list[float] | None:
    try:
        one, five, fifteen = os.getloadavg()
        return [round(float(one), 4), round(float(five), 4), round(float(fifteen), 4)]
    except (AttributeError, OSError):
        return None


def _cpu_percent_fallback() -> float | None:
    loads = _load_avg()
    if not loads:
        return None
    cpu_count = os.cpu_count() or 1
    return round((loads[0] / float(cpu_count)) * 100.0, 2)


def _memory_percent_fallback() -> float | None:
    # Linux fallback from /proc/meminfo.
    meminfo_path = Path("/proc/meminfo")
    if not meminfo_path.exists():
        return None

    values: dict[str, int] = {}
    for line in meminfo_path.read_text(encoding="utf-8").splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        parts = rest.strip().split()
        if not parts:
            continue
        try:
            values[key] = int(parts[0])
        except ValueError:
            continue

    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if total is None or available is None or total <= 0:
        return None
    used = total - available
    return round((float(used) / float(total)) * 100.0, 2)


def _disk_percent_fallback() -> float | None:
    try:
        usage = shutil.disk_usage("/")
    except Exception:
        return None
    if usage.total <= 0:
        return None
    return round((float(usage.used) / float(usage.total)) * 100.0, 2)


def _collect_metrics() -> dict[str, Any]:
    if psutil is not None:
        cpu_percent = float(psutil.cpu_percent(interval=0.1))
        memory_percent = float(psutil.virtual_memory().percent)
        disk_percent = float(psutil.disk_usage("/").percent)
        load_avg = _load_avg()
    else:
        cpu_percent = _cpu_percent_fallback()
        memory_percent = _memory_percent_fallback()
        disk_percent = _disk_percent_fallback()
        load_avg = _load_avg()

    return {
        "cpu_percent": cpu_percent,
        "memory_percent": memory_percent,
        "disk_percent": disk_percent,
        "load_avg_json": load_avg,
    }


def collect_system_metrics_snapshot() -> str:
    metrics_id = str(uuid.uuid4())
    snapshot = _collect_metrics()
    with _connect() as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO system_metrics (
                id, cpu_percent, memory_percent, disk_percent, load_avg_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                metrics_id,
                snapshot["cpu_percent"],
                snapshot["memory_percent"],
                snapshot["disk_percent"],
                json.dumps(snapshot["load_avg_json"], separators=(",", ":"), ensure_ascii=True)
                if snapshot["load_avg_json"] is not None
                else None,
                _to_iso(),
            ),
        )
        conn.commit()
    return metrics_id


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    load_avg_raw = row["load_avg_json"]
    load_avg_json: list[float] | None
    if load_avg_raw is None:
        load_avg_json = None
    else:
        try:
            parsed = json.loads(load_avg_raw)
            load_avg_json = parsed if isinstance(parsed, list) else None
        except json.JSONDecodeError:
            load_avg_json = None

    return {
        "id": row["id"],
        "cpu_percent": row["cpu_percent"],
        "memory_percent": row["memory_percent"],
        "disk_percent": row["disk_percent"],
        "load_avg_json": load_avg_json,
        "created_at": row["created_at"],
    }


def get_latest_system_metrics() -> dict[str, Any] | None:
    with _connect() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            """
            SELECT id, cpu_percent, memory_percent, disk_percent, load_avg_json, created_at
            FROM system_metrics
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def list_system_metrics(limit: int = 100) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 1000))
    with _connect() as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT id, cpu_percent, memory_percent, disk_percent, load_avg_json, created_at
            FROM system_metrics
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]
