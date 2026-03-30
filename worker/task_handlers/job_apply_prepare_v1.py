from __future__ import annotations

import re
from typing import Any

from task_handlers.errors import NonRetryableTaskError
from task_handlers.jobs_pipeline_common import (
    build_upstream_ref,
    expect_artifact_type,
    fetch_upstream_result_content_json,
    new_pipeline_id,
    payload_object,
    resolve_profile_context,
    stage_idempotency_key,
    utc_iso,
)

_REQUIREMENT_HINTS = (
    "experience",
    "required",
    "must",
    "should",
    "preferred",
    "proficient",
    "familiar",
    "background",
    "knowledge",
    "degree",
    "python",
    "machine learning",
    "ml",
    "sql",
    "cloud",
    "distributed",
    "communication",
)


def _extract_shortlist_jobs(upstream_result: dict[str, Any]) -> list[dict[str, Any]]:
    artifact_type = str(upstream_result.get("artifact_type") or "").strip()
    if artifact_type == "jobs.shortlist.v1":
        shortlist = upstream_result.get("shortlist")
        if isinstance(shortlist, list):
            return [row for row in shortlist if isinstance(row, dict)]
        jobs_top = upstream_result.get("jobs_top_artifact")
        if isinstance(jobs_top, dict):
            top_jobs = jobs_top.get("top_jobs")
            if isinstance(top_jobs, list):
                return [row for row in top_jobs if isinstance(row, dict)]
    if artifact_type == "jobs_top.v1":
        top_jobs = upstream_result.get("top_jobs")
        if isinstance(top_jobs, list):
            return [row for row in top_jobs if isinstance(row, dict)]
    raise NonRetryableTaskError(
        "upstream contract mismatch: job_apply_prepare_v1 expects artifact_type 'jobs.shortlist.v1' or 'jobs_top.v1'"
    )


def _selected_job(jobs: list[dict[str, Any]], selection: dict[str, Any]) -> dict[str, Any]:
    if not jobs:
        raise NonRetryableTaskError("job_apply_prepare_v1 requires at least one shortlisted job")

    requested_job_id = str(selection.get("job_id") or "").strip()
    if requested_job_id:
        for row in jobs:
            if str(row.get("job_id") or row.get("normalized_job_id") or "").strip() == requested_job_id:
                return dict(row)
        raise NonRetryableTaskError(f"job_apply_prepare_v1 could not find shortlisted job_id '{requested_job_id}'")

    shortlist_index = selection.get("shortlist_index")
    if shortlist_index is not None:
        try:
            index = int(shortlist_index)
        except (TypeError, ValueError) as exc:
            raise NonRetryableTaskError("selection.shortlist_index must be an integer") from exc
        if index < 0 or index >= len(jobs):
            raise NonRetryableTaskError("selection.shortlist_index is out of range for the shortlist")
        return dict(jobs[index])

    return dict(jobs[0])


def _text_lines(*values: Any) -> list[str]:
    lines: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        pieces = re.split(r"(?:\n+|•|·|;)", text)
        for piece in pieces:
            normalized = " ".join(piece.split())
            if normalized:
                lines.append(normalized)
    return lines


def _extract_requirements(job: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in _text_lines(job.get("description"), job.get("description_snippet"), job.get("summary"), job.get("title")):
        low = line.lower()
        if len(line) < 12:
            continue
        if not any(hint in low for hint in _REQUIREMENT_HINTS):
            continue
        key = low[:180]
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            {
                "requirement": line[:240],
                "source": "job_text",
                "confidence": "explicit",
            }
        )
        if len(candidates) >= 8:
            break

    if not candidates:
        title = str(job.get("title") or "").strip()
        work_mode = str(job.get("work_mode") or "").strip()
        location = str(job.get("location") or "").strip()
        if title:
            candidates.append(
                {
                    "requirement": f"Demonstrated fit for the '{title}' role scope.",
                    "source": "derived",
                    "confidence": "inferred",
                }
            )
        if work_mode:
            candidates.append(
                {
                    "requirement": f"Comfort with {work_mode} collaboration expectations.",
                    "source": "derived",
                    "confidence": "inferred",
                }
            )
        if location:
            candidates.append(
                {
                    "requirement": f"Location alignment for {location}.",
                    "source": "derived",
                    "confidence": "inferred",
                }
            )

    return candidates


