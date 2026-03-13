import importlib
import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture()
def worker_module(tmp_path, monkeypatch):
    db_path = tmp_path / "worker_jobs_v2_reliability.db"
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


def test_digest_artifact_persists_when_notify_followup_enqueue_fails(worker_module, monkeypatch) -> None:
    upstream_task_id = str(uuid.uuid4())
    upstream_run_id = str(uuid.uuid4())
    digest_task_id = str(uuid.uuid4())

    upstream_result = {
        "artifact_type": "jobs.shortlist.v1",
        "jobs_top_artifact": {
            "artifact_type": "jobs_top.v1",
            "top_jobs": [
                {
                    "job_id": "j-1",
                    "title": "Senior ML Engineer",
                    "company": "Acme AI",
                    "location": "Remote",
                    "source": "linkedin",
                    "source_url": "https://www.linkedin.com/jobs/view/9001",
                    "salary_min": 180000,
                    "salary_max": 220000,
                    "explanation_summary": "Excellent profile fit.",
                }
            ],
            "pipeline_counts": {"collected_count": 25, "deduped_count": 11, "scored_count": 6},
        },
        "pipeline_counts": {"collected_count": 25, "deduped_count": 11, "scored_count": 6},
    }

    with worker_module.SessionLocal() as db:
        now = worker_module.now_utc()
        upstream_task = worker_module.Task(
            id=upstream_task_id,
            created_at=now,
            updated_at=now,
            status=worker_module.TaskStatus.success,
            task_type="jobs_shortlist_v1",
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
            content_json=upstream_result,
        )
        digest_task = worker_module.Task(
            id=digest_task_id,
            created_at=now,
            updated_at=now,
            status=worker_module.TaskStatus.queued,
            task_type="jobs_digest_v2",
            payload_json=json.dumps(
                {
                    "pipeline_id": "pipe-digest-rel",
                    "upstream": {
                        "task_id": upstream_task_id,
                        "run_id": upstream_run_id,
                        "task_type": "jobs_shortlist_v1",
                    },
                    "request": {"notify_on_empty": False},
                    "digest_policy": {"llm_enabled": False, "notify_on_empty": False},
                },
                separators=(",", ":"),
                ensure_ascii=True,
            ),
            model="gpt-4o-mini",
            max_attempts=3,
        )
        db.add(upstream_task)
        db.add(upstream_run)
        db.add(upstream_artifact)
        db.add(digest_task)
        db.commit()

    def _fail_followup(db, *, spec, parent_task_id, parent_run_id):
        del db, spec, parent_task_id, parent_run_id
        raise RuntimeError("simulated notify enqueue failure")

    monkeypatch.setattr(worker_module, "_enqueue_followup_task", _fail_followup)

    worker_module.run_task(digest_task_id)

    with worker_module.SessionLocal() as db:
        digest_task = db.get(worker_module.Task, digest_task_id)
        digest_run = (
            db.query(worker_module.Run)
            .filter(worker_module.Run.task_id == digest_task_id)
            .order_by(worker_module.Run.created_at.desc())
            .first()
        )
        result_artifact = (
            db.query(worker_module.Artifact)
            .filter(worker_module.Artifact.task_id == digest_task_id)
            .filter(worker_module.Artifact.artifact_type == "result.json")
            .order_by(worker_module.Artifact.created_at.desc())
            .first()
        )
        followup_artifact = (
            db.query(worker_module.Artifact)
            .filter(worker_module.Artifact.task_id == digest_task_id)
            .filter(worker_module.Artifact.artifact_type == "followup.json")
            .order_by(worker_module.Artifact.created_at.desc())
            .first()
        )
        notify_tasks = db.query(worker_module.Task).filter(worker_module.Task.task_type == "notify_v1").all()

    assert digest_task is not None
    assert digest_task.status == worker_module.TaskStatus.success
    assert digest_run is not None
    assert digest_run.status == worker_module.RunStatus.success
    assert result_artifact is not None
    assert isinstance(result_artifact.content_json, dict)
    assert result_artifact.content_json.get("artifact_type") == "jobs.digest.v2"
    assert isinstance(result_artifact.content_json.get("jobs_digest_json_artifact"), dict)
    assert followup_artifact is not None
    assert isinstance(followup_artifact.content_json, dict)
    assert followup_artifact.content_json.get("requested_count") == 1
    assert followup_artifact.content_json.get("counts", {}).get("enqueue_failed") == 1
    assert notify_tasks == []


