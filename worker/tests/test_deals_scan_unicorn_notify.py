"""Tests for deals_scan_v1 unicorn notification triggering."""

import importlib
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture()
def worker_module(tmp_path, monkeypatch):
    db_path = tmp_path / "worker_deals_unicorn.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("USE_LLM", "false")
    monkeypatch.setenv("DAILY_BUDGET_USD", "10.0")
    monkeypatch.setenv("BUDGET_BUFFER_USD", "0.0")
    monkeypatch.setenv("UNICORN_MAX_ITEMS_IN_MESSAGE", "5")
    monkeypatch.setenv("UNICORN_NOTIFY_SEVERITY", "info")
    monkeypatch.setenv("NOTIFY_DEDUPE_TTL_SECONDS", "21600")
    monkeypatch.setenv("DEAL_ALERT_STATE_DB_PATH", str(tmp_path / "deal_alert_state.sqlite3"))
    monkeypatch.setenv("DEAL_ALERT_COOLDOWN_SECONDS", "21600")
    monkeypatch.setenv("DEAL_ALERT_MATERIAL_PRICE_CHANGE_PCT", "3")
    monkeypatch.setenv("DEAL_ALERT_MATERIAL_PRICE_CHANGE_ABS_USD", "25")
    monkeypatch.setenv("SCRAPE_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("SCRAPE_CACHE_TTL_SECONDS", "1")
    monkeypatch.setenv("SCRAPE_RATE_LIMIT_SECONDS", "0")

    for module_name in ("worker.worker", "worker", "task_handlers.deals_scan_v1"):
        if module_name in sys.modules:
            del sys.modules[module_name]

    module = importlib.import_module("worker")
    module.Base.metadata.create_all(bind=module.engine)
    return module


def _create_deals_task(module, payload: dict) -> str:
    task_id = str(uuid.uuid4())
    with module.SessionLocal() as db:
        task = module.Task(
            id=task_id,
            created_at=module.now_utc(),
            updated_at=module.now_utc(),
            status=module.TaskStatus.queued,
            task_type="deals_scan_v1",
            payload_json=json.dumps(payload),
            model="gpt-4o-mini",
            max_attempts=3,
        )
        db.add(task)
        db.commit()
    return task_id


def _create_notify_task(module, payload: dict) -> str:
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


def test_unicorn_filtering_and_message_formatting() -> None:
    deals_module = importlib.import_module("task_handlers.deals_scan_v1")
    normalized = deals_module.normalize_deals(
        [
            {"title": "RTX 5090 Graphics Card", "url": "https://deal/a", "price": 1999},
            {"title": "Gaming Desktop PC with RTX 5090", "url": "https://deal/b", "price": 3999},
            {"title": "RTX 5090 Graphics Card", "url": "https://deal/c", "price": 2400, "discount_pct": 70},
            {"title": "Gaming Laptop RTX 5090", "url": "https://deal/d", "price": 1999},
        ]
    )
    unicorns = deals_module.filter_unicorn_deals(
        normalized,
        gpu_5090_max_price=2000.0,
        pc_5090_max_price=4000.0,
    )

    titles = [item["title"] for item in unicorns]
    urls = {item.get("url") for item in unicorns}
    assert "RTX 5090 Graphics Card" in titles
    assert "Gaming Desktop PC with RTX 5090" in titles
    assert "Gaming Laptop RTX 5090" not in titles
    assert "https://deal/c" not in urls

    message = deals_module.format_unicorn_message(unicorns, max_items=2)
    assert message.startswith("🦄 Unicorn deals found: 2")
    assert message.count("• ") == 2
    assert "https://deal/a" in message
    assert "https://deal/b" in message
    assert "(70%)" not in message


def test_deals_scan_enqueues_notify_for_unicorns(worker_module, monkeypatch) -> None:
    enqueued: list[tuple[str, str]] = []

    def fake_enqueue(func_name, arg):
        enqueued.append((func_name, arg))

    monkeypatch.setattr(worker_module.queue, "enqueue", fake_enqueue)
    deals_module = importlib.import_module("task_handlers.deals_scan_v1")
    monkeypatch.setattr(
        deals_module,
        "_collect_scraped_deals",
        lambda: (
            [
                {
                    "source": "bestbuy",
                    "title": "Gaming Desktop PC with RTX 5090",
                    "url": "https://shop.example/desktop",
                    "price": 3799.0,
                    "old_price": 4099.0,
                    "discount_pct": 7.32,
                    "sku": "1234567",
                    "in_stock": True,
                    "scraped_at": "2026-03-04T00:00:00Z",
                    "raw": {"hint": "test"},
                }
            ],
            {"bestbuy": 1, "newegg": 0, "microcenter": 0},
            [],
        ),
    )

    task_id = _create_deals_task(
        worker_module,
        {
            "source": "test-feed",
            "collectors_enabled": True,
        },
    )

    worker_module.run_task(task_id)

    with worker_module.SessionLocal() as db:
        parent_task = db.get(worker_module.Task, task_id)
        assert parent_task is not None
        assert parent_task.status == worker_module.TaskStatus.success

        notify_task = (
            db.query(worker_module.Task)
            .filter(worker_module.Task.task_type == "notify_v1")
            .order_by(worker_module.Task.created_at.desc())
            .first()
        )
        assert notify_task is not None
        payload = json.loads(notify_task.payload_json)
        assert payload["source_task_type"] == "deals_scan_v1"
        assert payload["channels"] == ["discord"]
        assert payload["severity"] == "info"
        assert payload["include_header"] is False
        assert payload["include_metadata"] is False
        assert payload["dedupe_ttl_seconds"] == 21600
        assert payload["dedupe_key"].startswith("unicorn:")
        assert "🦄 Unicorn deals found: 1" in payload["message"]
        assert "https://shop.example/desktop" in payload["message"]
        metadata = payload.get("metadata") or {}
        assert metadata.get("scan_source") == "test-feed"
        assert metadata.get("deals_count") == 1
        assert metadata.get("unicorn_count") == 1

    assert len(enqueued) == 1
    assert enqueued[0][0] == "worker.run_task"


def test_deals_scan_skips_notify_when_no_unicorns(worker_module, monkeypatch) -> None:
    enqueued: list[tuple[str, str]] = []

    def fake_enqueue(func_name, arg):
        enqueued.append((func_name, arg))

    monkeypatch.setattr(worker_module.queue, "enqueue", fake_enqueue)

    task_id = _create_deals_task(
        worker_module,
        {
            "source": "test-feed",
            "collectors_enabled": False,
            "deals": [
                {
                    "title": "Small Discount",
                    "url": "https://shop.example/basic",
                    "price": 90,
                    "old_price": 100,
                    "size_usd": 500000,
                }
            ],
        },
    )

    worker_module.run_task(task_id)

    with worker_module.SessionLocal() as db:
        parent_task = db.get(worker_module.Task, task_id)
        assert parent_task is not None
        assert parent_task.status == worker_module.TaskStatus.success
        notify_tasks = db.query(worker_module.Task).filter(worker_module.Task.task_type == "notify_v1").all()
        assert notify_tasks == []

    assert enqueued == []


def test_notify_payload_can_skip_header_and_metadata(worker_module, monkeypatch) -> None:
    notify_module = importlib.import_module("task_handlers.notify_v1")
    captured = {"message": None}

    def fake_send_notification(channels, message, metadata):
        captured["message"] = message
        return {
            "discord": {
                "provider": "discord",
                "status": "sent",
                "http_status": 204,
                "rate_limited": False,
            }
        }

    monkeypatch.setattr(notify_module, "send_notification", fake_send_notification)

    task_id = _create_notify_task(
        worker_module,
        {
            "channels": ["discord"],
            "message": "just the deal line",
            "severity": "info",
            "source_task_type": "deals_scan_v1",
            "metadata": {"scan_source": "manual", "deals_count": 1, "unicorn_count": 1},
            "include_header": False,
            "include_metadata": False,
        },
    )

    worker_module.run_task(task_id)

    assert captured["message"] == "just the deal line"


def test_deals_scan_alert_policy_suppresses_duplicates_until_material_change(worker_module) -> None:
    deals_module = importlib.import_module("task_handlers.deals_scan_v1")
    base_ts = datetime(2026, 3, 10, 0, 0, tzinfo=timezone.utc)
    payload = {
        "source": "test-feed",
        "collectors_enabled": False,
        "deals": [
            {
                "source": "bestbuy",
                "title": "Gaming Desktop PC with RTX 5090",
                "url": "https://shop.example/desktop",
                "price": 3799.0,
                "old_price": 4099.0,
                "sku": "SKU-123",
                "in_stock": True,
            }
        ],
    }
    payload_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)

    first = deals_module.build_unicorn_notify_request(
        payload_json=payload_json,
        result_json=None,
        run_timestamp=base_ts,
    )
    assert first.get("notify_payload") is not None
    assert first.get("alertable_unicorn_count") == 1

    second = deals_module.build_unicorn_notify_request(
        payload_json=payload_json,
        result_json=None,
        run_timestamp=base_ts + timedelta(minutes=30),
    )
    assert second.get("notify_payload") is None
    assert second.get("unicorn_count") == 1
    assert second.get("alertable_unicorn_count") == 0

    changed_payload = dict(payload)
    changed_payload["deals"] = [dict(payload["deals"][0], price=3599.0)]
    third = deals_module.build_unicorn_notify_request(
        payload_json=json.dumps(changed_payload, separators=(",", ":"), ensure_ascii=True),
        result_json=None,
        run_timestamp=base_ts + timedelta(hours=1),
    )
    assert third.get("notify_payload") is not None
    assert third.get("alertable_unicorn_count") == 1
