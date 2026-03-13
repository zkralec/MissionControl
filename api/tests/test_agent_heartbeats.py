from __future__ import annotations

from datetime import datetime, timedelta, timezone

try:
    from api import agent_heartbeats as hb
except ModuleNotFoundError:
    import agent_heartbeats as hb


def test_set_status_merges_existing_metadata(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "heartbeats.sqlite3"
    monkeypatch.setenv("AGENT_HEARTBEAT_DB_PATH", str(db_path))

    hb.upsert_agent_heartbeat(
        agent_name="worker",
        status="alive",
        metadata_json={"agent_type": "worker", "worker_name": "worker", "pid": 1234},
    )

    changed = hb.set_agent_heartbeat_status(
        agent_name="worker",
        status="stale",
        metadata_json={"watchdog_stale_for_seconds": 900},
    )
    assert changed is True

    row = hb.get_agent_heartbeat("worker")
    assert row is not None
    assert row["status"] == "stale"
    metadata = row["metadata_json"]
    assert isinstance(metadata, dict)
    assert metadata.get("agent_type") == "worker"
    assert metadata.get("pid") == 1234
    assert metadata.get("watchdog_stale_for_seconds") == 900


def test_delete_old_heartbeats_keeps_tracked_names(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "heartbeats.sqlite3"
    monkeypatch.setenv("AGENT_HEARTBEAT_DB_PATH", str(db_path))

    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=12)

    hb.upsert_agent_heartbeat(agent_name="scheduler", status="alive", last_seen_at=old)
    hb.upsert_agent_heartbeat(agent_name="worker", status="alive", last_seen_at=old)
    hb.upsert_agent_heartbeat(agent_name="old-container-id", status="stale", last_seen_at=old)

    deleted = hb.delete_old_agent_heartbeats(
        older_than_seconds=3600,
        now=now,
        keep_agent_names={"scheduler", "worker"},
    )
    assert deleted == 1

    rows = hb.list_recent_agent_heartbeats(limit=10)
    names = {row["agent_name"] for row in rows}
    assert "scheduler" in names
    assert "worker" in names
    assert "old-container-id" not in names


def test_list_stale_heartbeats_agent_filter_avoids_historical_limit_bias(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "heartbeats.sqlite3"
    monkeypatch.setenv("AGENT_HEARTBEAT_DB_PATH", str(db_path))

    now = datetime.now(timezone.utc)
    stale = now - timedelta(minutes=45)
    very_old = now - timedelta(days=7)

    hb.upsert_agent_heartbeat(agent_name="worker", status="alive", last_seen_at=stale)
    hb.upsert_agent_heartbeat(agent_name="stale-history-1", status="stale", last_seen_at=very_old)
    hb.upsert_agent_heartbeat(agent_name="stale-history-2", status="stale", last_seen_at=very_old - timedelta(minutes=1))

    unfiltered = hb.list_stale_agent_heartbeats(
        stale_after_seconds=60,
        now=now,
        limit=2,
    )
    assert {row["agent_name"] for row in unfiltered} == {"stale-history-1", "stale-history-2"}

    filtered = hb.list_stale_agent_heartbeats(
        stale_after_seconds=60,
        now=now,
        limit=2,
        agent_names={"worker"},
    )
    assert len(filtered) == 1
    assert filtered[0]["agent_name"] == "worker"