def test_jobs_digest_v2_enqueues_notify_and_notify_task_succeeds(worker_module, monkeypatch) -> None:
    monkeypatch.setenv("NOTIFY_DISCORD_ALLOWLIST", "deals_scan_v1,jobs_digest_v2")

    upstream_task_id = str(uuid.uuid4())
    upstream_run_id = str(uuid.uuid4())
    digest_task_id = str(uuid.uuid4())
    queued: list[str] = []

    def _fake_enqueue(_func_name, queued_task_id):
        queued.append(str(queued_task_id))

    monkeypatch.setattr(worker_module.queue, "enqueue", _fake_enqueue)

    upstream_result = {
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
                    "source_url": "https://www.linkedin.com/jobs/view/123",
                    "salary_min": 160000,
                    "salary_max": 190000,
                    "explanation_summary": "Strong fit.",
                }
            ],
            "pipeline_counts": {"collected_count": 15, "deduped_count": 7, "scored_count": 4},
        },
        "pipeline_counts": {"collected_count": 15, "deduped_count": 7, "scored_count": 4},
    }

    with worker_module.SessionLocal() as db:
        now = worker_module.now_utc()
        upstream_task = worker_module.Task(
            id=upstream_task_id,
            created_at=now,
            updated_at=now,
            status=worker_module.TaskStatus.success,
            task_type="jobs_shortlist_v1",
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
            content_json=upstream_result,
        )
        digest_task = worker_module.Task(
            id=digest_task_id,
            created_at=now,
            updated_at=now,
            status=worker_module.TaskStatus.queued,
            task_type="jobs_digest_v2",
            payload_json=json.dumps(
                {
                    "pipeline_id": "pipe-digest-notify-ok",
                    "upstream": {
                        "task_id": upstream_task_id,
                        "run_id": upstream_run_id,
                        "task_type": "jobs_shortlist_v1",
                    },
                    "request": {"notify_on_empty": False},
                    "digest_policy": {"llm_enabled": False, "notify_on_empty": False},
                },
                separators=(",", ":"),
                ensure_ascii=True,
            ),
            model="gpt-4o-mini",
            max_attempts=3,
        )
        db.add(upstream_task)
        db.add(upstream_run)
        db.add(upstream_artifact)
        db.add(digest_task)
        db.commit()

    worker_module.run_task(digest_task_id)

    with worker_module.SessionLocal() as db:
        digest_task = db.get(worker_module.Task, digest_task_id)
        notify_task = (
            db.query(worker_module.Task)
            .filter(worker_module.Task.task_type == "notify_v1")
            .order_by(worker_module.Task.created_at.desc())
            .first()
        )
        followup_artifact = (
            db.query(worker_module.Artifact)
            .filter(worker_module.Artifact.task_id == digest_task_id)
            .filter(worker_module.Artifact.artifact_type == "followup.json")
            .order_by(worker_module.Artifact.created_at.desc())
            .first()
        )
        assert digest_task is not None
        assert digest_task.status == worker_module.TaskStatus.success
        assert notify_task is not None
        assert notify_task.status == worker_module.TaskStatus.queued
        notify_payload = json.loads(notify_task.payload_json)
        assert notify_payload["source_task_type"] == "jobs_digest_v2"
        assert notify_payload["channels"] == ["discord"]
        assert notify_task.id in queued
        assert followup_artifact is not None
        assert isinstance(followup_artifact.content_json, dict)
        assert followup_artifact.content_json.get("requested_count") == 1
        assert followup_artifact.content_json.get("counts", {}).get("enqueued") == 1

    notify_module = importlib.import_module("task_handlers.notify_v1")

    def _fake_send_notification(channels, message, metadata):
        del message, metadata
        assert channels == ["discord"]
        return {
            "discord": {
                "provider": "discord",
                "status": "sent",
                "http_status": 204,
                "rate_limited": False,
            }
        }

    monkeypatch.setattr(notify_module, "send_notification", _fake_send_notification)
    worker_module.run_task(notify_task.id)

    with worker_module.SessionLocal() as db:
        notify_after = db.get(worker_module.Task, notify_task.id)
        notify_run = (
            db.query(worker_module.Run)
            .filter(worker_module.Run.task_id == notify_task.id)
            .order_by(worker_module.Run.created_at.desc())
            .first()
        )
        notify_result = (
            db.query(worker_module.Artifact)
            .filter(worker_module.Artifact.task_id == notify_task.id)
            .filter(worker_module.Artifact.artifact_type == "result.json")
            .order_by(worker_module.Artifact.created_at.desc())
            .first()
        )

    assert notify_after is not None
    assert notify_after.status == worker_module.TaskStatus.success
    assert notify_run is not None
    assert notify_run.status == worker_module.RunStatus.success
    assert notify_result is not None
    assert isinstance(notify_result.content_json, dict)
    assert notify_result.content_json.get("sent") is True
    assert notify_result.content_json.get("source_task_type") == "jobs_digest_v2"


