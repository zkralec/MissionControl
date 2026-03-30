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


def test_jobs_collect_v1_accepts_breadth_controls() -> None:
    validate_payload(
        "jobs_collect_v1",
        {
            "request": {
                "query": "machine learning engineer",
                "locations": ["Remote", "New York, NY"],
                "sources": ["linkedin", "indeed"],
                "result_limit_per_source": 600,
                "max_total_jobs": 1500,
                "max_pages_per_source": 8,
                "max_queries_per_title_location_pair": 5,
                "max_queries_per_run": 12,
                "enable_query_expansion": True,
                "jobs_notification_cooldown_days": 7,
                "jobs_shortlist_repeat_penalty": 5,
                "resurface_seen_jobs": True,
                "early_stop_when_no_new_results": False,
            }
        },
    )


def test_openclaw_jobs_collect_v1_accepts_feature_gate_controls() -> None:
    validate_payload(
        "openclaw_jobs_collect_v1",
        {
            "request": {
                "query": "machine learning engineer",
                "locations": ["Remote"],
                "sources": ["handshake", "glassdoor"],
                "result_limit_per_source": 80,
                "minimum_raw_jobs_total": 40,
                "minimum_unique_jobs_total": 20,
                "openclaw_enabled": True,
                "openclaw_capture_screenshots": True,
                "openclaw_max_screenshots_per_source": 4,
                "openclaw_command_timeout_seconds": 120,
            }
        },
    )


def test_job_apply_prepare_resume_tailor_and_openclaw_apply_payloads_validate() -> None:
    validate_payload(
        "job_apply_prepare_v1",
        {
            "pipeline_id": "pipe-apply-1",
            "upstream": {"task_id": "task-shortlist", "run_id": "run-shortlist", "task_type": "jobs_shortlist_v1"},
            "request": {"notify_channels": ["discord"]},
            "selection": {"job_id": "job-123"},
            "prepare_policy": {"include_cover_letter": True, "enqueue_openclaw_apply": True},
        },
    )
    validate_payload(
        "resume_tailor_v1",
        {
            "pipeline_id": "pipe-apply-1",
            "upstream": {"task_id": "task-prepare", "run_id": "run-prepare", "task_type": "job_apply_prepare_v1"},
            "request": {"notify_channels": ["discord"]},
            "tailor_policy": {"include_cover_letter": True, "enqueue_openclaw_apply": True},
        },
    )
    validate_payload(
        "openclaw_apply_draft_v1",
        {
            "pipeline_id": "pipe-apply-1",
            "upstream": {"task_id": "task-tailor", "run_id": "run-tailor", "task_type": "resume_tailor_v1"},
            "request": {
                "openclaw_apply_enabled": True,
                "openclaw_apply_capture_screenshots": True,
                "openclaw_apply_max_screenshots": 6,
                "openclaw_apply_timeout_seconds": 300,
                "notify_channels": ["discord"],
            },
        },
    )
