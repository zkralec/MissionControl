from __future__ import annotations

from typing import Any

from application_draft_state import (
    build_application_identity,
    claim_application_draft_identity,
    record_application_draft_result,
)
from integrations.openclaw_apply_draft import (
    openclaw_apply_command_configured,
    openclaw_apply_enabled,
    run_openclaw_apply_draft,
)
from task_handlers.errors import NonRetryableTaskError
from task_handlers.jobs_pipeline_common import (
    expect_artifact_type,
    fetch_upstream_result_content_json,
    new_pipeline_id,
    payload_object,
    utc_iso,
)


def _as_text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _meaningful_draft_status(value: Any) -> bool:
    return str(value or "").strip().lower() in {"draft_ready", "partial_draft"}


def _safe_auto_submit_signal(meta: dict[str, Any]) -> bool:
    page_diagnostics = meta.get("page_diagnostics") if isinstance(meta.get("page_diagnostics"), dict) else {}
    form_diagnostics = meta.get("form_diagnostics") if isinstance(meta.get("form_diagnostics"), dict) else {}
    auto_submit_allowed = bool(page_diagnostics.get("auto_submit_allowed") or form_diagnostics.get("auto_submit_allowed"))
    auto_submit_succeeded = bool(page_diagnostics.get("auto_submit_succeeded") or form_diagnostics.get("auto_submit_succeeded"))
    return bool(meta.get("submitted", False)) and auto_submit_allowed and auto_submit_succeeded


