"""Tests for worker retry policy and permanent failure status."""

import importlib
import os
import sys
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture()
def worker_module(tmp_path, monkeypatch):
    db_path = tmp_path / "worker_retry.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("USE_LLM", "false")
    monkeypatch.setenv("DAILY_BUDGET_USD", "10.0")
    monkeypatch.setenv("BUDGET_BUFFER_USD", "0.0")
    monkeypatch.setenv("OPENAI_MIN_COST_USD", "0.000001")

    if "worker" in sys.modules:
        del sys.modules["worker"]

    module = importlib.import_module("worker")
    module.Base.metadata.create_all(bind=module.engine)
    return module


def _create_task(module, task_type: str, max_attempts: int) -> str:
    task_id = str(uuid.uuid4())
    with module.SessionLocal() as db:
        task = module.Task(
            id=task_id,
            created_at=module.now_utc(),
            updated_at=module.now_utc(),
            status=module.TaskStatus.queued,
            task_type=task_type,
            payload_json='{"x":1}',
            model="gpt-4o-mini",
            max_attempts=max_attempts,
        )
        db.add(task)
        db.commit()
    return task_id


def test_transient_error_schedules_retry(worker_module, monkeypatch) -> None:
    task_id = _create_task(worker_module, "jobs_digest_v1", max_attempts=3)

    def failing_handler(task, db):
        raise TimeoutError("temporary timeout")

    worker_module.HANDLERS["jobs_digest_v1"] = failing_handler

    called = {"value": False}

    def fake_enqueue_at(when, func_name, arg):
        called["value"] = True

    monkeypatch.setattr(worker_module.queue, "enqueue_at", fake_enqueue_at)

    worker_module.run_task(task_id)

    with worker_module.SessionLocal() as db:
        run = db.query(worker_module.Run).filter(worker_module.Run.task_id == task_id).one()
        task = db.get(worker_module.Task, task_id)
        assert run.status == worker_module.RunStatus.failed
        assert task.status == worker_module.TaskStatus.queued
        assert task.next_run_at is not None
        assert called["value"] is True


def test_transient_error_hits_max_attempts_then_failed_permanent(worker_module, monkeypatch) -> None:
    task_id = _create_task(worker_module, "deals_scan_v1", max_attempts=1)

    def failing_handler(task, db):
        raise TimeoutError("temporary timeout")

    worker_module.HANDLERS["deals_scan_v1"] = failing_handler
    monkeypatch.setattr(worker_module.queue, "enqueue_at", lambda when, func_name, arg: None)

    with pytest.raises(TimeoutError):
        worker_module.run_task(task_id)

    with worker_module.SessionLocal() as db:
        run = db.query(worker_module.Run).filter(worker_module.Run.task_id == task_id).one()
        task = db.get(worker_module.Task, task_id)
        assert run.status == worker_module.RunStatus.failed
        assert task.status == worker_module.TaskStatus.failed_permanent
        assert task.next_run_at is None


def test_orphaned_running_task_recovery_requeues_when_attempts_remain(worker_module, monkeypatch) -> None:
    task_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    now = worker_module.now_utc()
    stale_time = now - timedelta(minutes=10)

    with worker_module.SessionLocal() as db:
        task = worker_module.Task(
            id=task_id,
            created_at=stale_time,
            updated_at=stale_time,
            status=worker_module.TaskStatus.running,
            task_type="jobs_rank_v1",
            payload_json='{"request":{}}',
            model="gpt-5",
            max_attempts=3,
        )
        run = worker_module.Run(
            id=run_id,
            task_id=task_id,
            attempt=1,
            status=worker_module.RunStatus.running,
            started_at=stale_time,
            created_at=stale_time,
        )
        db.add(task)
        db.add(run)
        db.commit()

    enqueued: list[tuple[str, str]] = []
    monkeypatch.setattr(worker_module, "_started_task_ids", lambda: set())
    monkeypatch.setattr(worker_module, "ORPHANED_RUN_RECOVERY_ENABLED", True)
    monkeypatch.setattr(worker_module, "ORPHANED_RUN_STALE_AFTER_SEC", 60)
    monkeypatch.setattr(worker_module.queue, "enqueue", lambda fn, arg: enqueued.append((fn, arg)))

    worker_module._recover_orphaned_running_tasks_once()

    with worker_module.SessionLocal() as db:
        task = db.get(worker_module.Task, task_id)
        run = db.get(worker_module.Run, run_id)
        assert task is not None
        assert run is not None
        assert task.status == worker_module.TaskStatus.queued
        assert run.status == worker_module.RunStatus.failed
        assert run.ended_at is not None
        assert "Recovered orphaned running task" in (task.error or "")

    assert enqueued == [("worker.run_task", task_id)]


