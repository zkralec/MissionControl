"""Tests for notify_v1 Discord notifications."""

import importlib
import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture()
def worker_module(tmp_path, monkeypatch):
    db_path = tmp_path / "worker_notify.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("USE_LLM", "false")
    monkeypatch.setenv("DAILY_BUDGET_USD", "10.0")
    monkeypatch.setenv("BUDGET_BUFFER_USD", "0.0")
    monkeypatch.setenv("NOTIFY_DISCORD_ALLOWLIST", "deals_scan_v1")

    if "worker" in sys.modules:
        del sys.modules["worker"]

    module = importlib.import_module("worker")
    module.Base.metadata.create_all(bind=module.engine)
    return module


def _create_task(module, payload: dict) -> str:
    task_id = str(uuid.uuid4())
    with module.SessionLocal() as db:
        task = module.Task(
            id=task_id,
            created_at=module.now_utc(),
            updated_at=module.now_utc(),
            status=module.TaskStatus.queued,
            task_type="notify_v1",
            payload_json=json.dumps(payload),
            model="gpt-4o-mini",
            max_attempts=3,
        )
        db.add(task)
        db.commit()
    return task_id


def _latest_result_json(module, task_id: str) -> dict:
    with module.SessionLocal() as db:
        run = db.query(module.Run).filter(module.Run.task_id == task_id).order_by(module.Run.created_at.desc()).first()
        artifact = (
            db.query(module.Artifact)
            .filter(module.Artifact.task_id == task_id)
            .filter(module.Artifact.run_id == run.id)
            .filter(module.Artifact.artifact_type == "result.json")
            .one()
        )
        return artifact.content_json or {}


@pytest.mark.parametrize(
    "payload",
    [
        {"message": "hello", "source_task_type": "deals_scan_v1"},
        {"channels": ["discord"], "source_task_type": "deals_scan_v1"},
    ],
)
def test_schema_rejects_missing_required_fields(worker_module, payload) -> None:
    task_id = _create_task(worker_module, payload)

    worker_module.run_task(task_id)

    with worker_module.SessionLocal() as db:
        run = db.query(worker_module.Run).filter(worker_module.Run.task_id == task_id).one()
        task = db.get(worker_module.Task, task_id)
        assert run.status == worker_module.RunStatus.failed
        assert task.status == worker_module.TaskStatus.failed_permanent
        assert "VALIDATION_ERROR" in (task.error or "")


def test_allowlist_blocks_unknown_source_task_type(worker_module) -> None:
    payload = {
        "channels": ["discord"],
        "message": "hello",
        "severity": "info",
        "source_task_type": "jobs_digest_v1",
    }
    task_id = _create_task(worker_module, payload)

    worker_module.run_task(task_id)

    with worker_module.SessionLocal() as db:
        run = db.query(worker_module.Run).filter(worker_module.Run.task_id == task_id).one()
        task = db.get(worker_module.Task, task_id)
        assert run.status == worker_module.RunStatus.failed
        assert task.status == worker_module.TaskStatus.failed_permanent
        assert "NON_RETRYABLE_ERROR" in (task.error or "")
        assert "NOTIFY_DISCORD_ALLOWLIST" in (task.error or "")


def test_ops_report_source_task_type_is_always_allowed(worker_module, monkeypatch) -> None:
    notify_module = importlib.import_module("task_handlers.notify_v1")

    def fake_send_notification(channels, message, metadata):
        return {
            "discord": {
                "provider": "discord",
                "status": "sent",
                "http_status": 204,
                "rate_limited": False,
            }
        }

    monkeypatch.setattr(notify_module, "send_notification", fake_send_notification)

    payload = {
        "channels": ["discord"],
        "message": "ops report smoke test",
        "severity": "info",
        "source_task_type": "ops_report_v1",
    }
    task_id = _create_task(worker_module, payload)

    worker_module.run_task(task_id)

    with worker_module.SessionLocal() as db:
        run = db.query(worker_module.Run).filter(worker_module.Run.task_id == task_id).one()
        task = db.get(worker_module.Task, task_id)
        assert run.status == worker_module.RunStatus.success
        assert task.status == worker_module.TaskStatus.success


def test_jobs_digest_v2_source_task_type_allowed_when_in_allowlist(worker_module, monkeypatch) -> None:
    monkeypatch.setenv("NOTIFY_DISCORD_ALLOWLIST", "deals_scan_v1,jobs_digest_v2")
    notify_module = importlib.import_module("task_handlers.notify_v1")

    def fake_send_notification(channels, message, metadata):
        return {
            "discord": {
                "provider": "discord",
                "status": "sent",
                "http_status": 204,
                "rate_limited": False,
            }
        }

    monkeypatch.setattr(notify_module, "send_notification", fake_send_notification)

    payload = {
        "channels": ["discord"],
        "message": "jobs digest test",
        "severity": "info",
        "source_task_type": "jobs_digest_v2",
    }
    task_id = _create_task(worker_module, payload)

    worker_module.run_task(task_id)

    with worker_module.SessionLocal() as db:
        run = db.query(worker_module.Run).filter(worker_module.Run.task_id == task_id).one()
        task = db.get(worker_module.Task, task_id)
        assert run.status == worker_module.RunStatus.success
        assert task.status == worker_module.TaskStatus.success


