import importlib.util
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def scheduler_module(monkeypatch):
    api_dir = str(Path(__file__).resolve().parents[1])
    if api_dir in sys.path:
        sys.path.remove(api_dir)
    sys.path.insert(0, api_dir)

    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    main_path = Path(api_dir) / "main.py"
    scheduler_path = Path(api_dir) / "scheduler.py"
    for module_name in (
        "main",
        "scheduler",
        "ai_usage_log",
        "candidate_profile",
        "agent_heartbeats",
        "event_log",
        "system_metrics",
        "router",
    ):
        sys.modules.pop(module_name, None)

    main_spec = importlib.util.spec_from_file_location("main", main_path)
    if main_spec is None or main_spec.loader is None:
        raise RuntimeError("Failed to load api/main.py for scheduler tests")
    main = importlib.util.module_from_spec(main_spec)
    sys.modules["main"] = main
    main_spec.loader.exec_module(main)

    scheduler_spec = importlib.util.spec_from_file_location("scheduler", scheduler_path)
    if scheduler_spec is None or scheduler_spec.loader is None:
        raise RuntimeError("Failed to load api/scheduler.py for scheduler tests")
    module = importlib.util.module_from_spec(scheduler_spec)
    sys.modules["scheduler"] = module
    scheduler_spec.loader.exec_module(module)

    engine = create_engine("sqlite:///:memory:")
    main.Base.metadata.create_all(bind=engine)
    test_session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    monkeypatch.setattr(module, "SessionLocal", test_session_local)
    monkeypatch.setattr(module, "RUNNING_TASK_RECOVERY_ENABLED", True)
    monkeypatch.setattr(module, "RUNNING_TASK_STALE_AFTER_SEC", 60)
    monkeypatch.setattr(module, "RUNNING_TASK_RECOVERY_MAX_PER_CYCLE", 20)
    monkeypatch.setattr(module, "RUNNING_TASK_AUTO_KILL_ENABLED", True)

    enqueued: list[tuple[str, str]] = []
    monkeypatch.setattr(module.queue, "enqueue", lambda fn, task_id: enqueued.append((fn, task_id)))

    return module, test_session_local, enqueued


def test_recover_stale_running_task_requeues_when_no_active_started_job(scheduler_module, monkeypatch) -> None:
    module, test_session_local, enqueued = scheduler_module
    task_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    now = module.now_utc()
    stale_time = now - timedelta(minutes=15)

    with test_session_local() as db:
        db.add(
            module.Task(
                id=task_id,
                created_at=stale_time,
                updated_at=stale_time,
                status=module.TaskStatus.running,
                task_type="jobs_rank_v1",
                payload_json='{"request":{}}',
                model="gpt-5-mini",
                max_attempts=3,
            )
        )
        db.add(
            module.Run(
                id=run_id,
                task_id=task_id,
                attempt=1,
                status=module.RunStatus.running,
                started_at=stale_time,
                created_at=stale_time,
            )
        )
        db.commit()

    monkeypatch.setattr(module, "_started_jobs_by_task_id", lambda: {})
    summary = module.recover_stale_running_tasks(now=now)

    with test_session_local() as db:
        task = db.get(module.Task, task_id)
        run = db.get(module.Run, run_id)
        assert task is not None
        assert run is not None
        assert task.status == module.TaskStatus.queued
        assert run.status == module.RunStatus.failed
        assert run.ended_at is not None
        assert "Recovered stale running task" in (task.error or "")

    assert summary["recovered"] == 1
    assert summary["reenqueued"] == 1
    assert summary["stop_requested"] == 0
    assert enqueued == [("worker.run_task", task_id)]


def test_recover_stale_running_task_requests_stop_for_active_started_job(scheduler_module, monkeypatch) -> None:
    module, test_session_local, enqueued = scheduler_module
    task_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    now = module.now_utc()
    stale_time = now - timedelta(minutes=12)

    with test_session_local() as db:
        db.add(
            module.Task(
                id=task_id,
                created_at=stale_time,
                updated_at=stale_time,
                status=module.TaskStatus.running,
                task_type="jobs_rank_v1",
                payload_json='{"request":{}}',
                model="gpt-5-mini",
                max_attempts=3,
            )
        )
        db.add(
            module.Run(
                id=run_id,
                task_id=task_id,
                attempt=1,
                status=module.RunStatus.running,
                started_at=stale_time,
                created_at=stale_time,
            )
        )
        db.commit()

    stop_calls: list[str] = []
    monkeypatch.setattr(module, "_started_jobs_by_task_id", lambda: {task_id: ["job-1"]})

    def _fake_stop(job_id: str) -> tuple[bool, str | None]:
        stop_calls.append(job_id)
        return True, None

    monkeypatch.setattr(module, "_request_stop_job", _fake_stop)
    summary = module.recover_stale_running_tasks(now=now)

    with test_session_local() as db:
        task = db.get(module.Task, task_id)
        run = db.get(module.Run, run_id)
        assert task is not None
        assert run is not None
        assert task.status == module.TaskStatus.running
        assert run.status == module.RunStatus.running

    assert stop_calls == ["job-1"]
    assert summary["active_waiting_stop"] == 1
    assert summary["stop_requested"] == 1
    assert summary["recovered"] == 0
    assert enqueued == []