def _common_questions(job: dict[str, Any]) -> list[dict[str, Any]]:
    title = str(job.get("title") or "this role").strip() or "this role"
    company = str(job.get("company") or "the company").strip() or "the company"
    return [
        {
            "question": f"Why are you interested in the {title} role at {company}?",
            "answer_type": "motivation",
        },
        {
            "question": f"What relevant experience makes you a strong fit for {title}?",
            "answer_type": "experience",
        },
        {
            "question": "Describe a project or accomplishment that matches the role requirements.",
            "answer_type": "impact",
        },
    ]


def execute(task: Any, db: Any) -> dict[str, Any]:
    payload = payload_object(task.payload_json)
    upstream = payload.get("upstream") if isinstance(payload.get("upstream"), dict) else {}
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    selection = payload.get("selection") if isinstance(payload.get("selection"), dict) else {}
    prepare_policy = payload.get("prepare_policy") if isinstance(payload.get("prepare_policy"), dict) else {}
    pipeline_id = new_pipeline_id(payload.get("pipeline_id"))

    upstream_result = fetch_upstream_result_content_json(db, upstream)
    shortlist_jobs = _extract_shortlist_jobs(upstream_result)
    job = _selected_job(shortlist_jobs, selection)

    request_with_profile = dict(request)
    request_with_profile["profile_mode"] = "resume_profile"
    profile_context = resolve_profile_context(request_with_profile)
    if not bool(profile_context.get("applied")) or not str(profile_context.get("resume_text") or "").strip():
        raise NonRetryableTaskError("job_apply_prepare_v1 requires a stored Mission Control resume profile")

    requirements = _extract_requirements(job)
    common_questions = _common_questions(job)
    include_cover_letter = bool(prepare_policy.get("include_cover_letter", True))

    application_target = {
        "job_id": job.get("job_id") or job.get("normalized_job_id"),
        "title": job.get("title"),
        "company": job.get("company"),
        "location": job.get("location"),
        "location_normalized": job.get("location_normalized"),
        "source": job.get("source"),
        "source_url": job.get("source_url"),
        "application_url": job.get("url") or job.get("source_url"),
        "work_mode": job.get("work_mode"),
        "posted_at_normalized": job.get("posted_at_normalized") or job.get("posted_at"),
        "posted_age_days": job.get("posted_age_days"),
        "salary_text": job.get("salary_text"),
        "salary_min": job.get("salary_min"),
        "salary_max": job.get("salary_max"),
        "description_snippet": job.get("description_snippet") or job.get("summary"),
    }

    artifact = {
        "artifact_type": "job.apply.prepare.v1",
        "artifact_schema": "job.apply.prepare.v1",
        "pipeline_id": pipeline_id,
        "prepared_at": utc_iso(),
        "request": request,
        "selection": selection,
        "prepare_policy": {
            "include_cover_letter": include_cover_letter,
        },
        "application_target": application_target,
        "candidate_profile": {
            "resume_source": profile_context.get("source"),
            "resume_name": profile_context.get("resume_name"),
            "resume_sha256": profile_context.get("resume_sha256"),
            "resume_char_count": profile_context.get("resume_char_count"),
        },
        "extracted_requirements": requirements,
        "common_questions": common_questions,
        "requirements_summary": [row.get("requirement") for row in requirements[:5]],
        "awaiting_review": False,
        "review_status": "preparing_materials",
        "upstream": upstream,
    }

    next_payload = {
        "pipeline_id": pipeline_id,
        "upstream": build_upstream_ref(task, "job_apply_prepare_v1"),
        "request": request,
        "tailor_policy": {
            "include_cover_letter": include_cover_letter,
            "enqueue_openclaw_apply": bool(prepare_policy.get("enqueue_openclaw_apply", True)),
        },
        "lineage": payload.get("lineage") if isinstance(payload.get("lineage"), dict) else {},
    }
    next_task = {
        "task_type": "resume_tailor_v1",
        "payload_json": next_payload,
        "idempotency_key": stage_idempotency_key(
            pipeline_id,
            "resume_tailor_v1",
            str(getattr(task, "_run_id", "") or ""),
            prefix="jobapply",
        ),
    }

    return {
        "artifact_type": "job.apply.prepare.v1",
        "content_json": artifact,
        "next_tasks": [next_task],
        "debug_json": {
            "selected_job_id": application_target.get("job_id"),
            "requirements_count": len(requirements),
            "common_questions_count": len(common_questions),
            "resume_profile_loaded": True,
        },
    }