def test_retry_and_failed_attempt_costs_roll_up_into_task_total(worker_module, monkeypatch) -> None:
    task_id = _create_task(worker_module, "jobs_digest_v1", max_attempts=2)

    def failing_handler(task, db):
        del task, db
        err = TimeoutError("temporary timeout")
        err.usage = {
            "tokens_in": 100,
            "tokens_out": 50,
            "cost_usd": "0.00100000",
            "openai_request_ids": ["req-retry-cost"],
        }
        raise err

    worker_module.HANDLERS["jobs_digest_v1"] = failing_handler
    monkeypatch.setattr(worker_module.queue, "enqueue_at", lambda when, func_name, arg: None)

    # First attempt fails transiently and is queued for retry.
    worker_module.run_task(task_id)
    with worker_module.SessionLocal() as db:
        run1 = (
            db.query(worker_module.Run)
            .filter(worker_module.Run.task_id == task_id, worker_module.Run.attempt == 1)
            .one()
        )
        task = db.get(worker_module.Task, task_id)
        assert run1.status == worker_module.RunStatus.failed
        assert str(run1.cost_usd) == "0.00100000"
        assert run1.tokens_in == 100
        assert run1.tokens_out == 50
        assert task is not None
        assert task.status == worker_module.TaskStatus.queued
        assert str(task.cost_usd) == "0.00100000"
        assert task.tokens_in == 100
        assert task.tokens_out == 50
        task.next_run_at = None
        db.commit()

    # Second attempt fails and reaches max attempts.
    with pytest.raises(TimeoutError):
        worker_module.run_task(task_id)

    with worker_module.SessionLocal() as db:
        runs = (
            db.query(worker_module.Run)
            .filter(worker_module.Run.task_id == task_id)
            .order_by(worker_module.Run.attempt.asc())
            .all()
        )
        task = db.get(worker_module.Task, task_id)

        assert len(runs) == 2
        assert str(runs[0].cost_usd) == "0.00100000"
        assert str(runs[1].cost_usd) == "0.00100000"
        assert task is not None
        assert task.status == worker_module.TaskStatus.failed_permanent
        assert str(task.cost_usd) == "0.00200000"
        assert task.tokens_in == 200
        assert task.tokens_out == 100


def test_blocked_budget_does_not_erase_prior_retry_spend(worker_module, monkeypatch) -> None:
    task_id = _create_task(worker_module, "jobs_digest_v1", max_attempts=2)

    def failing_handler(task, db):
        del task, db
        err = TimeoutError("temporary timeout")
        err.usage = {
            "tokens_in": 100,
            "tokens_out": 50,
            "cost_usd": "0.00100000",
            "openai_request_ids": ["req-retry-budget"],
        }
        raise err

    worker_module.HANDLERS["jobs_digest_v1"] = failing_handler
    monkeypatch.setattr(worker_module.queue, "enqueue_at", lambda when, func_name, arg: None)

    worker_module.run_task(task_id)
    with worker_module.SessionLocal() as db:
        task = db.get(worker_module.Task, task_id)
        assert task is not None
        assert task.status == worker_module.TaskStatus.queued
        assert str(task.cost_usd) == "0.00100000"
        task.next_run_at = None
        db.commit()

    monkeypatch.setattr(
        worker_module,
        "enforce_budget",
        lambda db, min_required_usd: (False, Decimal("10.00000000"), Decimal("0.00000000")),
    )
    worker_module.run_task(task_id)

    with worker_module.SessionLocal() as db:
        task = db.get(worker_module.Task, task_id)
        runs = (
            db.query(worker_module.Run)
            .filter(worker_module.Run.task_id == task_id)
            .order_by(worker_module.Run.attempt.asc())
            .all()
        )
        assert task is not None
        assert task.status == worker_module.TaskStatus.blocked_budget
        assert str(task.cost_usd) == "0.00100000"
        assert len(runs) == 2
        assert str(runs[0].cost_usd) == "0.00100000"
        assert runs[1].cost_usd == Decimal("0")
