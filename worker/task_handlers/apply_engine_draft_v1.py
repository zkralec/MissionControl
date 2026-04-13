"""
apply_engine_draft_v1 task handler.

Replaces openclaw_apply_draft_v1. Uses the new Playwright-native apply engine
instead of the OpenClaw external tool.

Input payload (from resume_tailor_v1 or job_apply_prepare_v1):
  - upstream_task_id: task ID of the resume_tailor_v1 result
  - application_url: direct URL to the job application (optional override)
  - headless: bool (default True)
  - enable_llm: bool (default False)

Output artifact:
  {
    "artifact_type": "job.apply.engine.draft.v1",
    "status": "draft_ready | partial | blocked | auth_required | failed",
    "review_reached": bool,
    "adapter_name": str,
    "site_name": str,
    "fields_filled_count": int,
    "fields_failed_count": int,
    "step_count": int,
    "screenshots": [...],
    "summary_path": str,
    "failure_reason": str | null,
    "llm_calls_used": int,
    "fields_manifest": [...],
    "notes": [...],
  }
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from application_draft_state import (
    build_application_identity,
    claim_application_draft_identity,
    record_application_draft_result,
)
from task_handlers.errors import NonRetryableTaskError
from task_handlers.jobs_pipeline_common import (
    expect_artifact_type,
    fetch_upstream_result_content_json,
    new_pipeline_id,
    payload_object,
    utc_iso,
)


_DEFAULT_OUTPUT_DIR = Path(os.environ.get("APPLY_ENGINE_OUTPUT_DIR", "/data/apply_engine_runs"))
_DEFAULT_PROFILE_PATH = Path(os.environ.get("APPLY_ENGINE_PROFILE_PATH", "/config/applicant_profile.yaml"))
_DEFAULT_AUTH_STATE = os.environ.get("APPLY_ENGINE_LINKEDIN_AUTH_STATE")


def execute(task: Any, db: Any) -> dict[str, Any]:
    """Synchronous task handler wrapper — runs the async apply engine in a new event loop."""
    payload = payload_object(task.payload_json)

    # Fetch upstream artifacts
    upstream_content = fetch_upstream_result_content_json(db, task)
    application_target = _extract_application_target(upstream_content)

    if not application_target:
        raise NonRetryableTaskError("apply_engine_draft_v1: no application_target found in upstream artifacts")

    application_url = (
        str(payload.get("application_url") or "").strip()
        or str(application_target.get("application_url") or application_target.get("source_url") or "").strip()
    )
    if not application_url:
        raise NonRetryableTaskError("apply_engine_draft_v1: no application_url available")

    # Deduplication — prevent running twice for the same job
    identity = build_application_identity(
        application_url=application_url,
        company=application_target.get("company"),
        job_id=application_target.get("job_id"),
    )
    claimed = claim_application_draft_identity(db, identity)
    if not claimed:
        return {
            "artifact_type": "job.apply.engine.draft.v1",
            "content_json": {
                "status": "already_in_progress",
                "message": "A draft run for this application is already claimed.",
                "application_url": application_url,
            },
            "next_tasks": [],
        }

    # Config from payload / environment
    headless = bool(payload.get("headless", True))
    enable_llm = bool(payload.get("enable_llm", False))
    profile_path = Path(str(payload.get("profile_path") or _DEFAULT_PROFILE_PATH))
    output_dir = Path(str(payload.get("output_dir") or _DEFAULT_OUTPUT_DIR))
    auth_state = str(payload.get("auth_state_path") or _DEFAULT_AUTH_STATE or "").strip() or None

    if not profile_path.exists():
        raise NonRetryableTaskError(
            f"apply_engine_draft_v1: profile not found at {profile_path}. "
            "Set APPLY_ENGINE_PROFILE_PATH or pass profile_path in payload."
        )

    # Run the apply engine (sync wrapper around async)
    try:
        result = asyncio.run(_run_apply(
            job_url=application_url,
            profile_path=profile_path,
            output_dir=output_dir,
            headless=headless,
            auth_state=auth_state,
            enable_llm=enable_llm,
        ))
    except Exception as exc:
        record_application_draft_result(
            db,
            identity=identity,
            status="failed",
            failure_category="engine_exception",
            blocking_reason=str(exc),
        )
        raise NonRetryableTaskError(f"apply_engine_draft_v1: engine exception: {exc}") from exc

    # Persist result to dedup state
    record_application_draft_result(
        db,
        identity=identity,
        status=result.status,
        failure_category=_map_failure_category(result.status, result.failure_reason),
        blocking_reason=result.failure_reason,
    )

    content = {
        "artifact_type": "job.apply.engine.draft.v1",
        **result.to_dict(),
        "application_target": application_target,
        "generated_at": utc_iso(),
    }

    next_tasks = []
    if result.status == "draft_ready" and result.review_reached:
        next_tasks = [_notify_task(task, application_target, result)]

    return {
        "artifact_type": "job.apply.engine.draft.v1",
        "content_json": content,
        "next_tasks": next_tasks,
        "debug_json": {
            "run_id": result.run_id,
            "fields_manifest": result.fields_manifest,
            "screenshots": result.screenshots,
        },
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _run_apply(
    job_url: str,
    profile_path: Path,
    output_dir: Path,
    headless: bool,
    auth_state: str | None,
    enable_llm: bool,
) -> Any:
    # Import here to avoid circular imports and keep startup fast
    from integrations.apply_engine.runner import ApplyConfig, run_apply

    config = ApplyConfig(
        job_url=job_url,
        profile_path=profile_path,
        output_dir=output_dir,
        headless=headless,
        storage_state_path=auth_state,
        enable_llm=enable_llm,
    )
    return await run_apply(config)


def _extract_application_target(upstream: dict[str, Any]) -> dict[str, Any] | None:
    """Pull application_target from whichever upstream artifact has it."""
    if not isinstance(upstream, dict):
        return None

    # Direct key (from job_apply_prepare_v1)
    if "application_target" in upstream:
        return upstream["application_target"]

    # Nested in content_json
    for key in ("content_json", "result"):
        nested = upstream.get(key)
        if isinstance(nested, dict) and "application_target" in nested:
            return nested["application_target"]

    return None


def _map_failure_category(status: str, failure_reason: str | None) -> str | None:
    reason = (failure_reason or "").lower()
    if status == "auth_required" or "login" in reason or "auth" in reason:
        return "login_required"
    if "captcha" in reason or "bot" in reason:
        return "captcha_or_bot_challenge"
    if "timeout" in reason or "timed out" in reason:
        return "timed_out"
    if "not found" in reason or "404" in reason:
        return "job_not_found"
    if status == "blocked":
        return "site_blocked"
    if status == "failed":
        return "engine_error"
    return None


def _notify_task(
    task: Any,
    application_target: dict[str, Any],
    result: Any,
) -> dict[str, Any]:
    title = str(application_target.get("title") or "Unknown role").strip()
    company = str(application_target.get("company") or "Unknown company").strip()
    pipeline_id = new_pipeline_id()

    return {
        "task_type": "notify_v1",
        "payload_json": {
            "pipeline_id": pipeline_id,
            "channels": ["jobs_apply_engine_v1"],
            "message": "\n".join([
                "Apply engine draft ready for review.",
                f"Title: {title}",
                f"Company: {company}",
                f"Status: {result.status}",
                f"Site: {result.site_name} ({result.adapter_name})",
                f"Fields filled: {result.fields_filled_count}",
                f"Screenshots: {len(result.screenshots)}",
                "Review required before submission.",
            ]),
            "severity": "info",
            "source_task_type": "apply_engine_draft_v1",
            "dedupe_key": f"jobapply:draft-ready:{pipeline_id}:{application_target.get('job_id', 'unknown')}",
            "metadata": {
                "pipeline_id": pipeline_id,
                "application_target": {
                    "job_id": application_target.get("job_id"),
                    "title": title,
                    "company": company,
                },
                "review_reached": result.review_reached,
                "submitted": False,
                "fields_filled_count": result.fields_filled_count,
                "screenshots_count": len(result.screenshots),
            },
            "include_header": True,
            "include_metadata": True,
        },
    }
