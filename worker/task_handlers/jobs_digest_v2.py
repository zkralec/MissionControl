from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import Any

from jsonschema import Draft7Validator

from llm.openai_adapter import run_chat_completion
from models.catalog import tier_model
from task_handlers.errors import NonRetryableTaskError
from task_handlers.jobs_pipeline_common import (
    fetch_upstream_result_content_json,
    new_pipeline_id,
    payload_object,
    resolve_request,
    stage_idempotency_key,
    utc_iso,
)
from task_handlers.prompts.jobs_digest_v2 import (
    DIGEST_OUTPUT_SCHEMA,
    DIGEST_PROMPT_VERSION,
    build_digest_messages,
)

DEFAULT_LLM_MAX_RETRIES = 1
STRICT_DEFAULT_LLM_MAX_RETRIES = 2
MAX_LLM_MAX_RETRIES = 5
MAX_DIGEST_ITEMS = 10
DEFAULT_LLM_RETRY_COST_CAP_USD = Decimal("0.00150000")
STRICT_LLM_RETRY_COST_CAP_USD = Decimal("0.00400000")
MAX_LLM_RETRY_COST_CAP_USD = Decimal("0.10000000")
_DIGEST_OUTPUT_VALIDATOR = Draft7Validator(DIGEST_OUTPUT_SCHEMA)


def _llm_runtime_enabled() -> bool:
    return os.getenv("USE_LLM", "false").strip().lower() == "true"


def _default_llm_max_retries() -> int:
    raw = os.getenv("JOBS_DIGEST_LLM_MAX_RETRIES_DEFAULT", str(DEFAULT_LLM_MAX_RETRIES))
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        parsed = DEFAULT_LLM_MAX_RETRIES
    return max(1, min(parsed, MAX_LLM_MAX_RETRIES))


def _resolve_retry_cost_cap(raw_value: Any, *, strict_llm_output: bool) -> Decimal:
    env_value = os.getenv("JOBS_DIGEST_LLM_RETRY_COST_CAP_USD_DEFAULT")
    if raw_value is None and env_value is not None:
        raw_value = env_value
    if raw_value is None:
        return STRICT_LLM_RETRY_COST_CAP_USD if strict_llm_output else DEFAULT_LLM_RETRY_COST_CAP_USD
    try:
        parsed = Decimal(str(raw_value))
    except Exception:
        return STRICT_LLM_RETRY_COST_CAP_USD if strict_llm_output else DEFAULT_LLM_RETRY_COST_CAP_USD
    if parsed <= 0:
        return STRICT_LLM_RETRY_COST_CAP_USD if strict_llm_output else DEFAULT_LLM_RETRY_COST_CAP_USD
    return min(parsed.quantize(Decimal("0.00000001")), MAX_LLM_RETRY_COST_CAP_USD)


def _canonical_error_code(exc: Exception) -> str:
    message = str(exc or "").strip()
    if not message:
        return type(exc).__name__.lower()
    if ":" in message:
        return message.split(":", 1)[0].strip().lower()
    return message.replace(" ", "_").lower()


def _runtime_error_with_usage(
    message: str,
    *,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: str = "0.00000000",
    request_ids: list[str] | None = None,
    task_run_ids: list[str] | None = None,
) -> RuntimeError:
    err = RuntimeError(message)
    err.usage = {
        "tokens_in": max(int(tokens_in), 0),
        "tokens_out": max(int(tokens_out), 0),
        "cost_usd": str(cost_usd or "0.00000000"),
        "openai_request_ids": request_ids or [],
        "ai_usage_task_run_ids": task_run_ids or [],
    }
    return err


def _compact_text(value: Any, max_chars: int) -> str:
    text = " ".join(str(value or "").strip().split())
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _as_int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return parsed


def _salary_text(job: dict[str, Any]) -> str:
    salary_text = str(job.get("salary_text") or "").strip()
    if salary_text:
        return salary_text
    min_salary = job.get("salary_min")
    max_salary = job.get("salary_max")
    currency = str(job.get("salary_currency") or "USD").strip().upper() or "USD"

    def _fmt(value: Any) -> str | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if parsed <= 0:
            return None
        return f"{currency} {int(round(parsed)):,}"

    low = _fmt(min_salary)
    high = _fmt(max_salary)
    if low and high:
        return f"{low} - {high}"
    if low:
        return f"{low}+"
    if high:
        return f"Up to {high}"
    return "Not listed"


def _normalize_top_jobs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for idx, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        item = dict(row)
        job_id = str(item.get("job_id") or item.get("normalized_job_id") or "").strip()
        if not job_id:
            job_id = f"digest-job-{idx:04d}"
        source_url = item.get("source_url")
        if not isinstance(source_url, str) or not source_url.strip():
            source_url = item.get("url") if isinstance(item.get("url"), str) and item.get("url", "").strip() else ""
        item["job_id"] = job_id
        item["source_url"] = str(source_url or "")
        item["salary_text"] = _salary_text(item)
        normalized.append(item)
    return normalized


