"""Write/inspect heartbeat rows and show stale detection behavior."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    default_db = repo_root / "task_run_history.sqlite3"
    os.environ.setdefault("AGENT_HEARTBEAT_DB_PATH", str(default_db))

    api_dir = repo_root / "api"
    if str(api_dir) not in sys.path:
        sys.path.insert(0, str(api_dir))

    import agent_heartbeats  # type: ignore

    now = datetime.now(timezone.utc)
    stale_ts = now - timedelta(minutes=10)

    agent_heartbeats.upsert_agent_heartbeat(
        agent_name="verify-worker",
        status="alive",
        metadata_json={"source": "verify_agent_heartbeats", "kind": "worker"},
        last_seen_at=now,
    )
    agent_heartbeats.upsert_agent_heartbeat(
        agent_name="verify-stale-agent",
        status="alive",
        metadata_json={"source": "verify_agent_heartbeats", "kind": "stale-sim"},
        last_seen_at=stale_ts,
    )

    rows = agent_heartbeats.list_recent_agent_heartbeats(limit=5)
    stale = agent_heartbeats.list_stale_agent_heartbeats(stale_after_seconds=180, now=now, limit=10)

    print(f"agent_heartbeat_db={agent_heartbeats.get_agent_heartbeat_db_path()}")
    print("recent=")
    for row in rows:
        print(row)
    print("stale(>180s)=")
    for row in stale:
        print(row)


if __name__ == "__main__":
    main()
