"""Verify planner runtime controls and template persistence."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    api_dir = repo_root / "api"
    if str(api_dir) not in sys.path:
        sys.path.insert(0, str(api_dir))

    default_db = repo_root / "task_run_history.sqlite3"
    os.environ.setdefault("PLANNER_CONTROL_DB_PATH", str(default_db))

    planner_control = importlib.import_module("planner_control")

    cfg = planner_control.get_planner_runtime_config()
    print("planner_enabled_before=", cfg.get("enabled"))

    updated = planner_control.update_planner_runtime_config(
        {
            "enabled": True,
            "execution_enabled": False,
            "require_approval": True,
            "approved": False,
            "interval_sec": 300,
            "max_create_per_cycle": 1,
        },
        updated_by="verify-script",
    )
    print("planner_enabled_after=", updated.get("enabled"))
    print("planner_mode_after=", "execute" if updated.get("execution_enabled") else "recommendation")

    preset = planner_control.ensure_rtx5090_deals_template(
        interval_seconds=300,
        gpu_max_price=2000,
        pc_max_price=4000,
        enabled=True,
    )
    print("preset_id=", preset.get("id"))
    print("preset_task_type=", preset.get("task_type"))
    print("preset_interval_seconds=", preset.get("min_interval_seconds"))

    rows = planner_control.list_planner_task_templates(limit=5)
    print("template_rows=", len(rows))
    if rows:
        print("latest_template=", rows[0].get("id"), rows[0].get("name"), rows[0].get("enabled"))


if __name__ == "__main__":
    main()
