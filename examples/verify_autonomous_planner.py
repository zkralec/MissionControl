#!/usr/bin/env python3
"""Verify autonomous planner decision output in recommendation mode."""

from __future__ import annotations

import importlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    api_dir = repo_root / "api"
    if str(api_dir) not in sys.path:
        sys.path.insert(0, str(api_dir))

    os.environ.setdefault("AUTONOMOUS_PLANNER_MAX_EXECUTE_PER_CYCLE", "2")
    os.environ.setdefault("AUTONOMOUS_PLANNER_MAX_CREATE_PER_CYCLE", "1")
    os.environ.setdefault("AUTONOMOUS_PLANNER_CREATE_TASK_TYPE", "deals_scan_v1")
    os.environ.setdefault(
        "AUTONOMOUS_PLANNER_CREATE_PAYLOAD_JSON",
        '{"source":"autonomous-planner","collectors_enabled":true}',
    )

    planner = importlib.import_module("autonomous_planner")
    now = datetime.now(timezone.utc)
    policy = planner.PlannerPolicy.from_env()

    sample_state = {
        "captured_at": now.isoformat(),
        "pending_count": 0,
        "running_count": 0,
        "recent_total_runs": 12,
        "recent_failed_runs": 1,
        "execute_candidates": [
            {
                "task_id": "stale-task-123",
                "task_type": "jobs_digest_v1",
                "created_at": now.isoformat(),
                "attempts_used": 0,
                "max_attempts": 3,
            }
        ],
        "ai_usage_summary": {
            "cost_usd_total": 0.08,
            "total_tokens_sum": 4200,
        },
        "latest_system_health": {
            "cpu_percent": 18.5,
            "memory_percent": 42.0,
            "disk_percent": 36.1,
        },
    }

    decisions = planner.build_planner_decisions(sample_state, policy, now)
    execution_results = planner.execute_planner_decisions(
        decisions,
        now=now,
        policy=policy,
        execution_enabled=False,
        require_approval=True,
        approved=False,
    )

    print("planner_mode=recommendation")
    print(f"decision_count={len(decisions)}")
    print("decisions=")
    print(json.dumps(decisions, indent=2, sort_keys=True))
    print("execution_results=")
    print(json.dumps(execution_results, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