def test_jobs_digest_v2_empty_shortlist_skips_notify_intentionally(worker_module) -> None:
    upstream_task_id = str(uuid.uuid4())
    upstream_run_id = str(uuid.uuid4())
    digest_task_id = str(uuid.uuid4())

    upstream_result = {
        "artifact_type": "jobs.shortlist.v1",
        "jobs_top_artifact": {
            "artifact_type": "jobs_top.v1",
            "top_jobs": [],
            "pipeline_counts": {"collected_count": 9, "deduped_count": 5, "scored_count": 3},
        },
        "pipeline_counts": {"collected_count": 9, "deduped_count": 5, "scored_count": 3},
    }

    with worker_module.SessionLocal() as db:
        now = worker_module.now_utc()
        upstream_task = worker_module.Task(
            id=upstream_task_id,
            created_at=now,
            updated_at=now,
            status=worker_module.TaskStatus.success,
            task_type="jobs_shortlist_v1",
            payload_json=json.dumps({"pipeline_id": "pipe-upstream-empty"}, separators=(",", ":"), ensure_ascii=True),
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
            content_json=upstream_result,
        )
        digest_task = worker_module.Task(
            id=digest_task_id,
            created_at=now,
            updated_at=now,
            status=worker_module.TaskStatus.queued,
            task_type="jobs_digest_v2",
            payload_json=json.dumps(
                {
                    "pipeline_id": "pipe-digest-empty-intentional",
                    "upstream": {
                        "task_id": upstream_task_id,
                        "run_id": upstream_run_id,
                        "task_type": "jobs_shortlist_v1",
                    },
                    "request": {"notify_on_empty": False},
                    "digest_policy": {"llm_enabled": False, "notify_on_empty": False},
                },
                separators=(",", ":"),
                ensure_ascii=True,
            ),
            model="gpt-4o-mini",
            max_attempts=3,
        )
        db.add(upstream_task)
        db.add(upstream_run)
        db.add(upstream_artifact)
        db.add(digest_task)
        db.commit()

    worker_module.run_task(digest_task_id)

    with worker_module.SessionLocal() as db:
        digest_task = db.get(worker_module.Task, digest_task_id)
        notify_tasks = db.query(worker_module.Task).filter(worker_module.Task.task_type == "notify_v1").all()
        followup_artifact = (
            db.query(worker_module.Artifact)
            .filter(worker_module.Artifact.task_id == digest_task_id)
            .filter(worker_module.Artifact.artifact_type == "followup.json")
            .order_by(worker_module.Artifact.created_at.desc())
            .first()
        )

    assert digest_task is not None
    assert digest_task.status == worker_module.TaskStatus.success
    assert notify_tasks == []
    assert followup_artifact is not None
    assert isinstance(followup_artifact.content_json, dict)
    assert followup_artifact.content_json.get("requested_count") == 0
    assert followup_artifact.content_json.get("notify_decision", {}).get("should_notify") is False
    assert followup_artifact.content_json.get("notify_decision", {}).get("reason") == "skipped_empty_shortlist"
    assert followup_artifact.content_json.get("counts", {}).get("enqueued") == 0