def _extract_top_jobs_and_counts(upstream_result: dict[str, Any]) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    artifact_type = str(upstream_result.get("artifact_type") or "").strip()
    if artifact_type == "jobs.shortlist.v1":
        jobs_top_artifact = upstream_result.get("jobs_top_artifact") if isinstance(upstream_result.get("jobs_top_artifact"), dict) else {}
        top_jobs = jobs_top_artifact.get("top_jobs") if isinstance(jobs_top_artifact.get("top_jobs"), list) else upstream_result.get("shortlist")
        if not isinstance(top_jobs, list):
            top_jobs = []

        summary = jobs_top_artifact.get("summary") if isinstance(jobs_top_artifact.get("summary"), dict) else {}
        shortlist_meta = upstream_result.get("shortlist_summary_metadata") if isinstance(upstream_result.get("shortlist_summary_metadata"), dict) else {}
        pipeline_counts = {}
        for candidate in (
            upstream_result.get("pipeline_counts"),
            jobs_top_artifact.get("pipeline_counts"),
            summary.get("pipeline_counts"),
            shortlist_meta.get("pipeline_counts"),
        ):
            if isinstance(candidate, dict):
                pipeline_counts.update(candidate)
        if not pipeline_counts:
            input_scored_count = _as_int_or_none(shortlist_meta.get("input_scored_count"))
            if input_scored_count is not None:
                pipeline_counts["scored_count"] = input_scored_count

        return artifact_type, _normalize_top_jobs(top_jobs), pipeline_counts

    if artifact_type == "jobs_top.v1":
        top_jobs = upstream_result.get("top_jobs")
        if not isinstance(top_jobs, list):
            top_jobs = []
        summary = upstream_result.get("summary") if isinstance(upstream_result.get("summary"), dict) else {}
        pipeline_counts = {}
        for candidate in (
            upstream_result.get("pipeline_counts"),
            summary.get("pipeline_counts"),
        ):
            if isinstance(candidate, dict):
                pipeline_counts.update(candidate)
        return artifact_type, _normalize_top_jobs(top_jobs), pipeline_counts

    raise NonRetryableTaskError(
        "upstream contract mismatch: jobs_digest_v2 expects artifact_type "
        "'jobs.shortlist.v1' or 'jobs_top.v1'"
    )


def _normalized_pipeline_counts(raw: dict[str, Any], shortlisted_count: int) -> dict[str, int | None]:
    collected = _as_int_or_none(raw.get("collected_count"))
    if collected is None:
        collected = _as_int_or_none(raw.get("raw_count"))

    normalized = _as_int_or_none(raw.get("normalized_count"))
    deduped = _as_int_or_none(raw.get("deduped_count"))
    duplicates_collapsed = _as_int_or_none(raw.get("duplicates_collapsed"))
    scored = _as_int_or_none(raw.get("scored_count"))
    if scored is None:
        scored = _as_int_or_none(raw.get("input_scored_count"))

    return {
        "collected_count": collected,
        "normalized_count": normalized,
        "deduped_count": deduped,
        "duplicates_collapsed": duplicates_collapsed,
        "scored_count": scored,
        "shortlisted_count": shortlisted_count,
    }


