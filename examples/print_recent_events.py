#!/usr/bin/env python3
"""Print the 20 most recent structured events."""

import importlib
import json
import os
import sys
from pathlib import Path


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    worker_dir = repo_root / "worker"
    sys.path.insert(0, str(worker_dir))

    default_events_db = repo_root / "task_run_history.sqlite3"
    os.environ.setdefault("EVENT_LOG_DB_PATH", str(default_events_db))

    event_log = importlib.import_module("event_log")
    rows = event_log.list_recent_events(limit=20)
    print(f"event_log_db={event_log.get_event_log_db_path()}")
    print(json.dumps(rows, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
