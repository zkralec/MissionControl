"""
Tests for task handler registry behavior in worker.run_task().
"""

import importlib
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture()
def worker_module(tmp_path, monkeypatch):
    db_path = tmp_path / "worker_registry.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("USE_LLM", "false")
    monkeypatch.setenv("DAILY_BUDGET_USD", "10.0")
    monkeypatch.setenv("BUDGET_BUFFER_USD", "0.0")
    monkeypatch.setenv("OPENAI_MIN_COST_USD", "0.000001")

    if "worker" in sys.modules:
        del sys.modules["worker"]

    module = importlib.import_module("worker")
    module.Base.metadata.create_all(bind=module.engine)
    return module


def test_registry_contains_required_handlers(worker_module) -> None:
    assert "jobs_digest_v1" in worker_module.HANDLERS
    assert "jobs_collect_v1" in worker_module.HANDLERS
    assert "jobs_normalize_v1" in worker_module.HANDLERS
    assert "jobs_rank_v1" in worker_module.HANDLERS
    assert "jobs_shortlist_v1" in worker_module.HANDLERS
    assert "jobs_digest_v2" in worker_module.HANDLERS
    assert "deals_scan_v1" in worker_module.HANDLERS
    assert "slides_outline_v1" in worker_module.HANDLERS
    assert "notify_v1" in worker_module.HANDLERS


def test_run_task_fails_with_clear_error_for_missing_handler(worker_module) -> None:
    task_id = str(uuid.uuid4())
    with worker_module.SessionLocal() as db:
        task = worker_module.Task(
            id=task_id,
            created_at=worker_module.now_utc(),
            updated_at=worker_module.now_utc(),
            status=worker_module.TaskStatus.queued,
            task_type="unknown_task_type",
            payload_json='{"x":1}',
            model="gpt-4o-mini",
        )
        db.add(task)
        db.commit()

    worker_module.run_task(task_id)

    with worker_module.SessionLocal() as db:
        run = db.query(worker_module.Run).filter(worker_module.Run.task_id == task_id).one()
        task = db.get(worker_module.Task, task_id)
        assert run.status == worker_module.RunStatus.failed
        assert task.status == worker_module.TaskStatus.failed
        assert "Unknown task_type 'unknown_task_type'" in (run.error or "")


def test_run_task_executes_stub_handler_end_to_end(worker_module) -> None:
    task_id = str(uuid.uuid4())
    with worker_module.SessionLocal() as db:
        task = worker_module.Task(
            id=task_id,
            created_at=worker_module.now_utc(),
            updated_at=worker_module.now_utc(),
            status=worker_module.TaskStatus.queued,
            task_type="deals_scan_v1",
            payload_json='{"deals":[]}',
            model="gpt-4o-mini",
        )
        db.add(task)
        db.commit()

    worker_module.run_task(task_id)

    with worker_module.SessionLocal() as db:
        run = db.query(worker_module.Run).filter(worker_module.Run.task_id == task_id).one()
        task = db.get(worker_module.Task, task_id)
        artifact = (
            db.query(worker_module.Artifact)
            .filter(worker_module.Artifact.task_id == task_id)
            .filter(worker_module.Artifact.run_id == run.id)
            .filter(worker_module.Artifact.artifact_type == "result.json")
            .one()
        )
        assert run.status == worker_module.RunStatus.success
        assert task.status == worker_module.TaskStatus.success
        assert artifact.artifact_type == "result.json"


def test_run_task_enqueues_followup_tasks_from_handler(worker_module, monkeypatch) -> None:
    task_id = str(uuid.uuid4())
    with worker_module.SessionLocal() as db:
        task = worker_module.Task(
            id=task_id,
            created_at=worker_module.now_utc(),
            updated_at=worker_module.now_utc(),
            status=worker_module.TaskStatus.queued,
            task_type="jobs_collect_v1",
            payload_json='{"request":{"collectors_enabled":false,"sources":["manual"],"manual_jobs":[{"title":"ML Engineer","company":"Acme"}]}}',
            model="gpt-4o-mini",
            max_attempts=3,
        )
        db.add(task)
        db.commit()

    enqueued_ids: list[str] = []

    def fake_enqueue(_func_name, queued_task_id):
        enqueued_ids.append(str(queued_task_id))

    monkeypatch.setattr(worker_module.queue, "enqueue", fake_enqueue)

    worker_module.run_task(task_id)

    with worker_module.SessionLocal() as db:
        followup = (
            db.query(worker_module.Task)
            .filter(worker_module.Task.task_type == "jobs_normalize_v1")
            .order_by(worker_module.Task.created_at.desc())
            .first()
        )
        assert followup is not None
        assert followup.status == worker_module.TaskStatus.queued
        assert followup.id in enqueued_ids


def test_run_task_records_handler_reported_usage(worker_module) -> None:
    task_type = "slides_outline_v1"
    task_id = str(uuid.uuid4())
    with worker_module.SessionLocal() as db:
        task = worker_module.Task(
            id=task_id,
            created_at=worker_module.now_utc(),
            updated_at=worker_module.now_utc(),
            status=worker_module.TaskStatus.queued,
            task_type=task_type,
            payload_json='{"topic":"usage probe"}',
            model="gpt-5",
            max_attempts=3,
        )
        db.add(task)
        db.commit()

    def _usage_handler(task, db):
        del task, db
        return {
            "artifact_type": "usage.probe.v1",
            "content_json": {"ok": True},
            "usage": {
                "tokens_in": 321,
                "tokens_out": 123,
                "cost_usd": "0.01234567",
                "openai_request_ids": ["req-usage-1", "req-usage-2"],
            },
        }

    worker_module.HANDLERS[task_type] = _usage_handler
    worker_module.run_task(task_id)

    with worker_module.SessionLocal() as db:
        run = db.query(worker_module.Run).filter(worker_module.Run.task_id == task_id).one()
        task = db.get(worker_module.Task, task_id)
        debug_artifact = (
            db.query(worker_module.Artifact)
            .filter(worker_module.Artifact.task_id == task_id)
            .filter(worker_module.Artifact.run_id == run.id)
            .filter(worker_module.Artifact.artifact_type == "debug.json")
            .one()
        )

        assert run.status == worker_module.RunStatus.success
        assert run.tokens_in == 321
        assert run.tokens_out == 123
        assert str(run.cost_usd) == "0.01234567"
        assert task is not None
        assert task.tokens_in == 321
        assert task.tokens_out == 123
        assert str(task.cost_usd) == "0.01234567"
        assert isinstance(debug_artifact.content_json, dict)
        assert debug_artifact.content_json.get("openai_request_ids") == ["req-usage-1", "req-usage-2"]
