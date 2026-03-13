"""Regression tests for payload schema resolution and notify_v1 compatibility."""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.schema_validate import _schema_dirs, validate_payload


def test_schema_dirs_prioritize_worker_task_payloads() -> None:
    dirs = _schema_dirs()
    assert dirs, "schema directory list must not be empty"
    first = dirs[0]
    assert str(first).endswith("/worker/schemas/task_payloads")


def test_notify_v1_accepts_disable_dedupe_flag() -> None:
    validate_payload(
        "notify_v1",
        {
            "channels": ["discord"],
            "message": "schema acceptance",
            "source_task_type": "ops_report_v1",
            "disable_dedupe": True,
        },
    )

