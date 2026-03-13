from __future__ import annotations

from typing import Any

from task_handlers.errors import NonRetryableTaskError
from task_handlers.jobs_pipeline_common import (
    build_upstream_ref,
    fetch_upstream_result_content_json,
    new_pipeline_id,
    payload_object,
    resolve_request,
    stage_idempotency_key,
    utc_iso,
)
from task_handlers.jobs_shortlist_helpers import (
    normalize_scored_jobs,
    resolve_min_score_100,
    shortlist_jobs,
)


def _extract_scored_jobs(upstream_result: dict[str, Any]) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    artifact_type = str(upstream_result.get("artifact_type") or "").strip()
    if artifact_type == "jobs.rank.v1":
        pipeline_counts = upstream_result.get("pipeline_counts") if isinstance(upstream_result.get("pipeline_counts"), dict) else {}
        jobs_scored_artifact = upstream_result.get("jobs_scored_artifact")
        if isinstance(jobs_scored_artifact, dict):
            if isinstance(jobs_scored_artifact.get("pipeline_counts"), dict):
                pipeline_counts = jobs_scored_artifact.get("pipeline_counts")
            rows = jobs_scored_artifact.get("jobs_scored")
            if isinstance(rows, list):
                return artifact_type, rows, pipeline_counts
        rows = upstream_result.get("ranked_jobs")
        if isinstance(rows, list):
            return artifact_type, rows, pipeline_counts
        return artifact_type, [], pipeline_counts

    if artifact_type == "jobs_scored.v1":
        pipeline_counts = upstream_result.get("pipeline_counts") if isinstance(upstream_result.get("pipeline_counts"), dict) else {}
        rows = upstream_result.get("jobs_scored")
        if isinstance(rows, list):
            return artifact_type, rows, pipeline_counts
        return artifact_type, [], pipeline_counts

    raise NonRetryableTaskError(
        "upstream contract mismatch: jobs_shortlist_v1 expects artifact_type "
        "'jobs.rank.v1' or 'jobs_scored.v1'"
    )


