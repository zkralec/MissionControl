"""
Integration-style tests for artifact persistence in worker.run_task().
"""

import importlib
import os
import sys
import uuid
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture()
def worker_module(tmp_path, monkeypatch):
    """Load worker module with an isolated SQLite database."""
    db_path = tmp_path / "worker_artifacts.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("USE_LLM", "true")
    monkeypatch.setenv("DAILY_BUDGET_USD", "10.0")
    monkeypatch.setenv("BUDGET_BUFFER_USD", "0.0")
    monkeypatch.setenv("OPENAI_MIN_COST_USD", "0.000001")

    if "worker" in sys.modules:
        del sys.modules["worker"]

    module = importlib.import_module("worker")
    module.Base.metadata.create_all(bind=module.engine)
    return module


def _create_task(module, model: str = "gpt-4o-mini") -> str:
    task_id = str(uuid.uuid4())
    with module.SessionLocal() as db:
        task = module.Task(
            id=task_id,
            created_at=module.now_utc(),
            updated_at=module.now_utc(),
            status=module.TaskStatus.queued,
            task_type="deals_scan_v1",
            payload_json='{"deals":[]}',
            model=model,
        )
        db.add(task)
        db.commit()
    return task_id


def test_run_task_persists_artifact_for_successful_llm_run(worker_module) -> None:
    task_id = _create_task(worker_module)

    with patch(
        "worker.run_chat_completion",
        return_value={
            "output_text": "Generated summary text",
            "tokens_in": 42,
            "tokens_out": 21,
            "cost_usd": 0.001,
            "model": "gpt-4o-mini",
        },
    ):
        worker_module.run_task(task_id)

    with worker_module.SessionLocal() as db:
        run = db.query(worker_module.Run).filter(worker_module.Run.task_id == task_id).one()
        artifact = (
            db.query(worker_module.Artifact)
            .filter(worker_module.Artifact.task_id == task_id)
            .filter(worker_module.Artifact.run_id == run.id)
            .filter(worker_module.Artifact.artifact_type == "result.json")
            .one()
        )
        assert artifact.content_text == "Generated summary text"


def test_run_task_allows_empty_output_text(worker_module) -> None:
    task_id = _create_task(worker_module)

    with patch(
        "worker.run_chat_completion",
        return_value={
            "output_text": "",
            "tokens_in": 10,
            "tokens_out": 5,
            "cost_usd": 0.0002,
            "model": "gpt-4o-mini",
        },
    ):
        worker_module.run_task(task_id)

    with worker_module.SessionLocal() as db:
        run = db.query(worker_module.Run).filter(worker_module.Run.task_id == task_id).one()
        artifact = (
            db.query(worker_module.Artifact)
            .filter(worker_module.Artifact.task_id == task_id)
            .filter(worker_module.Artifact.run_id == run.id)
            .filter(worker_module.Artifact.artifact_type == "result.json")
            .one()
        )
        assert artifact.content_text in (None, "")