def test_dedupe_prevents_second_send_within_ttl(worker_module, monkeypatch) -> None:
    notify_module = importlib.import_module("task_handlers.notify_v1")
    calls = {"count": 0}

    def fake_send_notification(channels, message, metadata):
        calls["count"] += 1
        assert channels == ["discord"]
        return {
            "discord": {
                "provider": "discord",
                "status": "sent",
                "http_status": 204,
                "rate_limited": False,
            }
        }

    monkeypatch.setattr(notify_module, "send_notification", fake_send_notification)

    payload = {
        "channels": ["discord"],
        "message": "dedupe test",
        "severity": "warn",
        "dedupe_key": "test:discord:001",
        "dedupe_ttl_seconds": 21600,
        "source_task_type": "deals_scan_v1",
        "metadata": {"source": "test"},
    }

    first_task_id = _create_task(worker_module, payload)
    second_task_id = _create_task(worker_module, payload)

    worker_module.run_task(first_task_id)
    worker_module.run_task(second_task_id)

    first_result = _latest_result_json(worker_module, first_task_id)
    second_result = _latest_result_json(worker_module, second_task_id)

    assert calls["count"] == 1
    assert first_result.get("sent") is True
    assert first_result.get("deduped") is False
    assert second_result.get("sent") is False
    assert second_result.get("deduped") is True
    provider_result = second_result.get("provider_result") or {}
    assert provider_result.get("status") == "deduped"


def test_disable_dedupe_allows_manual_test_send_twice(worker_module, monkeypatch) -> None:
    notify_module = importlib.import_module("task_handlers.notify_v1")
    calls = {"count": 0}

    def fake_send_notification(channels, message, metadata):
        calls["count"] += 1
        assert channels == ["discord"]
        return {
            "discord": {
                "provider": "discord",
                "status": "sent",
                "http_status": 204,
                "rate_limited": False,
            }
        }

    monkeypatch.setattr(notify_module, "send_notification", fake_send_notification)

    payload = {
        "channels": ["discord"],
        "message": "manual test notify",
        "severity": "warn",
        "dedupe_key": "manual:test:001",
        "dedupe_ttl_seconds": 21600,
        "disable_dedupe": True,
        "source_task_type": "deals_scan_v1",
        "metadata": {"source": "manual-test"},
    }

    first_task_id = _create_task(worker_module, payload)
    second_task_id = _create_task(worker_module, payload)

    worker_module.run_task(first_task_id)
    worker_module.run_task(second_task_id)

    first_result = _latest_result_json(worker_module, first_task_id)
    second_result = _latest_result_json(worker_module, second_task_id)

    assert calls["count"] == 2
    assert first_result.get("sent") is True
    assert first_result.get("deduped") is False
    assert first_result.get("disable_dedupe") is True
    assert second_result.get("sent") is True
    assert second_result.get("deduped") is False
    assert second_result.get("disable_dedupe") is True


def test_enqueue_notify_task_disable_dedupe_does_not_collapse(worker_module, monkeypatch) -> None:
    enqueued: list[tuple[str, str]] = []

    def fake_enqueue(func_name, task_id):
        enqueued.append((func_name, task_id))

    monkeypatch.setattr(worker_module.queue, "enqueue", fake_enqueue)

    payload = {
        "channels": ["discord"],
        "message": "manual enqueue notify",
        "source_task_type": "deals_scan_v1",
        "severity": "info",
        "dedupe_key": "manual:test:enqueue",
        "disable_dedupe": True,
    }

    with worker_module.SessionLocal() as db:
        first_task_id = worker_module._enqueue_notify_task(db, payload=payload)
        second_task_id = worker_module._enqueue_notify_task(db, payload=payload)

    assert first_task_id != second_task_id
    assert len(enqueued) == 2

    with worker_module.SessionLocal() as db:
        first = db.get(worker_module.Task, first_task_id)
        second = db.get(worker_module.Task, second_task_id)
        assert first is not None
        assert second is not None
        assert first.idempotency_key is None
        assert second.idempotency_key is None


def test_enqueue_followup_notify_disable_dedupe_ignores_spec_idempotency(worker_module, monkeypatch) -> None:
    enqueued: list[tuple[str, str]] = []

    def fake_enqueue(func_name, task_id):
        enqueued.append((func_name, task_id))

    monkeypatch.setattr(worker_module.queue, "enqueue", fake_enqueue)

    spec = {
        "task_type": "notify_v1",
        "payload_json": {
            "channels": ["discord"],
            "message": "manual follow-up notify",
            "source_task_type": "deals_scan_v1",
            "severity": "info",
            "dedupe_key": "manual:test:followup",
            "disable_dedupe": True,
        },
        "idempotency_key": "notify:manual:test:followup",
    }

    with worker_module.SessionLocal() as db:
        first_task_id, first_created = worker_module._enqueue_followup_task(
            db,
            spec=spec,
            parent_task_id="parent-task",
            parent_run_id="parent-run",
        )
        second_task_id, second_created = worker_module._enqueue_followup_task(
            db,
            spec=spec,
            parent_task_id="parent-task",
            parent_run_id="parent-run",
        )

    assert first_created is True
    assert second_created is True
    assert first_task_id != second_task_id
    assert len(enqueued) == 2

    with worker_module.SessionLocal() as db:
        first = db.get(worker_module.Task, first_task_id)
        second = db.get(worker_module.Task, second_task_id)
        assert first is not None
        assert second is not None
        assert first.idempotency_key is None
        assert second.idempotency_key is None


def test_notify_dev_mode_returns_mocked_result(monkeypatch) -> None:
    monkeypatch.setenv("NOTIFY_DEV_MODE", "true")
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)

    discord_module = importlib.import_module("notifications.discord")
    result = discord_module.send_discord_webhook("hello")

    assert result["provider"] == "discord"
    assert result["status"] == "mocked"
