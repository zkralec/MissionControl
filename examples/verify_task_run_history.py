#!/usr/bin/env python3
"""Run one task locally and print its SQLite task run history row."""

import importlib
import json
import os
import sys
import uuid
from pathlib import Path


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    worker_dir = repo_root / "worker"
    sys.path.insert(0, str(worker_dir))

    default_task_db = repo_root / "verify_worker.sqlite3"
    default_history_db = repo_root / "task_run_history.sqlite3"

    os.environ.setdefault("DATABASE_URL", f"sqlite:///{default_task_db}")
    os.environ.setdefault("TASK_RUN_HISTORY_DB_PATH", str(default_history_db))
    os.environ.setdefault("REDIS_URL", "redis://redis:6379/0")
    os.environ.setdefault("USE_LLM", "false")
    os.environ.setdefault("DAILY_BUDGET_USD", "10.0")
    os.environ.setdefault("BUDGET_BUFFER_USD", "0.0")

    worker = importlib.import_module("worker")
    history = importlib.import_module("task_run_history")

    worker.Base.metadata.create_all(bind=worker.engine)

    task_id = str(uuid.uuid4())
    with worker.SessionLocal() as db:
        task = worker.Task(
            id=task_id,
            created_at=worker.now_utc(),
            updated_at=worker.now_utc(),
            status=worker.TaskStatus.queued,
            task_type="jobs_digest_v1",
            payload_json=json.dumps(
                {
                    "jobs": [
                        {
                            "title": "ML Engineer",
                            "company": "Acme",
                            "remote": True,
                            "salary_max": 180000,
                        }
                    ]
                },
                separators=(",", ":"),
                ensure_ascii=True,
            ),
            model="gpt-4o-mini",
            max_attempts=3,
        )
        db.add(task)
        db.commit()

    worker.run_task(task_id)

    rows = history.list_recent_task_runs(limit=1)
    if not rows:
        raise RuntimeError("No rows found in task_run_history")

    print(f"task_run_history_db={history.get_task_run_history_db_path()}")
    print(json.dumps(rows[0], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
