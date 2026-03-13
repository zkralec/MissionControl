"""Tests for SQLite task run history persistence."""

import importlib
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture()
def worker_module(tmp_path, monkeypatch):
    db_path = tmp_path / "worker_history_main.db"
    history_path = tmp_path / "task_run_history.db"

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("TASK_RUN_HISTORY_DB_PATH", str(history_path))
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("USE_LLM", "false")
    monkeypatch.setenv("DAILY_BUDGET_USD", "10.0")
    monkeypatch.setenv("BUDGET_BUFFER_USD", "0.0")

    for name in ("worker", "task_run_history"):
        if name in sys.modules:
            del sys.modules[name]

    module = importlib.import_module("worker")
    module.Base.metadata.create_all(bind=module.engine)
    return module


def _create_task(module) -> str:
    task_id = str(uuid.uuid4())
    with module.SessionLocal() as db:
        task = module.Task(
            id=task_id,
            created_at=module.now_utc(),
            updated_at=module.now_utc(),
            status=module.TaskStatus.queued,
            task_type="jobs_digest_v1",
            payload_json='{"jobs":[{"title":"ML Engineer","company":"Acme","remote":true,"salary_max":180000}]}',
            model="gpt-4o-mini",
            max_attempts=3,
        )
        db.add(task)
        db.commit()
    return task_id


def test_run_task_writes_sqlite_task_run_history(worker_module) -> None:
    task_id = _create_task(worker_module)

    worker_module.run_task(task_id)

    history_module = importlib.import_module("task_run_history")
    rows = history_module.list_recent_task_runs(limit=10)

    assert rows
    row = rows[0]
    assert row["task_name"] == "jobs_digest_v1"
    assert row["status"] == "succeeded"
    assert row["duration_ms"] is not None
    assert row["input_json"]["jobs"][0]["title"] == "ML Engineer"
    assert row["output_json"]["total_jobs"] == 1
    assert row["error_text"] is None
