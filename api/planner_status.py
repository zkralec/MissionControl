"""Read helpers for autonomous planner observability status."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

try:  # pragma: no cover - import path fallback for package mode
    from event_log import list_recent_events
    from planner_control import (
        get_planner_runtime_config,
        list_planner_task_templates,
    )
except ImportError:  # pragma: no cover
    from api.event_log import list_recent_events
    from api.planner_control import (
        get_planner_runtime_config,
        list_planner_task_templates,
    )


def _parse_iso(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def get_planner_status_snapshot(event_limit: int = 300) -> dict[str, Any]:
    safe_limit = max(20, min(int(event_limit), 2000))
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=1)

    rows = list_recent_events(limit=safe_limit)
    planner_rows = [
        row
        for row in rows
        if str(row.get("event_type") or "")
        in {"autonomous_planner_tick", "autonomous_planner_cycle"}
    ]

    last_tick = next((row for row in planner_rows if row.get("event_type") == "autonomous_planner_tick"), None)
    last_cycle = next((row for row in planner_rows if row.get("event_type") == "autonomous_planner_cycle"), None)

    cycles_24h = 0
    ticks_24h = 0
    alerts_24h = 0
    executed_24h = 0
    create_decisions_24h = 0

    for row in planner_rows:
        created_at = _parse_iso(row.get("created_at"))
        if created_at is None or created_at < start:
            continue
        event_type = str(row.get("event_type") or "")
        metadata = row.get("metadata_json") if isinstance(row.get("metadata_json"), dict) else {}

        if event_type == "autonomous_planner_tick":
            ticks_24h += 1
            alerts_24h += _as_int(metadata.get("alert_count"), 0)
            executed_24h += _as_int(metadata.get("executed_count"), 0)
            decision_counts = metadata.get("decision_counts")
            if isinstance(decision_counts, dict):
                create_decisions_24h += _as_int(decision_counts.get("create_task"), 0)
        elif event_type == "autonomous_planner_cycle":
            cycles_24h += 1
            executed_24h += _as_int(metadata.get("executed_count"), 0)
            decision_counts = metadata.get("decision_counts")
            if isinstance(decision_counts, dict):
                alerts_24h += _as_int(decision_counts.get("alert"), 0)
                create_decisions_24h += _as_int(decision_counts.get("create_task"), 0)

    try:
        runtime_cfg = get_planner_runtime_config()
    except Exception:
        runtime_cfg = {
            "enabled": False,
            "execution_enabled": False,
            "require_approval": True,
            "approved": False,
            "interval_sec": 300,
            "max_create_per_cycle": 1,
            "max_execute_per_cycle": 2,
            "cost_budget_usd": None,
            "token_budget": None,
        }

    try:
        templates = list_planner_task_templates(limit=100)
    except Exception:
        templates = []

    enabled_templates = [row for row in templates if bool(row.get("enabled"))]
    primary_template = enabled_templates[0] if enabled_templates else None

    return {
        "captured_at": now.isoformat(),
        "enabled": bool(runtime_cfg.get("enabled", False)),
        "mode": "execute" if bool(runtime_cfg.get("execution_enabled", False)) else "recommendation",
        "execution_enabled": bool(runtime_cfg.get("execution_enabled", False)),
        "require_approval": bool(runtime_cfg.get("require_approval", True)),
        "approved": bool(runtime_cfg.get("approved", False)),
        "interval_sec": int(runtime_cfg.get("interval_sec") or 300),
        "max_create_per_cycle": int(runtime_cfg.get("max_create_per_cycle") or 0),
        "max_execute_per_cycle": int(runtime_cfg.get("max_execute_per_cycle") or 0),
        "cost_budget_usd": runtime_cfg.get("cost_budget_usd"),
        "token_budget": runtime_cfg.get("token_budget"),
        "templates_total": len(templates),
        "templates_enabled": len(enabled_templates),
        "templates": templates[:20],
        "create_task_type": (primary_template or {}).get("task_type"),
        "create_payload_json": (primary_template or {}).get("payload_json"),
        "last_tick": last_tick,
        "last_cycle": last_cycle,
        "recent_summary_24h": {
            "ticks": ticks_24h,
            "cycles": cycles_24h,
            "alerts": alerts_24h,
            "executed_actions": executed_24h,
            "create_decisions": create_decisions_24h,
        },
        "recent_events": planner_rows[:20],
    }