def _best_fit_roles(jobs: list[dict[str, Any]]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for row in jobs:
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(title)
        if len(output) >= 4:
            break
    return output


def _strongest_patterns(jobs: list[dict[str, Any]]) -> list[str]:
    if not jobs:
        return ["No shortlist entries were available in this run."]

    source_counts: dict[str, int] = {}
    remote_like = 0
    salary_known = 0
    for row in jobs:
        source = str(row.get("source") or "unknown").strip().lower() or "unknown"
        source_counts[source] = source_counts.get(source, 0) + 1
        location_text = str(row.get("location") or "").lower()
        remote_type = str(row.get("remote_type") or row.get("work_mode") or "").lower()
        if "remote" in location_text or remote_type == "remote":
            remote_like += 1
        if _salary_text(row) != "Not listed":
            salary_known += 1

    dominant_source, dominant_count = max(source_counts.items(), key=lambda item: item[1])
    patterns = [
        f"{dominant_count}/{len(jobs)} top roles are from {dominant_source}.",
        f"{remote_like}/{len(jobs)} roles appear remote-friendly.",
        f"{salary_known}/{len(jobs)} roles include salary signal.",
    ]
    return patterns


def _build_fallback_digest(
    *,
    top_jobs: list[dict[str, Any]],
    pipeline_counts: dict[str, int | None],
) -> dict[str, Any]:
    jobs: list[dict[str, Any]] = []
    for idx, row in enumerate(top_jobs, start=1):
        why = _compact_text(
            row.get("explanation_summary")
            or row.get("fit_reason")
            or "Matches core title and profile preferences.",
            180,
        )
        if not why:
            why = "Matches core title and profile preferences."

        tradeoffs = []
        if _salary_text(row) == "Not listed":
            tradeoffs.append("Salary not listed.")
        if str(row.get("source") or "").strip().lower() in {"unknown", ""}:
            tradeoffs.append("Source quality signal is limited.")
        if str(row.get("location") or "").strip() == "":
            tradeoffs.append("Location details are limited.")
        tradeoff_text = _compact_text(" ".join(tradeoffs) or "Needs deeper role-level validation before applying.", 180)

        jobs.append(
            {
                "job_id": str(row.get("job_id") or f"digest-job-{idx:04d}"),
                "rank": idx,
                "title": str(row.get("title") or "Unknown role"),
                "company": str(row.get("company") or "Unknown company"),
                "location": str(row.get("location") or "Not listed"),
                "salary": _salary_text(row),
                "source": str(row.get("source") or "unknown"),
                "source_url": str(row.get("source_url") or row.get("url") or ""),
                "why_it_fits": why,
                "tradeoffs": tradeoff_text or "Needs deeper role-level validation before applying.",
            }
        )

    collected = pipeline_counts.get("collected_count")
    deduped = pipeline_counts.get("deduped_count")
    shortlist_count = pipeline_counts.get("shortlisted_count") or len(top_jobs)
    if collected is not None and deduped is not None:
        summary_text = (
            f"Reviewed {collected} collected jobs, {deduped} after dedupe, "
            f"and shortlisted {shortlist_count} for action."
        )
    elif deduped is not None:
        summary_text = f"Reviewed {deduped} deduped jobs and shortlisted {shortlist_count} strongest matches."
    else:
        summary_text = f"Shortlisted {shortlist_count} strongest matches from available ranked jobs."

    excerpt = summary_text
    if jobs:
        top_line = "; ".join(f"{row['rank']}) {row['title']} @ {row['company']}" for row in jobs[:3])
        excerpt = _compact_text(f"{summary_text} Top picks: {top_line}.", 500)

    return {
        "executive_summary": {
            "collected_count": collected,
            "deduped_count": deduped,
            "shortlisted_count": shortlist_count,
            "summary_text": _compact_text(summary_text, 320),
            "strongest_patterns": _strongest_patterns(top_jobs),
            "best_fit_roles": _best_fit_roles(top_jobs),
        },
        "jobs": jobs,
        "notification_excerpt": excerpt,
    }


def _extract_json(text: Any) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty_llm_output")
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    decoder = json.JSONDecoder()
    try:
        parsed_obj, _ = decoder.raw_decode(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        if start < 0:
            raise ValueError("llm_output_not_json") from None
        try:
            parsed_obj, _ = decoder.raw_decode(candidate[start:])
        except json.JSONDecodeError as exc:
            raise ValueError("llm_output_not_json") from exc
    parsed = parsed_obj
    if not isinstance(parsed, dict):
        raise ValueError("llm_output_must_be_object")
    return parsed


def _validate_schema_shape(payload: dict[str, Any]) -> None:
    errors = sorted(_DIGEST_OUTPUT_VALIDATOR.iter_errors(payload), key=lambda err: list(err.path))
    if not errors:
        return
    first = errors[0]
    path = ".".join(str(part) for part in first.path) or "$"
    raise ValueError(f"llm_output_schema_error:{path}:{first.message}")


def _is_repetitive_digest_pattern(rows: list[dict[str, Any]]) -> bool:
    if len(rows) < 3:
        return False
    why_set = {str(row.get("why_it_fits") or "").strip().lower() for row in rows}
    tradeoff_set = {str(row.get("tradeoffs") or "").strip().lower() for row in rows}
    return len(why_set) <= 1 and len(tradeoff_set) <= 1


def _parse_llm_digest(
    *,
    payload: dict[str, Any],
    expected_jobs: list[dict[str, Any]],
    pipeline_counts: dict[str, int | None],
) -> dict[str, Any]:
    _validate_schema_shape(payload)
    rows = payload.get("jobs")
    if not isinstance(rows, list):
        raise ValueError("llm_output_jobs_must_be_array")

    expected_by_id = {str(row.get("job_id")): row for row in expected_jobs}
    expected_ids = set(expected_by_id.keys())
    parsed_rows: dict[str, dict[str, Any]] = {}

    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("llm_output_job_row_must_be_object")
        job_id = str(row.get("job_id") or "").strip()
        if not job_id:
            raise ValueError("llm_output_missing_job_id")
        if job_id not in expected_ids:
            raise ValueError(f"llm_output_unknown_job_id:{job_id}")
        if job_id in parsed_rows:
            raise ValueError(f"llm_output_duplicate_job_id:{job_id}")
        source = expected_by_id[job_id]
        parsed_rows[job_id] = {
            "job_id": job_id,
            "rank": _as_int_or_none(row.get("rank")) or (_as_int_or_none(source.get("rank")) or 0),
            "title": _compact_text(row.get("title") or source.get("title") or "Unknown role", 120),
            "company": _compact_text(row.get("company") or source.get("company") or "Unknown company", 120),
            "location": _compact_text(row.get("location") or source.get("location") or "Not listed", 120),
            "salary": _compact_text(row.get("salary") or source.get("salary_text") or _salary_text(source), 120),
            "source": _compact_text(row.get("source") or source.get("source") or "unknown", 80),
            "source_url": _compact_text(row.get("source_url") or source.get("source_url") or source.get("url") or "", 260),
            "why_it_fits": _compact_text(row.get("why_it_fits"), 180),
            "tradeoffs": _compact_text(row.get("tradeoffs"), 180),
        }

        if not parsed_rows[job_id]["why_it_fits"]:
            parsed_rows[job_id]["why_it_fits"] = _compact_text(
                source.get("explanation_summary") or source.get("fit_reason") or "Strong preference alignment.",
                180,
            ) or "Strong preference alignment."
        if not parsed_rows[job_id]["tradeoffs"]:
            parsed_rows[job_id]["tradeoffs"] = "Needs deeper role-level validation before applying."

    missing = [job_id for job_id in expected_ids if job_id not in parsed_rows]
    if missing:
        raise ValueError("llm_output_partial_missing_job_ids:" + ",".join(sorted(missing)))

    ordered = sorted(
        parsed_rows.values(),
        key=lambda row: (
            int(row.get("rank") or 0),
            str(row.get("job_id") or ""),
        ),
    )
    for idx, row in enumerate(ordered, start=1):
        row["rank"] = idx
    if _is_repetitive_digest_pattern(ordered):
        raise ValueError("llm_output_repetitive_digest_pattern")

    executive_summary = payload.get("executive_summary")
    if not isinstance(executive_summary, dict):
        raise ValueError("llm_output_executive_summary_must_be_object")
    summary_text = _compact_text(executive_summary.get("summary_text"), 320)
    if not summary_text:
        raise ValueError("llm_output_missing_summary_text")

    strongest_patterns = executive_summary.get("strongest_patterns")
    if not isinstance(strongest_patterns, list):
        raise ValueError("llm_output_strongest_patterns_must_be_array")
    strongest_patterns = [_compact_text(item, 120) for item in strongest_patterns]
    strongest_patterns = [item for item in strongest_patterns if item][:4]
    if not strongest_patterns:
        strongest_patterns = _strongest_patterns(expected_jobs)

    best_fit_roles = executive_summary.get("best_fit_roles")
    if not isinstance(best_fit_roles, list):
        raise ValueError("llm_output_best_fit_roles_must_be_array")
    best_fit_roles = [_compact_text(item, 100) for item in best_fit_roles]
    best_fit_roles = [item for item in best_fit_roles if item][:4]
    if not best_fit_roles:
        best_fit_roles = _best_fit_roles(expected_jobs)

    excerpt = _compact_text(payload.get("notification_excerpt"), 500)
    if not excerpt:
        excerpt = _compact_text(summary_text, 500)

    return {
        "executive_summary": {
            "collected_count": pipeline_counts.get("collected_count"),
            "deduped_count": pipeline_counts.get("deduped_count"),
            "shortlisted_count": pipeline_counts.get("shortlisted_count"),
            "summary_text": summary_text,
            "strongest_patterns": strongest_patterns,
            "best_fit_roles": best_fit_roles,
        },
        "jobs": ordered,
        "notification_excerpt": excerpt,
    }


def _llm_generate_digest(
    *,
    model: str,
    task_id: str,
    run_id: str,
    top_jobs: list[dict[str, Any]],
    pipeline_counts: dict[str, int | None],
    digest_format: str,
    prompt_version: str,
    max_retries: int,
    retry_cost_cap_usd: Decimal,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    attempts_budget = max(1, max_retries)
    attempts_made = 0
    last_error: Exception | None = None
    openai_request_ids: list[str] = []
    ai_usage_task_run_ids: list[str] = []
    tokens_in_total = 0
    tokens_out_total = 0
    cost_total = Decimal("0")
    attempt_errors: list[dict[str, Any]] = []
    last_error_code: str | None = None
    repeated_error_count = 0
    stop_reason = "max_retries_exhausted"
    fast_fail_codes = {
        "empty_llm_output",
        "llm_output_not_json",
        "llm_output_must_be_object",
        "llm_output_schema_error",
        "llm_output_repetitive_digest_pattern",
    }

    summary_context = {
        "collected_count": pipeline_counts.get("collected_count"),
        "deduped_count": pipeline_counts.get("deduped_count"),
        "shortlisted_count": pipeline_counts.get("shortlisted_count"),
        "scored_count": pipeline_counts.get("scored_count"),
    }

    for attempt in range(1, attempts_budget + 1):
        attempts_made = attempt
        messages = build_digest_messages(
            top_jobs=top_jobs,
            summary_context=summary_context,
            prompt_version=prompt_version,
            digest_format=digest_format,
        )
        digest_task_run_id = f"{task_id}:{run_id}:jobs_digest_v2_{attempt}"
        ai_usage_task_run_ids.append(digest_task_run_id)
        llm_result = run_chat_completion(
            model=model,
            messages=messages,
            temperature=0.2,
            max_completion_tokens=2200,
            task_run_id=digest_task_run_id,
            agent_name="jobs_digest_v2",
        )
        tokens_in_total += int(llm_result.get("tokens_in") or 0)
        tokens_out_total += int(llm_result.get("tokens_out") or 0)
        cost_total += Decimal(str(llm_result.get("cost_usd") or "0"))
        req_id = llm_result.get("openai_request_id")
        if isinstance(req_id, str) and req_id.strip():
            openai_request_ids.append(req_id.strip())

        try:
            parsed = _extract_json(llm_result.get("output_text"))
            digest = _parse_llm_digest(payload=parsed, expected_jobs=top_jobs, pipeline_counts=pipeline_counts)
            stop_reason = "success"
            return digest, {
                "attempts": attempt,
                "error": None,
                "openai_request_ids": openai_request_ids,
                "ai_usage_task_run_ids": ai_usage_task_run_ids,
                "tokens_in": tokens_in_total,
                "tokens_out": tokens_out_total,
                "cost_usd": str(cost_total.quantize(Decimal("0.00000001"))),
                "attempt_errors": attempt_errors,
                "stop_reason": stop_reason,
                "retry_cost_cap_usd": str(retry_cost_cap_usd),
            }
        except Exception as exc:
            last_error = exc
            error_code = _canonical_error_code(exc)
            if error_code.startswith("llm_output_schema_error:"):
                error_code = "llm_output_schema_error"
            attempt_errors.append(
                {
                    "attempt": attempt,
                    "error_code": error_code,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            if error_code == last_error_code:
                repeated_error_count += 1
            else:
                repeated_error_count = 1
            last_error_code = error_code
            if repeated_error_count >= 2 and error_code in fast_fail_codes and attempt < attempts_budget:
                stop_reason = "fast_fail_repeated_output_pattern"
                break
            if cost_total >= retry_cost_cap_usd and attempt < attempts_budget:
                stop_reason = "retry_cost_cap_reached"
                break
            continue

    return None, {
        "attempts": attempts_made,
        "error": f"{type(last_error).__name__}: {last_error}",
        "openai_request_ids": openai_request_ids,
        "ai_usage_task_run_ids": ai_usage_task_run_ids,
        "tokens_in": tokens_in_total,
        "tokens_out": tokens_out_total,
        "cost_usd": str(cost_total.quantize(Decimal("0.00000001"))),
        "attempt_errors": attempt_errors,
        "stop_reason": stop_reason,
        "retry_cost_cap_usd": str(retry_cost_cap_usd),
    }


def _render_markdown(report: dict[str, Any]) -> str:
    summary = report.get("executive_summary") if isinstance(report.get("executive_summary"), dict) else {}
    jobs = report.get("jobs") if isinstance(report.get("jobs"), list) else []

    collected = summary.get("collected_count")
    deduped = summary.get("deduped_count")
    shortlisted = summary.get("shortlisted_count")

    lines = [
        "# Jobs Digest",
        "",
        "## Executive Summary",
        str(summary.get("summary_text") or "No summary available."),
        "",
        f"- Collected: {collected if collected is not None else 'n/a'}",
        f"- After dedupe: {deduped if deduped is not None else 'n/a'}",
        f"- Shortlisted: {shortlisted if shortlisted is not None else len(jobs)}",
    ]

    strongest_patterns = summary.get("strongest_patterns") if isinstance(summary.get("strongest_patterns"), list) else []
    if strongest_patterns:
        lines.append("- Strongest patterns: " + "; ".join(str(item) for item in strongest_patterns))
    best_fit_roles = summary.get("best_fit_roles") if isinstance(summary.get("best_fit_roles"), list) else []
    if best_fit_roles:
        lines.append("- Best-fit roles: " + ", ".join(str(item) for item in best_fit_roles))

    lines.append("")
    lines.append("## Top Jobs")
    if not jobs:
        lines.append("No shortlisted jobs were available.")
        return "\n".join(lines)

    for row in jobs:
        rank = int(row.get("rank") or 0)
        title = str(row.get("title") or "Unknown role")
        company = str(row.get("company") or "Unknown company")
        source = str(row.get("source") or "unknown")
        source_url = str(row.get("source_url") or "").strip()
        lines.append(f"### {rank}. {title} - {company}")
        lines.append(f"- Location: {row.get('location') or 'Not listed'}")
        lines.append(f"- Salary: {row.get('salary') or 'Not listed'}")
        lines.append(f"- Source: {source}" + (f" ({source_url})" if source_url else ""))
        lines.append(f"- Why it fits: {row.get('why_it_fits') or 'Not provided.'}")
        lines.append(f"- Tradeoffs: {row.get('tradeoffs') or 'Not provided.'}")
        lines.append("")

    return "\n".join(lines).strip()


def _build_artifact_references(
    *,
    task_id: str,
    run_id: str,
    pipeline_id: str,
    base_url: str | None,
) -> dict[str, Any]:
    refs: dict[str, Any] = {
        "task_id": task_id,
        "run_id": run_id,
        "pipeline_id": pipeline_id,
        "artifact_types": ["jobs.digest.v2", "jobs_digest.json.v1", "jobs_digest.md.v1"],
        "task_path": f"/tasks/{task_id}" if task_id else None,
        "runs_path": f"/tasks/{task_id}/runs" if task_id else None,
        "result_path": f"/tasks/{task_id}/result" if task_id else None,
    }
    if base_url:
        root = base_url.rstrip("/")
        if task_id:
            refs["task_url"] = f"{root}/tasks/{task_id}"
            refs["runs_url"] = f"{root}/tasks/{task_id}/runs"
            refs["result_url"] = f"{root}/tasks/{task_id}/result"
    return refs


def _build_discord_digest_message(
    *,
    report: dict[str, Any],
    pipeline_counts: dict[str, int | None],
    artifact_refs: dict[str, Any],
) -> str:
    summary = report.get("executive_summary") if isinstance(report.get("executive_summary"), dict) else {}
    jobs = report.get("jobs") if isinstance(report.get("jobs"), list) else []
    headline = _compact_text(summary.get("summary_text"), 200) or "Jobs digest generated."

    shortlisted_count = pipeline_counts.get("shortlisted_count")
    if shortlisted_count is None:
        shortlisted_count = len(jobs)
    collected_count = pipeline_counts.get("collected_count")
    deduped_count = pipeline_counts.get("deduped_count")

    count_line = f"Shortlisted: {shortlisted_count}"
    if collected_count is not None and deduped_count is not None:
        count_line += f" (collected {collected_count}, deduped {deduped_count})"
    elif deduped_count is not None:
        count_line += f" (deduped {deduped_count})"

    lines = [
        "**Jobs Digest**",
        headline,
        count_line,
    ]

    if jobs:
        lines.append("Top roles:")
        for row in jobs[:3]:
            title = _compact_text(row.get("title") or "Unknown role", 70)
            company = _compact_text(row.get("company") or "Unknown company", 50)
            source = _compact_text(row.get("source") or "unknown", 24)
            why = _compact_text(row.get("why_it_fits") or "", 80)
            base = f"{row.get('rank')}. {title} @ {company} ({source})"
            lines.append(base if not why else f"{base} - {why}")
    else:
        lines.append("No shortlist items matched this run.")

    result_url = artifact_refs.get("result_url")
    result_path = artifact_refs.get("result_path")
    task_id = artifact_refs.get("task_id")
    run_id = artifact_refs.get("run_id")
    ref_line = f"Ref: task={task_id} run={run_id}"
    lines.append(ref_line)
    if isinstance(result_url, str) and result_url.strip():
        lines.append(f"Result: {result_url}")
    elif isinstance(result_path, str) and result_path.strip():
        lines.append(f"Result: {result_path}")

    return _compact_text("\n".join(lines), 1800)


def execute(task: Any, db: Any) -> dict[str, Any]:
    payload = payload_object(task.payload_json)
    upstream = payload.get("upstream") if isinstance(payload.get("upstream"), dict) else {}
    request = resolve_request(payload.get("request") if isinstance(payload.get("request"), dict) else payload)
    pipeline_id = new_pipeline_id(payload.get("pipeline_id"))

    upstream_result = fetch_upstream_result_content_json(db, upstream)
    upstream_type, extracted_jobs, raw_pipeline_counts = _extract_top_jobs_and_counts(upstream_result)

    digest_policy = payload.get("digest_policy") if isinstance(payload.get("digest_policy"), dict) else {}
    try:
        max_items = int(digest_policy.get("max_items") or request.get("shortlist_max_items") or 10)
    except (TypeError, ValueError):
        max_items = 10
    max_items = max(1, min(max_items, MAX_DIGEST_ITEMS))

    digest_format = str(digest_policy.get("format") or request.get("digest_format") or "compact").strip().lower() or "compact"
    llm_enabled = bool(digest_policy.get("llm_enabled", bool(request.get("digest_llm_enabled", True))))
    strict_llm_output = bool(digest_policy.get("strict_llm_output", False))
    prompt_version = str(digest_policy.get("prompt_version") or DIGEST_PROMPT_VERSION)
    retries_raw = digest_policy.get("llm_max_retries")
    if retries_raw is None:
        retries_raw = request.get("digest_llm_max_retries")
    default_retries = _default_llm_max_retries()
    if strict_llm_output:
        default_retries = max(default_retries, STRICT_DEFAULT_LLM_MAX_RETRIES)
    try:
        llm_max_retries = int(retries_raw if retries_raw is not None else default_retries)
    except (TypeError, ValueError):
        llm_max_retries = default_retries
    llm_max_retries = max(1, min(llm_max_retries, MAX_LLM_MAX_RETRIES))
    retry_cost_cap_raw = digest_policy.get("llm_retry_cost_cap_usd")
    if retry_cost_cap_raw is None:
        retry_cost_cap_raw = request.get("digest_llm_retry_cost_cap_usd")
    llm_retry_cost_cap_usd = _resolve_retry_cost_cap(
        retry_cost_cap_raw,
        strict_llm_output=strict_llm_output,
    )

    notify_channels = digest_policy.get("notify_channels") if isinstance(digest_policy.get("notify_channels"), list) else request.get("notify_channels")
    if not isinstance(notify_channels, list):
        notify_channels = ["discord"]
    notify_channels = [str(row).strip().lower() for row in notify_channels if str(row).strip()]
    if not notify_channels:
        notify_channels = ["discord"]
    notify_channels = [row for row in notify_channels if row == "discord"] or ["discord"]

    notify_on_empty = bool(digest_policy.get("notify_on_empty", bool(request.get("notify_on_empty", False))))

    top_jobs = extracted_jobs[:max_items]
    pipeline_counts = _normalized_pipeline_counts(raw_pipeline_counts, len(top_jobs))

    runtime_llm = llm_enabled and _llm_runtime_enabled() and bool(top_jobs)
    llm_warnings: list[str] = []
    llm_meta = {
        "enabled": llm_enabled,
        "runtime_enabled": runtime_llm,
        "model": None,
        "prompt_version": prompt_version,
        "max_retries": llm_max_retries,
        "retry_cost_cap_usd": str(llm_retry_cost_cap_usd),
        "attempts": 0,
        "request_ids": [],
        "ai_usage_task_run_ids": [],
        "tokens_in_total": 0,
        "tokens_out_total": 0,
        "cost_usd_total": "0.00000000",
        "attempt_errors": [],
        "stop_reason": None,
        "fallback_used": False,
        "strict_failure": False,
    }
    digest_report: dict[str, Any]
    generation_mode = "deterministic_fallback"

    if runtime_llm:
        model_id = str(getattr(task, "model", "") or "").strip() or tier_model("advanced")
        digest_report, llm_run_meta = _llm_generate_digest(
            model=model_id,
            task_id=str(getattr(task, "id", "") or ""),
            run_id=str(getattr(task, "_run_id", "") or ""),
            top_jobs=top_jobs,
            pipeline_counts=pipeline_counts,
            digest_format=digest_format,
            prompt_version=prompt_version,
            max_retries=llm_max_retries,
            retry_cost_cap_usd=llm_retry_cost_cap_usd,
        )
        llm_meta.update(
            {
                "model": model_id,
                "attempts": int(llm_run_meta.get("attempts") or 0),
                "request_ids": llm_run_meta.get("openai_request_ids") or [],
                "ai_usage_task_run_ids": llm_run_meta.get("ai_usage_task_run_ids") or [],
                "tokens_in_total": int(llm_run_meta.get("tokens_in") or 0),
                "tokens_out_total": int(llm_run_meta.get("tokens_out") or 0),
                "cost_usd_total": str(llm_run_meta.get("cost_usd") or "0.00000000"),
                "attempt_errors": llm_run_meta.get("attempt_errors") if isinstance(llm_run_meta.get("attempt_errors"), list) else [],
                "stop_reason": llm_run_meta.get("stop_reason"),
            }
        )
        llm_error = llm_run_meta.get("error")
        if isinstance(llm_error, str) and llm_error.strip():
            llm_warnings.append(f"llm_digest_failed: {llm_error.strip()}")
            stop_reason = llm_run_meta.get("stop_reason")
            if isinstance(stop_reason, str) and stop_reason.strip():
                llm_warnings.append(f"llm_digest_stop_reason:{stop_reason.strip()}")
            if strict_llm_output:
                llm_meta["strict_failure"] = True
                raise _runtime_error_with_usage(
                    f"temporary llm digest failure (strict_llm_output=true): {llm_error.strip()}",
                    tokens_in=llm_meta.get("tokens_in_total") or 0,
                    tokens_out=llm_meta.get("tokens_out_total") or 0,
                    cost_usd=str(llm_meta.get("cost_usd_total") or "0.00000000"),
                    request_ids=llm_meta.get("request_ids") if isinstance(llm_meta.get("request_ids"), list) else [],
                    task_run_ids=llm_meta.get("ai_usage_task_run_ids")
                    if isinstance(llm_meta.get("ai_usage_task_run_ids"), list)
                    else [],
                )
            digest_report = _build_fallback_digest(top_jobs=top_jobs, pipeline_counts=pipeline_counts)
            llm_meta["fallback_used"] = True
        elif isinstance(digest_report, dict):
            generation_mode = "llm_structured"
        else:
            llm_meta["strict_failure"] = strict_llm_output
            raise _runtime_error_with_usage(
                "temporary llm digest failure (strict_llm_output=true): empty llm digest output"
                if strict_llm_output
                else "temporary llm digest failure: empty llm digest output",
                tokens_in=llm_meta.get("tokens_in_total") or 0,
                tokens_out=llm_meta.get("tokens_out_total") or 0,
                cost_usd=str(llm_meta.get("cost_usd_total") or "0.00000000"),
                request_ids=llm_meta.get("request_ids") if isinstance(llm_meta.get("request_ids"), list) else [],
                task_run_ids=llm_meta.get("ai_usage_task_run_ids")
                if isinstance(llm_meta.get("ai_usage_task_run_ids"), list)
                else [],
            )
    else:
        if llm_enabled and not runtime_llm and top_jobs:
            llm_warnings.append("llm_disabled_at_runtime_use_llm_false")
        digest_report = _build_fallback_digest(top_jobs=top_jobs, pipeline_counts=pipeline_counts)
        llm_meta["fallback_used"] = True

    summary_object = digest_report.get("executive_summary") if isinstance(digest_report.get("executive_summary"), dict) else {}
    summary = str(summary_object.get("summary_text") or "").strip() or (
        f"Jobs digest generated for query='{request.get('query')}' in '{request.get('location')}'. "
        f"Shortlist count={len(top_jobs)}."
    )
    why_these = [
        str(row.get("why_it_fits") or "")
        for row in digest_report.get("jobs", [])
        if isinstance(row, dict) and str(row.get("why_it_fits") or "").strip()
    ]
    risks = [
        str(row.get("tradeoffs") or "")
        for row in digest_report.get("jobs", [])
        if isinstance(row, dict) and str(row.get("tradeoffs") or "").strip()
    ]

    next_actions = [
        "draft_cover_letter",
        "draft_application_answers",
        "generate_interview_prep",
        "schedule_follow_up",
    ]

    should_notify = bool(top_jobs) or notify_on_empty
    upstream_run_id = str(upstream.get("run_id") or "")
    dedupe_key = f"jobsv2:digest:{pipeline_id}:{upstream_run_id or 'unknown'}"
    digest_base_url_raw = (
        digest_policy.get("artifact_base_url")
        or payload.get("artifact_base_url")
        or os.getenv("MISSION_CONTROL_API_BASE_URL", "")
    )
    digest_base_url = str(digest_base_url_raw or "").strip() or None
    artifact_refs = _build_artifact_references(
        task_id=str(getattr(task, "id", "") or ""),
        run_id=str(getattr(task, "_run_id", "") or ""),
        pipeline_id=pipeline_id,
        base_url=digest_base_url,
    )
    discord_message = _build_discord_digest_message(
        report=digest_report,
        pipeline_counts=pipeline_counts,
        artifact_refs=artifact_refs,
    )

    jobs_digest_json_artifact = {
        "artifact_type": "jobs_digest.json.v1",
        "artifact_schema": "jobs_digest.json.v1",
        "file_name": "jobs_digest.json",
        "pipeline_id": pipeline_id,
        "generated_at": utc_iso(),
        "generation_mode": generation_mode,
        "digest_format": digest_format,
        "executive_summary": summary_object,
        "jobs": digest_report.get("jobs") if isinstance(digest_report.get("jobs"), list) else [],
        "notification_excerpt": str(digest_report.get("notification_excerpt") or ""),
        "summary_for_ui": {
            "headline": summary,
            "strongest_patterns": summary_object.get("strongest_patterns") if isinstance(summary_object.get("strongest_patterns"), list) else [],
            "best_fit_roles": summary_object.get("best_fit_roles") if isinstance(summary_object.get("best_fit_roles"), list) else [],
        },
        "artifact_references": artifact_refs,
        "notification_seed": {
            "excerpt": str(digest_report.get("notification_excerpt") or ""),
            "jobs": [
                {
                    "rank": row.get("rank"),
                    "title": row.get("title"),
                    "company": row.get("company"),
                    "source": row.get("source"),
                    "source_url": row.get("source_url"),
                }
                for row in digest_report.get("jobs", [])[:3]
                if isinstance(row, dict)
            ],
        },
    }

    markdown_report = _render_markdown(digest_report)
    jobs_digest_md_artifact = {
        "artifact_type": "jobs_digest.md.v1",
        "artifact_schema": "jobs_digest.md.v1",
        "file_name": "jobs_digest.md",
        "pipeline_id": pipeline_id,
        "generated_at": utc_iso(),
        "generation_mode": generation_mode,
        "digest_format": digest_format,
        "content": markdown_report,
        "preview": _compact_text(markdown_report, 600),
    }

    notify_payload = None
    if should_notify:
        notify_payload = {
            "source_task_type": "jobs_digest_v2",
            "channels": notify_channels,
            "message": discord_message,
            "severity": "info",
            "include_header": False,
            "include_metadata": False,
            "dedupe_key": dedupe_key,
            "metadata": {
                "pipeline_id": pipeline_id,
                "shortlist_count": len(top_jobs),
                "digest_format": digest_format,
                "generation_mode": generation_mode,
                "artifact_references": artifact_refs,
            },
        }

    artifact = {
        "artifact_type": "jobs.digest.v2",
        "artifact_schema": "jobs.digest.v3",
        "pipeline_id": pipeline_id,
        "digested_at": utc_iso(),
        "request": request,
        "digest_policy": {
            "max_items": max_items,
            "format": digest_format,
            "notify_channels": notify_channels,
            "notify_on_empty": notify_on_empty,
            "llm_enabled": llm_enabled,
            "llm_max_retries": llm_max_retries,
            "llm_retry_cost_cap_usd": str(llm_retry_cost_cap_usd),
            "strict_llm_output": strict_llm_output,
            "prompt_version": prompt_version,
        },
        "pipeline_counts": pipeline_counts,
        "summary_for_ui": jobs_digest_json_artifact["summary_for_ui"],
        "summary": summary,
        "top_jobs": top_jobs,
        "digest_jobs": jobs_digest_json_artifact["jobs"],
        "jobs_digest_json_artifact": jobs_digest_json_artifact,
        "jobs_digest_md_artifact": jobs_digest_md_artifact,
        "artifact_references": artifact_refs,
        "why_these": why_these,
        "risks": risks,
        "next_actions": next_actions,
        "notification_excerpt": jobs_digest_json_artifact["notification_excerpt"],
        "generation_mode": generation_mode,
        "model_usage": llm_meta,
        "warnings": llm_warnings,
        "notify_decision": {
            "should_notify": should_notify,
            "reason": "shortlist_non_empty" if top_jobs else ("notify_on_empty" if notify_on_empty else "skipped_empty_shortlist"),
        },
        "notify_payload": notify_payload,
        "upstream_artifact_type": upstream_type,
        "upstream": upstream,
    }

    notify_followup_spec: dict[str, Any] | None = None
    next_tasks: list[dict[str, Any]] = []
    if should_notify and isinstance(notify_payload, dict):
        notify_followup_spec = {
            "task_type": "notify_v1",
            "payload_json": notify_payload,
            "idempotency_key": stage_idempotency_key(pipeline_id, "notify_v1", upstream_run_id or "unknown", prefix="notify"),
            "max_attempts": 3,
        }
        next_tasks.append(
            notify_followup_spec
        )

    result: dict[str, Any] = {
        "artifact_type": "jobs.digest.v2",
        "content_text": discord_message,
        "content_json": artifact,
        "debug_json": {
            "pipeline_id": pipeline_id,
            "notify_followup_requested": bool(next_tasks),
            "shortlist_count": len(top_jobs),
            "notify_decision": artifact["notify_decision"],
            "notify_channels": list(notify_channels),
            "notify_dedupe_key": dedupe_key,
            "notify_followup_spec": notify_followup_spec,
            "upstream_artifact_type": upstream_type,
            "llm_runtime_enabled": runtime_llm,
            "llm_attempts": llm_meta.get("attempts"),
            "llm_stop_reason": llm_meta.get("stop_reason"),
            "llm_attempt_errors": llm_meta.get("attempt_errors"),
            "fallback_used": llm_meta.get("fallback_used"),
            "strict_llm_output": strict_llm_output,
            "strict_mode_failed": llm_meta.get("strict_failure"),
            "ai_usage_task_run_ids": llm_meta.get("ai_usage_task_run_ids"),
            "generation_mode": generation_mode,
        },
        "next_tasks": next_tasks,
    }
    if runtime_llm:
        result["usage"] = {
            "tokens_in": int(llm_meta.get("tokens_in_total") or 0),
            "tokens_out": int(llm_meta.get("tokens_out_total") or 0),
            "cost_usd": str(llm_meta.get("cost_usd_total") or "0.00000000"),
            "openai_request_ids": llm_meta.get("request_ids") if isinstance(llm_meta.get("request_ids"), list) else [],
            "ai_usage_task_run_ids": llm_meta.get("ai_usage_task_run_ids")
            if isinstance(llm_meta.get("ai_usage_task_run_ids"), list)
            else [],
        }
    return result
