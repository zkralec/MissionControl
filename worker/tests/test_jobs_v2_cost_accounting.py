import importlib
import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture()
def worker_module(tmp_path, monkeypatch):
    db_path = tmp_path / "worker_jobs_v2_costs.db"
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


def _seed_upstream_artifact(worker_module, *, task_type: str, result_json: dict) -> tuple[str, str]:
    upstream_task_id = str(uuid.uuid4())
    upstream_run_id = str(uuid.uuid4())
    with worker_module.SessionLocal() as db:
        now = worker_module.now_utc()
        upstream_task = worker_module.Task(
            id=upstream_task_id,
            created_at=now,
            updated_at=now,
            status=worker_module.TaskStatus.success,
            task_type=task_type,
            payload_json=json.dumps({"pipeline_id": "pipe-upstream"}, separators=(",", ":"), ensure_ascii=True),
            model="gpt-4o-mini",
            max_attempts=3,
        )
        upstream_run = worker_module.Run(
            id=upstream_run_id,
            task_id=upstream_task_id,
            attempt=1,
            status=worker_module.RunStatus.success,
            started_at=now,
            ended_at=now,
            created_at=now,
        )
        upstream_artifact = worker_module.Artifact(
            id=str(uuid.uuid4()),
            task_id=upstream_task_id,
            run_id=upstream_run_id,
            artifact_type="result.json",
            content_json=result_json,
        )
        db.add(upstream_task)
        db.add(upstream_run)
        db.add(upstream_artifact)
        db.commit()
    return upstream_task_id, upstream_run_id


def test_jobs_rank_v1_malformed_retry_costs_roll_up_across_attempts(worker_module, monkeypatch) -> None:
    monkeypatch.setenv("USE_LLM", "true")
    monkeypatch.setattr(worker_module.queue, "enqueue_at", lambda when, fn, arg: None)

    upstream_task_id, upstream_run_id = _seed_upstream_artifact(
        worker_module,
        task_type="jobs_normalize_v1",
        result_json={
            "artifact_type": "jobs.normalize.v1",
            "normalized_jobs": [
                {
                    "normalized_job_id": "n1",
                    "title": "ML Engineer",
                    "company": "Acme",
                    "location": "Remote",
                    "source": "linkedin",
                }
            ],
        },
    )

    rank_task_id = str(uuid.uuid4())
    with worker_module.SessionLocal() as db:
        now = worker_module.now_utc()
        task = worker_module.Task(
            id=rank_task_id,
            created_at=now,
            updated_at=now,
            status=worker_module.TaskStatus.queued,
            task_type="jobs_rank_v1",
            payload_json=json.dumps(
                {
                    "pipeline_id": "pipe-rank-cost-rollup",
                    "upstream": {
                        "task_id": upstream_task_id,
                        "run_id": upstream_run_id,
                        "task_type": "jobs_normalize_v1",
                    },
                    "request": {"titles": ["ml engineer"]},
                    "rank_policy": {
                        "llm_enabled": True,
                        "llm_max_retries": 2,
                        "strict_llm_output": True,
                    },
                },
                separators=(",", ":"),
                ensure_ascii=True,
            ),
            model="gpt-4o-mini",
            max_attempts=2,
        )
        db.add(task)
        db.commit()

    jobs_rank_module = importlib.import_module("task_handlers.jobs_rank_v1")
    calls = {"count": 0}

    def _bad_llm(**kwargs):
        del kwargs
        calls["count"] += 1
        return {
            "output_text": "not-json",
            "tokens_in": 10,
            "tokens_out": 10,
            "cost_usd": "0.00010000",
            "openai_request_id": f"req-rank-cost-{calls['count']}",
        }

    monkeypatch.setattr(jobs_rank_module, "run_chat_completion", _bad_llm)

    # attempt=1 -> transient failure -> queued retry
    worker_module.run_task(rank_task_id)
    with worker_module.SessionLocal() as db:
        run1 = (
            db.query(worker_module.Run)
            .filter(worker_module.Run.task_id == rank_task_id, worker_module.Run.attempt == 1)
            .one()
        )
        task = db.get(worker_module.Task, rank_task_id)
        assert run1.status == worker_module.RunStatus.failed
        assert str(run1.cost_usd) == "0.00020000"
        assert run1.tokens_in == 20
        assert run1.tokens_out == 20
        assert task is not None
        assert task.status == worker_module.TaskStatus.queued
        assert str(task.cost_usd) == "0.00020000"
        task.next_run_at = None
        db.commit()

    # attempt=2 -> fails permanently
    with pytest.raises(RuntimeError, match="temporary llm scoring failure"):
        worker_module.run_task(rank_task_id)

    with worker_module.SessionLocal() as db:
        runs = (
            db.query(worker_module.Run)
            .filter(worker_module.Run.task_id == rank_task_id)
            .order_by(worker_module.Run.attempt.asc())
            .all()
        )
        task = db.get(worker_module.Task, rank_task_id)
        latest_debug = (
            db.query(worker_module.Artifact)
            .filter(worker_module.Artifact.task_id == rank_task_id)
            .filter(worker_module.Artifact.run_id == runs[-1].id)
            .filter(worker_module.Artifact.artifact_type == "debug.json")
            .order_by(worker_module.Artifact.created_at.desc())
            .first()
        )

    assert len(runs) == 2
    assert str(runs[0].cost_usd) == "0.00020000"
    assert str(runs[1].cost_usd) == "0.00020000"
    assert task is not None
    assert task.status == worker_module.TaskStatus.failed_permanent
    assert str(task.cost_usd) == "0.00040000"
    assert task.tokens_in == 40
    assert task.tokens_out == 40
    assert latest_debug is not None
    assert isinstance(latest_debug.content_json, dict)
    assert latest_debug.content_json.get("openai_request_ids") == ["req-rank-cost-3", "req-rank-cost-4"]
    ai_usage_task_run_ids = latest_debug.content_json.get("ai_usage_task_run_ids")
    assert isinstance(ai_usage_task_run_ids, list)
    assert len(ai_usage_task_run_ids) == 2