def test_notify_task_visible_in_task_list_and_runs_flow(worker_module, monkeypatch) -> None:
    monkeypatch.setenv("NOTIFY_DISCORD_ALLOWLIST", "deals_scan_v1,jobs_digest_v2")

    upstream_task_id = str(uuid.uuid4())
    upstream_run_id = str(uuid.uuid4())
    digest_task_id = str(uuid.uuid4())

    monkeypatch.setattr(worker_module.queue, "enqueue", lambda _fn, _arg: None)

    upstream_result = {
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
                    "source_url": "https://www.linkedin.com/jobs/view/123",
                    "salary_min": 160000,
                    "salary_max": 190000,
                    "explanation_summary": "Strong fit.",
                }
            ],
            "pipeline_counts": {"collected_count": 15, "deduped_count": 7, "scored_count": 4},
        },
        "pipeline_counts": {"collected_count": 15, "deduped_count": 7, "scored_count": 4},
    }

    with worker_module.SessionLocal() as db:
        now = worker_module.now_utc()
        upstream_task = worker_module.Task(
            id=upstream_task_id,
            created_at=now,
            updated_at=now,
            status=worker_module.TaskStatus.success,
            task_type="jobs_shortlist_v1",
            payload_json=json.dumps({"pipeline_id": "pipe-upstream-visibility"}, separators=(",", ":"), ensure_ascii=True),
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
            content_json=upstream_result,
        )
        digest_task = worker_module.Task(
            id=digest_task_id,
            created_at=now,
            updated_at=now,
            status=worker_module.TaskStatus.queued,
            task_type="jobs_digest_v2",
            payload_json=json.dumps(
                {
                    "pipeline_id": "pipe-digest-visibility",
                    "upstream": {
                        "task_id": upstream_task_id,
                        "run_id": upstream_run_id,
                        "task_type": "jobs_shortlist_v1",
                    },
                    "request": {"notify_on_empty": False},
                    "digest_policy": {"llm_enabled": False, "notify_on_empty": False},
                },
                separators=(",", ":"),
                ensure_ascii=True,
            ),
            model="gpt-4o-mini",
            max_attempts=3,
        )
        db.add(upstream_task)
        db.add(upstream_run)
        db.add(upstream_artifact)
        db.add(digest_task)
        db.commit()

    worker_module.run_task(digest_task_id)

    with worker_module.SessionLocal() as db:
        notify_task = (
            db.query(worker_module.Task)
            .filter(worker_module.Task.task_type == "notify_v1")
            .order_by(worker_module.Task.created_at.desc())
            .first()
        )
        assert notify_task is not None

    notify_module = importlib.import_module("task_handlers.notify_v1")
    monkeypatch.setattr(
        notify_module,
        "send_notification",
        lambda channels, message, metadata: {
            "discord": {
                "provider": "discord",
                "status": "sent",
                "http_status": 204,
                "rate_limited": False,
            }
        },
    )
    worker_module.run_task(notify_task.id)

    with worker_module.SessionLocal() as db:
        listed_tasks = db.query(worker_module.Task).order_by(worker_module.Task.created_at.desc()).limit(50).all()
        listed_task_ids = [task.id for task in listed_tasks]
        notify_runs = (
            db.query(worker_module.Run)
            .filter(worker_module.Run.task_id == notify_task.id)
            .order_by(worker_module.Run.attempt.desc())
            .limit(50)
            .all()
        )

    assert notify_task.id in listed_task_ids
    assert len(notify_runs) >= 1
    assert notify_runs[0].status == worker_module.RunStatus.success


