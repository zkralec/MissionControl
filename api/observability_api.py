"""Read-only observability API for Mission Control telemetry data."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from api.ai_usage_log import get_ai_usage_summary, list_ai_usage_today
from api.agent_heartbeats import list_recent_agent_heartbeats, list_stale_agent_heartbeats
from api.event_log import list_events_in_window, list_recent_events
from api.planner_status import get_planner_status_snapshot
from api.system_metrics import get_latest_system_metrics
try:
    from operational_day import current_operational_day_window_utc
except ModuleNotFoundError:  # pragma: no cover - package import fallback
    from api.operational_day import current_operational_day_window_utc

WORKER_DIR = Path(__file__).resolve().parent.parent / "worker"
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))

from task_run_history import get_task_run, list_recent_task_runs, list_task_runs_in_window

app = FastAPI(title="Mission Control Observability API", version="1.0.0")
STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
DEFAULT_HEARTBEAT_STALE_AFTER_SEC = max(
    int(os.getenv("WATCHDOG_STALE_AFTER_SEC", os.getenv("HEARTBEAT_STALE_AFTER_SEC", "180"))),
    30,
)


class HealthOut(BaseModel):
    status: str
    service: str
    utc_now: str


class TaskRunOut(BaseModel):
    id: str
    task_name: str
    status: str
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: int | None = None
    input_json: Any = None
    output_json: Any = None
    error_text: str | None = None
    worker_name: str | None = None
    created_at: str


class TaskRunsListOut(BaseModel):
    items: list[TaskRunOut]
    count: int


class EventOut(BaseModel):
    id: str
    event_type: str
    source: str
    level: str
    message: str
    metadata_json: Any = None
    created_at: str


class EventsListOut(BaseModel):
    items: list[EventOut]
    count: int


class AiUsageByModelOut(BaseModel):
    model: str
    requests_total: int
    total_tokens_sum: int | None = None
    cost_usd_total: float | None = None


class AiUsageSummaryOut(BaseModel):
    start: str
    end: str
    requests_total: int
    succeeded_total: int
    failed_total: int
    tokens_in_total: int | None = None
    tokens_out_total: int | None = None
    total_tokens_sum: int | None = None
    cost_usd_total: float | None = None
    latency_ms_avg: float | None = None
    by_model: list[AiUsageByModelOut] = Field(default_factory=list)


class AiUsageRowOut(BaseModel):
    id: str
    task_run_id: str | None = None
    agent_name: str
    model: str
    tokens_in: int | None = None
    tokens_out: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None
    status: str
    error_text: str | None = None
    created_at: str


class AiUsageTodayOut(BaseModel):
    items: list[AiUsageRowOut]
    count: int
    summary: AiUsageSummaryOut


class SystemMetricsOut(BaseModel):
    id: str
    cpu_percent: float | None = None
    memory_percent: float | None = None
    disk_percent: float | None = None
    load_avg_json: list[float] | None = None
    created_at: str


class AgentHeartbeatOut(BaseModel):
    agent_name: str
    last_seen_at: str
    status: str
    metadata_json: Any = None


class AgentHeartbeatsListOut(BaseModel):
    items: list[AgentHeartbeatOut]
    count: int


class TaskRunsTodaySummaryOut(BaseModel):
    total: int
    pending: int
    running: int
    succeeded: int
    failed: int


class EventsTodaySummaryOut(BaseModel):
    total: int
    by_level: dict[str, int]


class SummaryTodayOut(BaseModel):
    date_utc: str
    window_start: str
    window_end: str
    task_runs: TaskRunsTodaySummaryOut
    events: EventsTodaySummaryOut
    ai_usage: AiUsageSummaryOut
    system_latest: SystemMetricsOut | None = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _day_window_utc() -> tuple[datetime, datetime]:
    return current_operational_day_window_utc()


def _wrap_storage_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=500, detail=f"storage query failed: {exc}")


def _parse_agent_names(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {item.strip() for item in str(raw).split(",") if item.strip()}


def _tracked_agent_names_for_telemetry() -> set[str]:
    tracked = _parse_agent_names(os.getenv("HEARTBEAT_TRACKED_AGENTS", "scheduler,worker"))
    scheduler_name = os.getenv("SCHEDULER_NAME", "scheduler").strip() or "scheduler"
    worker_name = os.getenv("WORKER_NAME", "worker").strip() or "worker"
    tracked.add(scheduler_name)
    tracked.add(worker_name)
    return tracked


@app.get("/", include_in_schema=False)
def get_dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "observability.html")


@app.get("/observability", include_in_schema=False)
def get_dashboard_alias() -> FileResponse:
    return FileResponse(STATIC_DIR / "observability.html")


@app.get("/health", response_model=HealthOut)
def get_health() -> HealthOut:
    return HealthOut(status="ok", service="mission-control-observability", utc_now=_utc_now_iso())


@app.get("/api/task-runs", response_model=TaskRunsListOut)
def get_task_runs(limit: int = Query(default=50, ge=1, le=500)) -> TaskRunsListOut:
    try:
        rows = list_recent_task_runs(limit=limit)
    except Exception as exc:  # pragma: no cover - defensive error handling
        raise _wrap_storage_error(exc)
    items = [TaskRunOut.model_validate(row) for row in rows]
    return TaskRunsListOut(items=items, count=len(items))


@app.get("/api/task-runs/{task_run_id}", response_model=TaskRunOut)
def get_task_run_by_id(task_run_id: str) -> TaskRunOut:
    try:
        row = get_task_run(task_run_id)
    except Exception as exc:  # pragma: no cover - defensive error handling
        raise _wrap_storage_error(exc)
    if row is None:
        raise HTTPException(status_code=404, detail="task run not found")
    return TaskRunOut.model_validate(row)


@app.get("/api/events", response_model=EventsListOut)
def get_events(limit: int = Query(default=100, ge=1, le=1000)) -> EventsListOut:
    try:
        rows = list_recent_events(limit=limit)
    except Exception as exc:  # pragma: no cover - defensive error handling
        raise _wrap_storage_error(exc)
    items = [EventOut.model_validate(row) for row in rows]
    return EventsListOut(items=items, count=len(items))


@app.get("/api/ai-usage/today", response_model=AiUsageTodayOut)
def get_today_ai_usage(limit: int = Query(default=100, ge=1, le=1000)) -> AiUsageTodayOut:
    start, end = _day_window_utc()
    try:
        rows = list_ai_usage_today(limit=limit, start=start, end=end)
        summary = get_ai_usage_summary(start, end)
    except Exception as exc:  # pragma: no cover - defensive error handling
        raise _wrap_storage_error(exc)

    return AiUsageTodayOut(
        items=[AiUsageRowOut.model_validate(row) for row in rows],
        count=len(rows),
        summary=AiUsageSummaryOut.model_validate(summary),
    )


@app.get("/api/ai-usage/summary", response_model=AiUsageSummaryOut)
def get_ai_usage_summary_route(
    start: datetime | None = None,
    end: datetime | None = None,
) -> AiUsageSummaryOut:
    if start is None and end is None:
        resolved_start, resolved_end = _day_window_utc()
    elif start is not None and end is None:
        resolved_start = start
        resolved_end = start + timedelta(days=1)
    elif start is None and end is not None:
        resolved_end = end
        resolved_start = end - timedelta(days=1)
    else:
        resolved_start = start
        resolved_end = end

    if resolved_start.tzinfo is None:
        resolved_start = resolved_start.replace(tzinfo=timezone.utc)
    if resolved_end.tzinfo is None:
        resolved_end = resolved_end.replace(tzinfo=timezone.utc)
    if resolved_end <= resolved_start:
        raise HTTPException(status_code=400, detail="end must be after start")

    try:
        summary = get_ai_usage_summary(resolved_start, resolved_end)
    except Exception as exc:  # pragma: no cover - defensive error handling
        raise _wrap_storage_error(exc)
    return AiUsageSummaryOut.model_validate(summary)


@app.get("/api/system/latest", response_model=SystemMetricsOut | None)
def get_system_latest() -> SystemMetricsOut | None:
    try:
        row = get_latest_system_metrics()
    except Exception as exc:  # pragma: no cover - defensive error handling
        raise _wrap_storage_error(exc)
    if row is None:
        return None
    return SystemMetricsOut.model_validate(row)


@app.get("/api/heartbeats", response_model=AgentHeartbeatsListOut)
def get_heartbeats(limit: int = Query(default=100, ge=1, le=1000)) -> AgentHeartbeatsListOut:
    try:
        rows = list_recent_agent_heartbeats(limit=limit)
    except Exception as exc:  # pragma: no cover - defensive error handling
        raise _wrap_storage_error(exc)
    items = [AgentHeartbeatOut.model_validate(row) for row in rows]
    return AgentHeartbeatsListOut(items=items, count=len(items))


@app.get("/api/heartbeats/stale", response_model=AgentHeartbeatsListOut)
def get_stale_heartbeats(
    stale_after_seconds: int = Query(default=DEFAULT_HEARTBEAT_STALE_AFTER_SEC, ge=1, le=86400),
    limit: int = Query(default=1000, ge=1, le=5000),
    tracked_only: bool = Query(default=True),
    include_historical: bool = Query(default=False),
) -> AgentHeartbeatsListOut:
    safe_limit = max(1, min(int(limit), 5000))
    tracked = _tracked_agent_names_for_telemetry()
    try:
        now = datetime.now(timezone.utc)
        if not tracked_only:
            rows = list_stale_agent_heartbeats(
                stale_after_seconds=stale_after_seconds,
                now=now,
                limit=safe_limit,
            )
        else:
            tracked_rows = list_stale_agent_heartbeats(
                stale_after_seconds=stale_after_seconds,
                now=now,
                limit=max(safe_limit, len(tracked)),
                agent_names=tracked,
            )
            if not include_historical:
                rows = tracked_rows[:safe_limit]
            else:
                historical_rows = list_stale_agent_heartbeats(
                    stale_after_seconds=stale_after_seconds,
                    now=now,
                    limit=safe_limit,
                )
                merged = list(tracked_rows)
                for row in historical_rows:
                    if len(merged) >= safe_limit:
                        break
                    if str(row.get("agent_name") or "") in tracked:
                        continue
                    merged.append(row)
                rows = merged
    except Exception as exc:  # pragma: no cover - defensive error handling
        raise _wrap_storage_error(exc)
    items = [AgentHeartbeatOut.model_validate(row) for row in rows]
    return AgentHeartbeatsListOut(items=items, count=len(items))


@app.get("/api/summary/today", response_model=SummaryTodayOut)
def get_summary_today() -> SummaryTodayOut:
    start, end = _day_window_utc()
    try:
        task_runs = list_task_runs_in_window(start, end, limit=10000)
        events = list_events_in_window(start, end, limit=10000)
        ai_summary = get_ai_usage_summary(start, end)
        latest_system = get_latest_system_metrics()
    except Exception as exc:  # pragma: no cover - defensive error handling
        raise _wrap_storage_error(exc)

    task_counts = {
        "pending": 0,
        "running": 0,
        "succeeded": 0,
        "failed": 0,
    }
    for row in task_runs:
        status = str(row.get("status") or "").lower()
        if status in task_counts:
            task_counts[status] += 1

    level_counts: dict[str, int] = {}
    for event in events:
        level = str(event.get("level") or "unknown").lower()
        level_counts[level] = level_counts.get(level, 0) + 1

    system_model = SystemMetricsOut.model_validate(latest_system) if latest_system else None

    return SummaryTodayOut(
        date_utc=start.date().isoformat(),
        window_start=start.isoformat(),
        window_end=end.isoformat(),
        task_runs=TaskRunsTodaySummaryOut(total=len(task_runs), **task_counts),
        events=EventsTodaySummaryOut(total=len(events), by_level=level_counts),
        ai_usage=AiUsageSummaryOut.model_validate(ai_summary),
        system_latest=system_model,
    )


@app.get("/api/planner/status")
def get_planner_status(event_limit: int = Query(default=300, ge=20, le=2000)) -> dict[str, Any]:
    try:
        return get_planner_status_snapshot(event_limit=event_limit)
    except Exception as exc:  # pragma: no cover - defensive error handling
        raise _wrap_storage_error(exc)