def test_jobs_digest_v2_malformed_retry_costs_roll_up_across_attempts(worker_module, monkeypatch) -> None:
    monkeypatch.setenv("USE_LLM", "true")
    monkeypatch.setattr(worker_module.queue, "enqueue_at", lambda when, fn, arg: None)

    upstream_task_id, upstream_run_id = _seed_upstream_artifact(
        worker_module,
        task_type="jobs_shortlist_v1",
        result_json={
            "artifact_type": "jobs.shortlist.v1",
            "jobs_top_artifact": {
                "artifact_type": "jobs_top.v1",
                "top_jobs": [
                    {
                        "job_id": "j-1",
                        "title": "ML Engineer",
                        "company": "Acme",
                        "location": "Remote",
                        "source": "linkedin",
                        "source_url": "https://www.linkedin.com/jobs/view/1",
                        "explanation_summary": "Strong fit.",
                    }
                ],
                "pipeline_counts": {"collected_count": 12, "deduped_count": 6, "scored_count": 4},
            },
            "pipeline_counts": {"collected_count": 12, "deduped_count": 6, "scored_count": 4},
        },
    )

    digest_task_id = str(uuid.uuid4())
    with worker_module.SessionLocal() as db:
        now = worker_module.now_utc()
        task = worker_module.Task(
            id=digest_task_id,
            created_at=now,
            updated_at=now,
            status=worker_module.TaskStatus.queued,
            task_type="jobs_digest_v2",
            payload_json=json.dumps(
                {
                    "pipeline_id": "pipe-digest-cost-rollup",
                    "upstream": {
                        "task_id": upstream_task_id,
                        "run_id": upstream_run_id,
                        "task_type": "jobs_shortlist_v1",
                    },
                    "request": {"notify_on_empty": False},
                    "digest_policy": {
                        "llm_enabled": True,
                        "llm_max_retries": 2,
                        "strict_llm_output": True,
                    },
                },
                separators=(",", ":"),
                ensure_ascii=True,
            ),
            model="gpt-4o-mini",
            max_attempts=2,
        )
        db.add(task)
        db.commit()

    jobs_digest_module = importlib.import_module("task_handlers.jobs_digest_v2")
    calls = {"count": 0}

    def _bad_llm(**kwargs):
        del kwargs
        calls["count"] += 1
        return {
            "output_text": "not-json",
            "tokens_in": 50,
            "tokens_out": 20,
            "cost_usd": "0.00030000",
            "openai_request_id": f"req-digest-cost-{calls['count']}",
        }

    monkeypatch.setattr(jobs_digest_module, "run_chat_completion", _bad_llm)

    # attempt=1 -> transient failure -> queued retry
    worker_module.run_task(digest_task_id)
    with worker_module.SessionLocal() as db:
        run1 = (
            db.query(worker_module.Run)
            .filter(worker_module.Run.task_id == digest_task_id, worker_module.Run.attempt == 1)
            .one()
        )
        task = db.get(worker_module.Task, digest_task_id)
        assert run1.status == worker_module.RunStatus.failed
        assert str(run1.cost_usd) == "0.00060000"
        assert run1.tokens_in == 100
        assert run1.tokens_out == 40
        assert task is not None
        assert task.status == worker_module.TaskStatus.queued
        assert str(task.cost_usd) == "0.00060000"
        task.next_run_at = None
        db.commit()

    # attempt=2 -> fails permanently
    with pytest.raises(RuntimeError, match="temporary llm digest failure"):
        worker_module.run_task(digest_task_id)

    with worker_module.SessionLocal() as db:
        runs = (
            db.query(worker_module.Run)
            .filter(worker_module.Run.task_id == digest_task_id)
            .order_by(worker_module.Run.attempt.asc())
            .all()
        )
        task = db.get(worker_module.Task, digest_task_id)
        latest_debug = (
            db.query(worker_module.Artifact)
            .filter(worker_module.Artifact.task_id == digest_task_id)
            .filter(worker_module.Artifact.run_id == runs[-1].id)
            .filter(worker_module.Artifact.artifact_type == "debug.json")
            .order_by(worker_module.Artifact.created_at.desc())
            .first()
        )

    assert len(runs) == 2
    assert str(runs[0].cost_usd) == "0.00060000"
    assert str(runs[1].cost_usd) == "0.00060000"
    assert task is not None
    assert task.status == worker_module.TaskStatus.failed_permanent
    assert str(task.cost_usd) == "0.00120000"
    assert task.tokens_in == 200
    assert task.tokens_out == 80
    assert latest_debug is not None
    assert isinstance(latest_debug.content_json, dict)
    assert latest_debug.content_json.get("openai_request_ids") == ["req-digest-cost-3", "req-digest-cost-4"]
    ai_usage_task_run_ids = latest_debug.content_json.get("ai_usage_task_run_ids")
    assert isinstance(ai_usage_task_run_ids, list)
    assert len(ai_usage_task_run_ids) == 2
