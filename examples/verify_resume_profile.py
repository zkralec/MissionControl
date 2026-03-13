#!/usr/bin/env python3
"""Write/read/delete a resume profile row and print results."""

import importlib
import json
import os
import sys
from pathlib import Path


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    worker_dir = repo_root / "worker"
    sys.path.insert(0, str(worker_dir))

    default_db = repo_root / "task_run_history.sqlite3"
    os.environ.setdefault("CANDIDATE_PROFILE_DB_PATH", str(default_db))

    profile = importlib.import_module("candidate_profile")

    saved = profile.upsert_resume_profile(
        resume_text=(
            "Software engineer with backend, distributed systems, and applied ML experience. "
            "Built Python and Go services, plus data pipelines on cloud infrastructure."
        ),
        resume_name="Verification Resume",
        metadata_json={"source": "examples/verify_resume_profile.py"},
    )
    loaded = profile.get_resume_profile(include_text=False)
    removed = profile.delete_resume_profile()

    print(f"candidate_profile_db={profile.get_candidate_profile_db_path()}")
    print("saved=", json.dumps(saved, indent=2, sort_keys=True))
    print("loaded=", json.dumps(loaded, indent=2, sort_keys=True))
    print("deleted=", removed)


if __name__ == "__main__":
    main()
