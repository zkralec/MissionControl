"""Tests for schema validation in worker.run_task()."""

import importlib
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture()
def worker_module(tmp_path, monkeypatch):
    db_path = tmp_path / "worker_validation.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("USE_LLM", "false")
    monkeypatch.setenv("DAILY_BUDGET_USD", "10.0")
    monkeypatch.setenv("BUDGET_BUFFER_USD", "0.0")

    if "worker" in sys.modules:
        del sys.modules["worker"]

    module = importlib.import_module("worker")
    module.Base.metadata.create_all(bind=module.engine)
    return module


def test_schema_failure_is_non_retryable_and_persists_validation_artifact(worker_module, monkeypatch) -> None:
    task_id = str(uuid.uuid4())
    with worker_module.SessionLocal() as db:
        task = worker_module.Task(
            id=task_id,
            created_at=worker_module.now_utc(),
            updated_at=worker_module.now_utc(),
            status=worker_module.TaskStatus.queued,
            task_type="jobs_digest_v1",
            payload_json='["not-a-json-object"]',
            model="gpt-4o-mini",
            max_attempts=3,
        )
        db.add(task)
        db.commit()

    retry_called = {"value": False}

    def fake_enqueue_at(*_args, **_kwargs):
        retry_called["value"] = True

    monkeypatch.setattr(worker_module.queue, "enqueue_at", fake_enqueue_at)

    worker_module.run_task(task_id)

    with worker_module.SessionLocal() as db:
        run = db.query(worker_module.Run).filter(worker_module.Run.task_id == task_id).one()
        task = db.get(worker_module.Task, task_id)
        result_artifact = (
            db.query(worker_module.Artifact)
            .filter(worker_module.Artifact.task_id == task_id)
            .filter(worker_module.Artifact.run_id == run.id)
            .filter(worker_module.Artifact.artifact_type == "result.json")
            .one()
        )

        assert run.status == worker_module.RunStatus.failed
        assert task.status == worker_module.TaskStatus.failed_permanent
        assert task.next_run_at is None
        assert "VALIDATION_ERROR" in (task.error or "")
        assert retry_called["value"] is False

        assert result_artifact.content_json is not None
        assert result_artifact.content_json.get("error_type") == "VALIDATION_ERROR"
        assert "must be a JSON object" in (result_artifact.content_json.get("message") or "")