def test_jobs_digest_v2_followup_idempotency_records_deduped_existing(worker_module, monkeypatch) -> None:
    monkeypatch.setenv("NOTIFY_DISCORD_ALLOWLIST", "deals_scan_v1,jobs_digest_v2")
    monkeypatch.setattr(worker_module.queue, "enqueue", lambda _fn, _arg: None)

    upstream_task_id = str(uuid.uuid4())
    upstream_run_id = str(uuid.uuid4())
    first_digest_task_id = str(uuid.uuid4())
    second_digest_task_id = str(uuid.uuid4())

    upstream_result = {
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
                    "source_url": "https://www.linkedin.com/jobs/view/123",
                    "salary_min": 160000,
                    "salary_max": 190000,
                    "explanation_summary": "Strong fit.",
                }
            ],
            "pipeline_counts": {"collected_count": 15, "deduped_count": 7, "scored_count": 4},
        },
        "pipeline_counts": {"collected_count": 15, "deduped_count": 7, "scored_count": 4},
    }

    digest_payload = json.dumps(
        {
            "pipeline_id": "pipe-digest-idempotent-notify",
            "upstream": {
                "task_id": upstream_task_id,
                "run_id": upstream_run_id,
                "task_type": "jobs_shortlist_v1",
            },
            "request": {"notify_on_empty": False},
            "digest_policy": {"llm_enabled": False, "notify_on_empty": False},
        },
        separators=(",", ":"),
        ensure_ascii=True,
    )

    with worker_module.SessionLocal() as db:
        now = worker_module.now_utc()
        upstream_task = worker_module.Task(
            id=upstream_task_id,
            created_at=now,
            updated_at=now,
            status=worker_module.TaskStatus.success,
            task_type="jobs_shortlist_v1",
            payload_json=json.dumps({"pipeline_id": "pipe-upstream-idempotent"}, separators=(",", ":"), ensure_ascii=True),
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
            content_json=upstream_result,
        )
        first_digest_task = worker_module.Task(
            id=first_digest_task_id,
            created_at=now,
            updated_at=now,
            status=worker_module.TaskStatus.queued,
            task_type="jobs_digest_v2",
            payload_json=digest_payload,
            model="gpt-4o-mini",
            max_attempts=3,
        )
        second_digest_task = worker_module.Task(
            id=second_digest_task_id,
            created_at=now,
            updated_at=now,
            status=worker_module.TaskStatus.queued,
            task_type="jobs_digest_v2",
            payload_json=digest_payload,
            model="gpt-4o-mini",
            max_attempts=3,
        )
        db.add(upstream_task)
        db.add(upstream_run)
        db.add(upstream_artifact)
        db.add(first_digest_task)
        db.add(second_digest_task)
        db.commit()

    worker_module.run_task(first_digest_task_id)
    worker_module.run_task(second_digest_task_id)

    with worker_module.SessionLocal() as db:
        notify_tasks = db.query(worker_module.Task).filter(worker_module.Task.task_type == "notify_v1").all()
        assert len(notify_tasks) == 1
        first_notify_task_id = notify_tasks[0].id

        second_followup_artifact = (
            db.query(worker_module.Artifact)
            .filter(worker_module.Artifact.task_id == second_digest_task_id)
            .filter(worker_module.Artifact.artifact_type == "followup.json")
            .order_by(worker_module.Artifact.created_at.desc())
            .first()
        )

    assert second_followup_artifact is not None
    assert isinstance(second_followup_artifact.content_json, dict)
    assert second_followup_artifact.content_json.get("counts", {}).get("deduped_existing") == 1
    outcomes = second_followup_artifact.content_json.get("outcomes", [])
    assert isinstance(outcomes, list)
    assert outcomes[0]["status"] == "deduped_existing"
    assert outcomes[0]["task_id"] == first_notify_task_id