def execute(task: Any, db: Any) -> dict[str, Any]:
    payload = payload_object(task.payload_json)
    upstream = payload.get("upstream") if isinstance(payload.get("upstream"), dict) else {}
    request = resolve_request(payload.get("request") if isinstance(payload.get("request"), dict) else payload)
    pipeline_id = new_pipeline_id(payload.get("pipeline_id"))

    upstream_result = fetch_upstream_result_content_json(db, upstream)
    upstream_type, scored_rows, pipeline_counts = _extract_scored_jobs(upstream_result)
    scored_jobs = normalize_scored_jobs(scored_rows)

    shortlist_policy = payload.get("shortlist_policy") if isinstance(payload.get("shortlist_policy"), dict) else {}
    try:
        max_items = int(shortlist_policy.get("max_items") or request.get("shortlist_max_items") or 10)
    except (TypeError, ValueError):
        max_items = 10
    max_items = max(1, min(max_items, 100))

    min_score_value = shortlist_policy.get("min_score")
    if min_score_value is None:
        min_score_value = request.get("shortlist_min_score")
    min_score_100 = resolve_min_score_100(min_score_value)

    try:
        per_source_cap = int(shortlist_policy.get("per_source_cap") or request.get("shortlist_per_source_cap") or 3)
    except (TypeError, ValueError):
        per_source_cap = 3
    per_source_cap = max(1, min(per_source_cap, 50))

    try:
        per_company_cap = int(shortlist_policy.get("per_company_cap") or 2)
    except (TypeError, ValueError):
        per_company_cap = 2
    per_company_cap = max(1, min(per_company_cap, 10))

    try:
        source_diversity_weight = float(shortlist_policy.get("source_diversity_weight") or 4.0)
    except (TypeError, ValueError):
        source_diversity_weight = 4.0
    source_diversity_weight = max(0.0, min(source_diversity_weight, 20.0))

    try:
        company_repetition_penalty = float(shortlist_policy.get("company_repetition_penalty") or 8.0)
    except (TypeError, ValueError):
        company_repetition_penalty = 8.0
    company_repetition_penalty = max(0.0, min(company_repetition_penalty, 30.0))

    try:
        near_duplicate_title_similarity_threshold = float(
            shortlist_policy.get("near_duplicate_title_similarity_threshold") or 0.82
        )
    except (TypeError, ValueError):
        near_duplicate_title_similarity_threshold = 0.82
    near_duplicate_title_similarity_threshold = max(0.5, min(near_duplicate_title_similarity_threshold, 1.0))

    freshness_weight_enabled = bool(shortlist_policy.get("freshness_weight_enabled", False))
    try:
        freshness_max_bonus = float(shortlist_policy.get("freshness_max_bonus") or 8.0)
    except (TypeError, ValueError):
        freshness_max_bonus = 8.0
    freshness_max_bonus = max(0.0, min(freshness_max_bonus, 20.0))

    shortlist_raw, rejected_summary, diagnostics = shortlist_jobs(
        scored_jobs,
        max_items=max_items,
        min_score_100=min_score_100,
        per_source_cap=per_source_cap,
        per_company_cap=per_company_cap,
        source_diversity_weight=source_diversity_weight,
        company_repetition_penalty=company_repetition_penalty,
        near_duplicate_title_similarity_threshold=near_duplicate_title_similarity_threshold,
        freshness_weight_enabled=freshness_weight_enabled,
        freshness_max_bonus=freshness_max_bonus,
    )

    shortlist: list[dict[str, Any]] = []
    for row in shortlist_raw:
        item = {
            key: value
            for key, value in row.items()
            if not key.startswith("_")
        }
        shortlist.append(item)

    shortlist_summary_metadata = {
        "requested_size": max_items,
        "selected_size": len(shortlist),
        "input_scored_count": len(scored_jobs),
        "min_score_100": round(min_score_100, 2),
        "upstream_artifact_type": upstream_type,
        "pipeline_counts": {
            "collected_count": pipeline_counts.get("collected_count"),
            "normalized_count": pipeline_counts.get("normalized_count"),
            "deduped_count": pipeline_counts.get("deduped_count"),
            "duplicates_collapsed": pipeline_counts.get("duplicates_collapsed"),
            "scored_count": pipeline_counts.get("scored_count") or len(scored_jobs),
            "shortlisted_count": len(shortlist),
        },
    }

    anti_repetition_summary = {
        "enabled": True,
        "constraints": {
            "per_source_cap": per_source_cap,
            "per_company_cap": per_company_cap,
            "near_duplicate_title_similarity_threshold": near_duplicate_title_similarity_threshold,
            "source_diversity_weight": source_diversity_weight,
            "company_repetition_penalty": company_repetition_penalty,
            "freshness_weight_enabled": freshness_weight_enabled,
            "freshness_max_bonus": freshness_max_bonus,
        },
        "rejected_summary": rejected_summary,
    }

    jobs_top_artifact = {
        "artifact_type": "jobs_top.v1",
        "artifact_schema": "jobs_top.v1",
        "pipeline_id": pipeline_id,
        "shortlisted_at": utc_iso(),
        "request": request,
        "shortlist_policy": {
            "max_items": max_items,
            "min_score_100": round(min_score_100, 2),
            "per_source_cap": per_source_cap,
            "per_company_cap": per_company_cap,
            "source_diversity_weight": source_diversity_weight,
            "company_repetition_penalty": company_repetition_penalty,
            "near_duplicate_title_similarity_threshold": near_duplicate_title_similarity_threshold,
            "freshness_weight_enabled": freshness_weight_enabled,
            "freshness_max_bonus": freshness_max_bonus,
        },
        "top_jobs": shortlist,
        "summary": {
            **shortlist_summary_metadata,
            "rejected_summary": rejected_summary,
        },
        "pipeline_counts": shortlist_summary_metadata["pipeline_counts"],
        "anti_repetition_summary": anti_repetition_summary,
        "upstream": upstream,
    }

    # Structured seed payloads for downstream notifications and application drafting.
    action_seed = {
        "cover_letter": {
            "status": "not_started",
            "jobs": [
                {
                    "job_id": row.get("job_id"),
                    "title": row.get("title"),
                    "company": row.get("company"),
                    "url": row.get("url"),
                    "explanation_summary": row.get("explanation_summary"),
                }
                for row in shortlist[:5]
            ],
        },
        "application_draft": {
            "status": "not_started",
            "jobs": [
                {
                    "job_id": row.get("job_id"),
                    "title": row.get("title"),
                    "company": row.get("company"),
                    "source": row.get("source"),
                    "source_url": row.get("source_url"),
                }
                for row in shortlist[:5]
            ],
        },
        "interview_prep": {
            "status": "not_started",
            "jobs": [
                {
                    "job_id": row.get("job_id"),
                    "title": row.get("title"),
                    "company": row.get("company"),
                    "fit_tier": row.get("fit_tier"),
                }
                for row in shortlist[:5]
            ],
        },
        "follow_up": {
            "status": "not_started",
            "jobs": [
                {
                    "job_id": row.get("job_id"),
                    "title": row.get("title"),
                    "company": row.get("company"),
                    "url": row.get("url"),
                }
                for row in shortlist[:5]
            ],
        },
    }

    notification_candidates = [
        {
            "job_id": row.get("job_id"),
            "title": row.get("title"),
            "company": row.get("company"),
            "score": row.get("score"),
            "overall_score": row.get("overall_score"),
            "source": row.get("source"),
            "url": row.get("url"),
            "summary": row.get("explanation_summary"),
        }
        for row in shortlist[:10]
    ]

    artifact = {
        "artifact_type": "jobs.shortlist.v1",
        "artifact_schema": "jobs.shortlist.v2",
        "pipeline_id": pipeline_id,
        "shortlisted_at": utc_iso(),
        "request": request,
        "shortlist_policy": jobs_top_artifact["shortlist_policy"],
        "shortlist": shortlist,
        "shortlist_count": len(shortlist),
        "jobs_top_artifact": jobs_top_artifact,
        "pipeline_counts": shortlist_summary_metadata["pipeline_counts"],
        "shortlist_summary_metadata": shortlist_summary_metadata,
        "anti_repetition_summary": anti_repetition_summary,
        "rejected_summary": rejected_summary,
        "selection_diagnostics": diagnostics,
        "selection_reasons": [
            {
                "job_id": row.get("job_id"),
                "title": row.get("title"),
                "company": row.get("company"),
                "source": row.get("source"),
                "score": row.get("score"),
                "overall_score": row.get("overall_score"),
                "fit_tier": row.get("fit_tier"),
                "explanation_summary": row.get("explanation_summary"),
            }
            for row in shortlist
        ],
        "notification_candidates": notification_candidates,
        "action_seed": action_seed,
        "upstream": upstream,
    }

    next_upstream = build_upstream_ref(task, "jobs_shortlist_v1")
    upstream_run_id = next_upstream.get("run_id") or str(getattr(task, "id", ""))
    next_payload = {
        "pipeline_id": pipeline_id,
        "upstream": next_upstream,
        "request": request,
        "digest_policy": payload.get("digest_policy") if isinstance(payload.get("digest_policy"), dict) else {
            "max_items": max_items,
            "format": str(request.get("digest_format") or "compact"),
            "notify_channels": request.get("notify_channels") or ["discord"],
            "notify_on_empty": bool(request.get("notify_on_empty", False)),
            "llm_enabled": bool(request.get("digest_llm_enabled", True)),
        },
    }

    return {
        "artifact_type": "jobs.shortlist.v1",
        "content_text": (
            f"Shortlisted {len(shortlist)} jobs from {len(scored_jobs)} scored candidates "
            f"(target={max_items}, min_score_100={round(min_score_100, 2)})."
        ),
        "content_json": artifact,
        "debug_json": {
            "pipeline_id": pipeline_id,
            "selected_size": len(shortlist),
            "input_scored_count": len(scored_jobs),
            "upstream_artifact_type": upstream_type,
        },
        "next_tasks": [
            {
                "task_type": "jobs_digest_v2",
                "payload_json": next_payload,
                "idempotency_key": stage_idempotency_key(pipeline_id, "jobs_digest_v2", upstream_run_id),
                "max_attempts": 3,
            }
        ],
    }