def _recover_progress_diagnostics(meta: dict[str, Any], debug_json: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    page_diagnostics = meta.get("page_diagnostics") if isinstance(meta.get("page_diagnostics"), dict) else {}
    form_diagnostics = meta.get("form_diagnostics") if isinstance(meta.get("form_diagnostics"), dict) else {}
    if page_diagnostics or form_diagnostics:
        return page_diagnostics, form_diagnostics
    draft_progress = debug_json.get("draft_progress") if isinstance(debug_json.get("draft_progress"), dict) else {}
    recovered_page = draft_progress.get("page_diagnostics") if isinstance(draft_progress.get("page_diagnostics"), dict) else {}
    recovered_form = draft_progress.get("form_diagnostics") if isinstance(draft_progress.get("form_diagnostics"), dict) else {}
    return recovered_page, recovered_form


def _draft_notify_payload(
    *,
    pipeline_id: str,
    task: Any,
    application_target: dict[str, Any],
    status: str,
    fields_filled_count: int,
    screenshots_count: int,
    channels: list[str],
) -> dict[str, Any]:
    title = str(application_target.get("title") or "Unknown role").strip() or "Unknown role"
    company = str(application_target.get("company") or "Unknown company").strip() or "Unknown company"
    source = str(application_target.get("source") or "unknown").strip() or "unknown"
    application_url = str(application_target.get("application_url") or application_target.get("source_url") or "").strip()
    message_lines = [
        "Application draft ready for human review.",
        f"Title: {title}",
        f"Company: {company}",
        f"Source: {source}",
        f"Status: {status}",
        f"Fields filled: {fields_filled_count}",
        f"Screenshots captured: {screenshots_count}",
        "Submission: not attempted",
    ]
    if application_url:
        message_lines.append(f"Application URL: <{application_url}>")

    return {
        "channels": channels,
        "message": "\n".join(message_lines),
        "severity": "info",
        "source_task_type": "openclaw_apply_draft_v1",
        "dedupe_key": (
            f"jobapply:draft-ready:{pipeline_id}:{str(application_target.get('job_id') or 'unknown')}:{str(getattr(task, '_run_id', '') or 'unknown')}"
        ),
        "metadata": {
            "pipeline_id": pipeline_id,
            "application_target": {
                "job_id": application_target.get("job_id"),
                "title": title,
                "company": company,
                "source": source,
            },
            "awaiting_review": True,
            "submitted": False,
            "fields_filled_count": fields_filled_count,
            "screenshots_count": screenshots_count,
        },
        "include_header": True,
        "include_metadata": True,
    }


def _submitted_notify_payload(
    *,
    pipeline_id: str,
    task: Any,
    application_target: dict[str, Any],
    page_diagnostics: dict[str, Any],
    form_diagnostics: dict[str, Any],
    channels: list[str],
) -> dict[str, Any]:
    title = str(application_target.get("title") or "Unknown role").strip() or "Unknown role"
    company = str(application_target.get("company") or "Unknown company").strip() or "Unknown company"
    auto_submit_succeeded = bool(page_diagnostics.get("auto_submit_succeeded") or form_diagnostics.get("auto_submit_succeeded"))
    confidence = str(
        page_diagnostics.get("submit_confidence")
        or form_diagnostics.get("submit_confidence")
        or page_diagnostics.get("overall_submit_confidence")
        or form_diagnostics.get("overall_submit_confidence")
        or ("high" if auto_submit_succeeded else "unknown")
    ).strip() or ("high" if auto_submit_succeeded else "unknown")
    fallback_answers = page_diagnostics.get("fallback_answers_used")
    if not isinstance(fallback_answers, list):
        fallback_answers = form_diagnostics.get("fallback_answers_used")
    notable_fallbacks = [
        str(row.get("label") or row.get("canonical_key") or "").strip()
        for row in (fallback_answers or [])
        if isinstance(row, dict) and str(row.get("label") or row.get("canonical_key") or "").strip()
    ][:3]
    message_lines = [
        f"Company: {company}",
        f"Title: {title}",
        "Status: submitted",
        f"Confidence: {confidence}",
    ]
    if notable_fallbacks:
        message_lines.append(f"Fallbacks: {', '.join(notable_fallbacks)}")

    return {
        "channels": channels,
        "message": "\n".join(message_lines),
        "severity": "info",
        "source_task_type": "openclaw_apply_draft_v1",
        "dedupe_key": (
            f"jobapply:submitted:{pipeline_id}:{str(application_target.get('job_id') or 'unknown')}:{str(getattr(task, '_run_id', '') or 'unknown')}"
        ),
        "metadata": {
            "pipeline_id": pipeline_id,
            "application_target": {
                "job_id": application_target.get("job_id"),
                "title": title,
                "company": company,
            },
            "awaiting_review": False,
            "submitted": True,
            "submit_confidence": confidence,
            "fallback_answers_used": notable_fallbacks,
        },
        "include_header": False,
        "include_metadata": False,
    }


def _sanitize_runner_result(
    *,
    result: dict[str, Any],
    default_status: str,
) -> dict[str, Any]:
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    warnings = result.get("warnings") if isinstance(result.get("warnings"), list) else []
    errors = result.get("errors") if isinstance(result.get("errors"), list) else []
    draft_status = str(meta.get("draft_status") or default_status).strip() or default_status
    source_status = str(meta.get("source_status") or draft_status or default_status).strip() or default_status
    review_status = str(meta.get("review_status") or "blocked").strip() or "blocked"
    fields_filled_manifest = meta.get("fields_filled_manifest") if isinstance(meta.get("fields_filled_manifest"), list) else []
    screenshots = meta.get("screenshots") if isinstance(meta.get("screenshots"), list) else []
    checkpoint_urls = meta.get("checkpoint_urls") if isinstance(meta.get("checkpoint_urls"), list) else []
    blocking_reason = str(meta.get("blocking_reason") or "").strip() or None
    failure_category = str(meta.get("failure_category") or "").strip() or None
    submitted = bool(meta.get("submitted", False))
    safe_submitted = _safe_auto_submit_signal(meta)
    awaiting_review_requested = bool(meta.get("awaiting_review", default_status == "awaiting_review"))
    meta_notify_decision = meta.get("notify_decision") if isinstance(meta.get("notify_decision"), dict) else {}
    debug_json = meta.get("debug_json") if isinstance(meta.get("debug_json"), dict) else {}
    page_diagnostics, form_diagnostics = _recover_progress_diagnostics(meta, debug_json)

    if submitted and not safe_submitted:
        warnings = list(warnings) + ["worker_level_unsafe_submit_guard_triggered"]
        errors = list(errors) + ["unsafe_submit_attempted"]
        draft_status = "partial_draft" if fields_filled_manifest else "not_started"
        source_status = "unsafe_submit_attempted"
        review_status = "blocked"
        awaiting_review_requested = False
        failure_category = "unsafe_submit_attempted"
        blocking_reason = "Worker-level no-submit guard blocked a reported submit signal."

    review_ready = (
        _meaningful_draft_status(draft_status)
        and len(fields_filled_manifest) > 0
        and len(screenshots) > 0
        and len(checkpoint_urls) > 0
        and not submitted
    )
    awaiting_review = awaiting_review_requested and review_ready
    if not review_ready and (awaiting_review_requested or _meaningful_draft_status(draft_status)):
        missing_parts: list[str] = []
        if not _meaningful_draft_status(draft_status):
            missing_parts.append("draft_status")
        if not fields_filled_manifest:
            missing_parts.append("fields_filled_manifest")
        if not screenshots:
            missing_parts.append("screenshot_metadata_references")
        if not checkpoint_urls:
            missing_parts.append("checkpoint_urls")
        warnings = list(warnings) + ["worker_review_ready_validation_failed"]
        errors = list(errors) + ["incomplete_review_artifacts"]
        failure_category = failure_category or "manual_review_required"
        source_status = "manual_review_required" if source_status in {"success", "awaiting_review"} else source_status
        review_status = "blocked"
        awaiting_review = False
        if draft_status == "draft_ready":
            draft_status = "partial_draft"
        blocking_reason = blocking_reason or (
            "Runner output is missing required review metadata: " + ", ".join(missing_parts or ["review_ready_state"])
        )

    should_notify = awaiting_review and not submitted and len(screenshots) > 0 and bool(meta_notify_decision.get("should_notify", True))
    notify_decision = {
        "should_notify": should_notify,
        "reason": str(
            meta_notify_decision.get("reason")
            or ("application_submitted" if safe_submitted else ("draft_ready_for_review" if should_notify else review_status))
        ).strip()
        or ("application_submitted" if safe_submitted else ("draft_ready_for_review" if should_notify else review_status)),
        "channels": [],
    }

    return {
        "draft_status": draft_status,
        "source_status": source_status,
        "review_status": "submitted" if safe_submitted else ("awaiting_review" if awaiting_review else review_status),
        "awaiting_review": awaiting_review,
        "failure_category": failure_category,
        "blocking_reason": blocking_reason,
        "submitted": safe_submitted,
        "fields_filled_manifest": fields_filled_manifest,
        "screenshots": screenshots,
        "checkpoint_urls": checkpoint_urls,
        "page_title": meta.get("page_title"),
        "warnings": warnings,
        "errors": errors,
        "notify_decision": notify_decision,
        "account_created": bool(meta.get("account_created", False)),
        "page_diagnostics": page_diagnostics,
        "form_diagnostics": form_diagnostics,
        "inspect_only": bool(meta.get("inspect_only", False)),
        "debug_json": debug_json,
    }


def execute(task: Any, db: Any) -> dict[str, Any]:
    payload = payload_object(task.payload_json)
    upstream = payload.get("upstream") if isinstance(payload.get("upstream"), dict) else {}
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    draft_policy = payload.get("draft_policy") if isinstance(payload.get("draft_policy"), dict) else {}
    pipeline_id = new_pipeline_id(payload.get("pipeline_id"))

    if not openclaw_apply_enabled(request):
        raise NonRetryableTaskError(
            "openclaw_apply_draft_v1 is disabled. Set OPENCLAW_APPLY_DRAFT_ENABLED=true or request.openclaw_apply_enabled=true."
        )
    if not openclaw_apply_command_configured():
        raise NonRetryableTaskError(
            "openclaw_apply_draft_v1 requires OPENCLAW_APPLY_DRAFT_COMMAND to be configured."
        )

    upstream_result = fetch_upstream_result_content_json(db, upstream)
    expect_artifact_type(upstream_result, "resume.tailor.v1")

    application_target = (
        upstream_result.get("application_target")
        if isinstance(upstream_result.get("application_target"), dict)
        else {}
    )
    resume_variant = (
        upstream_result.get("resume_variant_artifact")
        if isinstance(upstream_result.get("resume_variant_artifact"), dict)
        else {}
    )
    application_answers_artifact = (
        upstream_result.get("application_answers_artifact")
        if isinstance(upstream_result.get("application_answers_artifact"), dict)
        else {}
    )
    candidate_profile = (
        upstream_result.get("candidate_profile")
        if isinstance(upstream_result.get("candidate_profile"), dict)
        else {}
    )
    cover_letter_artifact = (
        upstream_result.get("cover_letter_artifact")
        if isinstance(upstream_result.get("cover_letter_artifact"), dict)
        else {}
    )
    force_redraft = bool(draft_policy.get("force_redraft", request.get("force_redraft", False)))
    application_url = str(application_target.get("application_url") or application_target.get("source_url") or "").strip()
    if not application_url:
        raise NonRetryableTaskError("openclaw_apply_draft_v1 requires a direct application URL from the selected job")

    application_identity = build_application_identity(application_target)
    claimed, existing_identity = claim_application_draft_identity(
        application_identity,
        task_id=str(getattr(task, "id", "") or ""),
        run_id=str(getattr(task, "_run_id", "") or ""),
        pipeline_id=pipeline_id,
        force=force_redraft,
    )
    if not claimed:
        existing_identity = existing_identity or {}
        status = "manual_review_required"
        draft_status = "not_started"
        source_status = "manual_review_required"
        review_status = "blocked"
        awaiting_review = False
        blocking_reason = (
            "Duplicate application draft prevented for identity "
            f"{application_identity['identity_key']} (last_task_id={existing_identity.get('last_task_id') or 'unknown'})."
        )
        notify_decision = {"should_notify": False, "reason": "duplicate_application_identity", "channels": []}
        artifact = {
            "artifact_type": "openclaw.apply.draft.v1",
            "artifact_schema": "openclaw.apply.draft.v1",
            "pipeline_id": pipeline_id,
            "generated_at": utc_iso(),
            "request": request,
            "draft_policy": draft_policy,
            "application_target_metadata": application_target,
            "application_identity": application_identity,
            "draft_status": draft_status,
            "source_status": source_status,
            "failure_category": "manual_review_required",
            "blocking_reason": blocking_reason,
            "awaiting_review": awaiting_review,
            "review_status": review_status,
            "submitted": False,
            "account_created_flag": False,
            "fields_filled_manifest": [],
            "screenshot_metadata_references": [],
            "resume_variant_used": {
                "resume_variant_name": resume_variant.get("resume_variant_name"),
                "resume_file_name": resume_variant.get("resume_file_name"),
                "base_resume_name": resume_variant.get("base_resume_name"),
                "base_resume_sha256": resume_variant.get("base_resume_sha256"),
            },
            "answer_drafts_used": application_answers_artifact.get("items") if isinstance(application_answers_artifact.get("items"), list) else [],
            "cover_letter_draft_used": cover_letter_artifact.get("text"),
            "checkpoint_urls": [],
            "page_title": None,
            "warnings": ["duplicate_application_identity_blocked"],
            "errors": [],
            "notify_decision": notify_decision,
            "upstream": upstream,
        }
        record_application_draft_result(
            application_identity,
            task_id=str(getattr(task, "id", "") or ""),
            run_id=str(getattr(task, "_run_id", "") or ""),
            pipeline_id=pipeline_id,
            draft_status=draft_status,
            source_status=source_status,
            review_status=review_status,
            awaiting_review=awaiting_review,
            submitted=False,
            failure_category="manual_review_required",
            blocking_reason=blocking_reason,
            state_json={"duplicate_blocked": True, "existing_identity": existing_identity},
        )
        return {
            "artifact_type": "openclaw.apply.draft.v1",
            "content_json": artifact,
            "next_tasks": [],
        "debug_json": {
            "draft_status": draft_status,
            "source_status": source_status,
            "failure_category": "manual_review_required",
            "awaiting_review": awaiting_review,
            "submitted": False,
            "fields_filled_count": 0,
            "screenshots_count": 0,
            "notify_decision": notify_decision,
            "runner_debug": {},
        },
    }

    result = run_openclaw_apply_draft(
        application_target=application_target,
        resume_variant=resume_variant,
        candidate_profile=candidate_profile,
        answer_drafts=application_answers_artifact.get("items") if isinstance(application_answers_artifact.get("items"), list) else [],
        request=request,
        cover_letter_text=str(cover_letter_artifact.get("text") or "").strip(),
        lineage={
            "pipeline_id": pipeline_id,
            "task_id": str(getattr(task, "id", "") or ""),
            "run_id": str(getattr(task, "_run_id", "") or ""),
            "upstream": upstream,
        },
    )

    status = str(result.get("status") or "upstream_failure").strip() or "upstream_failure"
    sanitized = _sanitize_runner_result(result=result, default_status=status)
    draft_status = sanitized["draft_status"]
    source_status = sanitized["source_status"]
    fields_filled_manifest = sanitized["fields_filled_manifest"]
    screenshots = sanitized["screenshots"]
    awaiting_review = sanitized["awaiting_review"]
    review_status = sanitized["review_status"]
    blocking_reason = sanitized["blocking_reason"]
    failure_category = sanitized["failure_category"]
    submitted = sanitized["submitted"]

    notify_channels = _as_text_list(draft_policy.get("notify_channels")) or _as_text_list(request.get("notify_channels")) or ["discord"]
    next_tasks: list[dict[str, Any]] = []
    notify_decision = {
        "should_notify": (
            bool(sanitized["notify_decision"]["should_notify"]) and len(screenshots) > 0 and not submitted
        ) or submitted,
        "reason": str(
            sanitized["notify_decision"]["reason"]
            or ("application_submitted" if submitted else ("draft_ready_for_review" if awaiting_review else review_status))
        ).strip()
        or ("application_submitted" if submitted else ("draft_ready_for_review" if awaiting_review else review_status)),
        "channels": notify_channels if (submitted or (awaiting_review and len(screenshots) > 0 and not submitted)) else [],
    }
    if submitted:
        next_tasks.append(
            {
                "task_type": "notify_v1",
                "payload_json": _submitted_notify_payload(
                    pipeline_id=pipeline_id,
                    task=task,
                    application_target=application_target,
                    page_diagnostics=sanitized["page_diagnostics"],
                    form_diagnostics=sanitized["form_diagnostics"],
                    channels=notify_channels,
                ),
            }
        )
    elif notify_decision["should_notify"]:
        next_tasks.append(
            {
                "task_type": "notify_v1",
                "payload_json": _draft_notify_payload(
                    pipeline_id=pipeline_id,
                    task=task,
                    application_target=application_target,
                    status=draft_status,
                    fields_filled_count=len(fields_filled_manifest),
                    screenshots_count=len(screenshots),
                    channels=notify_channels,
                ),
            }
        )

    artifact = {
        "artifact_type": "openclaw.apply.draft.v1",
        "artifact_schema": "openclaw.apply.draft.v1",
        "pipeline_id": pipeline_id,
        "generated_at": utc_iso(),
        "request": request,
        "draft_policy": draft_policy,
        "application_target_metadata": application_target,
        "application_identity": application_identity,
        "draft_status": draft_status,
        "source_status": source_status,
        "failure_category": failure_category,
        "blocking_reason": blocking_reason,
        "awaiting_review": awaiting_review,
        "review_status": review_status,
        "submitted": submitted,
        "account_created_flag": bool(sanitized["account_created"]),
        "fields_filled_manifest": fields_filled_manifest,
        "screenshot_metadata_references": screenshots,
        "resume_variant_used": {
            "resume_variant_name": resume_variant.get("resume_variant_name"),
            "resume_file_name": resume_variant.get("resume_file_name"),
            "base_resume_name": resume_variant.get("base_resume_name"),
            "base_resume_sha256": resume_variant.get("base_resume_sha256"),
        },
        "answer_drafts_used": application_answers_artifact.get("items") if isinstance(application_answers_artifact.get("items"), list) else [],
        "cover_letter_draft_used": cover_letter_artifact.get("text"),
        "checkpoint_urls": sanitized["checkpoint_urls"],
        "page_title": sanitized["page_title"],
        "warnings": sanitized["warnings"],
        "errors": sanitized["errors"],
        "notify_decision": notify_decision,
        "page_diagnostics": sanitized["page_diagnostics"],
        "form_diagnostics": sanitized["form_diagnostics"],
        "inspect_only": sanitized["inspect_only"],
        "upstream": upstream,
    }

    record_application_draft_result(
        application_identity,
        task_id=str(getattr(task, "id", "") or ""),
        run_id=str(getattr(task, "_run_id", "") or ""),
        pipeline_id=pipeline_id,
        draft_status=draft_status,
        source_status=source_status,
        review_status=review_status,
        awaiting_review=awaiting_review,
        submitted=submitted,
        failure_category=failure_category,
        blocking_reason=blocking_reason,
        state_json={
            "fields_filled_count": len(fields_filled_manifest),
            "screenshots_count": len(screenshots),
            "notify_decision": notify_decision,
            "inspect_only": sanitized["inspect_only"],
        },
    )

    return {
        "artifact_type": "openclaw.apply.draft.v1",
        "content_json": artifact,
        "next_tasks": next_tasks,
        "debug_json": {
            "draft_status": draft_status,
            "source_status": source_status,
            "failure_category": failure_category,
            "awaiting_review": awaiting_review,
            "submitted": submitted,
            "fields_filled_count": len(fields_filled_manifest),
            "screenshots_count": len(screenshots),
            "notify_decision": notify_decision,
            "runner_debug": sanitized["debug_json"],
        },
    }
