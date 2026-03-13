import json
import sys
import uuid

sys.path.insert(0, "/app")

from fastapi.testclient import TestClient

from main import (
    app,
    Artifact,
    Run,
    RunStatus,
    SessionLocal,
    Task,
    TaskStatus,
    now_utc,
)

client = TestClient(app)


def _cleanup_watcher_and_task(watcher_id: str, task_id: str | None = None) -> None:
    client.delete(f"/watchers/{watcher_id}")
    if not task_id:
        return
    with SessionLocal() as db:
        db.query(Artifact).filter(Artifact.task_id == task_id).delete()
        db.query(Run).filter(Run.task_id == task_id).delete()
        db.query(Task).filter(Task.id == task_id).delete()
        db.commit()


def test_create_watcher_normalizes_template_fields() -> None:
    watcher_id = f"watcher-test-{uuid.uuid4()}"
    try:
        response = client.post(
            "/watchers",
            json={
                "id": watcher_id,
                "name": "Deals Watcher",
                "task_type": "deals_scan_v1",
                "payload_json": '{"source":"watcher-test","collectors_enabled":true}',
                "interval_seconds": 180,
                "enabled": True,
                "priority": 15,
                "notification_behavior": {"mode": "notify_on_unicorn", "channel": "discord"},
                "metadata": {"owner": "ops", "watcher_category": "deals"},
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["id"] == watcher_id
        assert payload["interval_seconds"] == 180
        assert payload["min_interval_seconds"] == 180
        assert payload["notification_behavior"]["mode"] == "notify_on_unicorn"
        assert payload["metadata"]["owner"] == "ops"
        assert "notification_behavior" not in payload["metadata"]

        get_response = client.get(f"/watchers/{watcher_id}")
        assert get_response.status_code == 200
        assert get_response.json()["name"] == "Deals Watcher"
    finally:
        _cleanup_watcher_and_task(watcher_id)


def test_patch_watcher_interval_and_notification_behavior() -> None:
    watcher_id = f"watcher-test-{uuid.uuid4()}"
    try:
        create_response = client.post(
            "/watchers",
            json={
                "id": watcher_id,
                "name": "Jobs Watcher",
                "task_type": "jobs_digest_v1",
                "payload_json": '{"source":"watcher-test-jobs"}',
                "interval_seconds": 300,
            },
        )
        assert create_response.status_code == 200

        patch_response = client.patch(
            f"/watchers/{watcher_id}",
            json={
                "min_interval_seconds": 420,
                "enabled": False,
                "notification_behavior": {"mode": "digest", "channel": "email"},
            },
        )
        assert patch_response.status_code == 200
        patched = patch_response.json()
        assert patched["enabled"] is False
        assert patched["interval_seconds"] == 420
        assert patched["min_interval_seconds"] == 420
        assert patched["notification_behavior"]["mode"] == "digest"
        assert patched["notification_behavior"]["channel"] == "email"
    finally:
        _cleanup_watcher_and_task(watcher_id)


def test_watcher_includes_last_run_and_outcome_summary() -> None:
    watcher_id = f"watcher-test-{uuid.uuid4()}"
    task_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    try:
        create_response = client.post(
            "/watchers",
            json={
                "id": watcher_id,
                "name": "Notify Watcher",
                "task_type": "notify_v1",
                "payload_json": '{"channel":"discord","message":"test"}',
                "interval_seconds": 300,
            },
        )
        assert create_response.status_code == 200

        with SessionLocal() as db:
            task = Task(
                id=task_id,
                created_at=now_utc(),
                updated_at=now_utc(),
                status=TaskStatus.failed,
                task_type="notify_v1",
                payload_json=json.dumps(
                    {
                        "channel": "discord",
                        "message": "watcher run",
                        "planner_template_id": watcher_id,
                    },
                    separators=(",", ":"),
                ),
                model="gpt-4o-mini",
                error="Notification delivery failed",
                max_attempts=3,
            )
            run = Run(
                id=run_id,
                task_id=task_id,
                attempt=1,
                status=RunStatus.failed,
                started_at=now_utc(),
                ended_at=now_utc(),
                wall_time_ms=321,
                error="Webhook timeout",
                created_at=now_utc(),
            )
            artifact = Artifact(
                id=str(uuid.uuid4()),
                task_id=task_id,
                run_id=run_id,
                artifact_type="result.json",
                content_json={"summary": "Delivery failed after timeout", "status": "failed"},
                created_at=now_utc(),
            )
            db.add(task)
            db.add(run)
            db.add(artifact)
            db.commit()

        watcher_response = client.get(f"/watchers/{watcher_id}")
        assert watcher_response.status_code == 200
        payload = watcher_response.json()
        assert payload["last_run_summary"] is not None
        assert payload["last_run_summary"]["task_id"] == task_id
        assert payload["last_run_summary"]["task_status"] == "failed"
        assert payload["last_run_summary"]["run_id"] == run_id
        assert payload["last_outcome_summary"] is not None
        assert payload["last_outcome_summary"]["artifact_type"] == "result.json"
        assert "Delivery failed" in (payload["last_outcome_summary"]["message"] or "")
    finally:
        _cleanup_watcher_and_task(watcher_id, task_id=task_id)
