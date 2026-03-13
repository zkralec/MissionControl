#!/usr/bin/env python3
"""Record one system metrics snapshot and print the latest row."""

import importlib
import json
import os
import sys
from pathlib import Path


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    api_dir = repo_root / "api"
    sys.path.insert(0, str(api_dir))

    default_db = repo_root / "task_run_history.sqlite3"
    os.environ.setdefault("SYSTEM_METRICS_DB_PATH", str(default_db))

    system_metrics = importlib.import_module("system_metrics")

    metrics_id = system_metrics.collect_system_metrics_snapshot()
    latest = system_metrics.get_latest_system_metrics()

    print(f"system_metrics_db={system_metrics.get_system_metrics_db_path()}")
    print(f"snapshot_id={metrics_id}")
    print(json.dumps(latest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
