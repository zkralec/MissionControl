from __future__ import annotations

from typing import Any

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
    cover_letter_artifact = (
        upstream_result.get("cover_letter_artifact")
        if isinstance(upstream_result.get("cover_letter_artifact"), dict)
        else {}
    )
    application_url = str(application_target.get("application_url") or application_target.get("source_url") or "").strip()
    if not application_url:
        raise NonRetryableTaskError("openclaw_apply_draft_v1 requires a direct application URL from the shortlisted job")

    result = run_openclaw_apply_draft(
        application_target=application_target,
        resume_variant=resume_variant,
        answer_drafts=application_answers_artifact.get("items") if isinstance(application_answers_artifact.get("items"), list) else [],
        request=request,
        cover_letter_text=str(cover_letter_artifact.get("text") or "").strip(),
    )

    status = str(result.get("status") or "upstream_failure").strip() or "upstream_failure"
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    fields_filled_manifest = meta.get("fields_filled_manifest") if isinstance(meta.get("fields_filled_manifest"), list) else []
    screenshots = meta.get("screenshots") if isinstance(meta.get("screenshots"), list) else []
    awaiting_review = bool(meta.get("awaiting_review", status == "awaiting_review"))
    blocking_reason = str(meta.get("blocking_reason") or "").strip() or None
    failure_category = str(meta.get("failure_category") or "").strip() or None

    notify_channels = _as_text_list(draft_policy.get("notify_channels")) or _as_text_list(request.get("notify_channels")) or ["discord"]
    next_tasks: list[dict[str, Any]] = []
    notify_decision = {
        "should_notify": awaiting_review,
        "reason": "draft_ready_for_review" if awaiting_review else status,
        "channels": notify_channels if awaiting_review else [],
    }
    if awaiting_review:
        next_tasks.append(
            {
                "task_type": "notify_v1",
                "payload_json": _draft_notify_payload(
                    pipeline_id=pipeline_id,
                    task=task,
                    application_target=application_target,
                    status=status,
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
        "draft_status": status,
        "source_status": status,
        "failure_category": failure_category,
        "blocking_reason": blocking_reason,
        "awaiting_review": awaiting_review,
        "review_status": "awaiting_review" if awaiting_review else status,
        "submitted": False,
        "account_created_flag": bool(meta.get("account_created", False)),
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
        "checkpoint_urls": meta.get("checkpoint_urls") if isinstance(meta.get("checkpoint_urls"), list) else [],
        "page_title": meta.get("page_title"),
        "warnings": result.get("warnings") if isinstance(result.get("warnings"), list) else [],
        "errors": result.get("errors") if isinstance(result.get("errors"), list) else [],
        "notify_decision": notify_decision,
        "upstream": upstream,
    }

    return {
        "artifact_type": "openclaw.apply.draft.v1",
        "content_json": artifact,
        "next_tasks": next_tasks,
        "debug_json": {
            "draft_status": status,
            "failure_category": failure_category,
            "awaiting_review": awaiting_review,
            "submitted": False,
            "fields_filled_count": len(fields_filled_manifest),
            "screenshots_count": len(screenshots),
            "notify_decision": notify_decision,
        },
    }
