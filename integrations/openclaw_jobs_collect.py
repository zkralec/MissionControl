from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from typing import Any

from integrations.jobs_collectors.base import supported_fields

OPENCLAW_SUPPORTED_SOURCES = ("glassdoor", "handshake")
DEFAULT_OPENCLAW_COMMAND_TIMEOUT_SECONDS = 180
MAX_OPENCLAW_COMMAND_TIMEOUT_SECONDS = 900
DEFAULT_OPENCLAW_MAX_SCREENSHOTS_PER_SOURCE = 6
MAX_OPENCLAW_MAX_SCREENSHOTS_PER_SOURCE = 20

SUPPORTED_FIELDS_BY_SOURCE = {
    source: {
        **supported_fields(source),
        "openclaw_enabled": True,
        "openclaw_capture_screenshots": True,
        "openclaw_max_screenshots_per_source": True,
        "openclaw_command_timeout_seconds": True,
    }
    for source in OPENCLAW_SUPPORTED_SOURCES
}


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"1", "true", "yes"}:
            return True
        if low in {"0", "false", "no"}:
            return False
    return None


def _as_bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _as_text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for row in value:
        if not isinstance(row, str):
            continue
        trimmed = row.strip()
        if trimmed:
            output.append(trimmed)
    return output


def _command_parts() -> list[str]:
    raw = str(os.getenv("OPENCLAW_COLLECTOR_COMMAND", "")).strip()
    if not raw:
        return []
    return [part for part in shlex.split(raw) if part.strip()]


def openclaw_command_configured() -> bool:
    return bool(_command_parts())


def _normalize_status(value: Any, *, collected_count: int) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {
        "success",
        "empty_success",
        "under_target",
        "auth_blocked",
        "anti_bot_blocked",
        "layout_mismatch",
        "upstream_failure",
        "skipped",
        "consent_blocked",
    }:
        return normalized
    if normalized in {"partial_success", "partial"}:
        return "under_target" if collected_count > 0 else "upstream_failure"
    if normalized in {"auth", "login_blocked"}:
        return "auth_blocked"
    if normalized in {"anti_bot", "blocked_by_bot"}:
        return "anti_bot_blocked"
    if normalized in {"layout_error", "selector_mismatch"}:
        return "layout_mismatch"
    if normalized in {"empty", "no_results"}:
        return "empty_success"
    if collected_count > 0:
        return "success"
    return "empty_success"


def _normalize_blocking_reason(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    aliases = {
        "login_wall": "login_wall",
        "login_wall_detected": "login_wall",
        "auth_required": "auth_required",
        "auth_blocked": "auth_required",
        "anti_bot": "anti_bot_detected",
        "anti_bot_detected": "anti_bot_detected",
        "anti_bot_blocked": "anti_bot_detected",
        "consent_blocked": "consent_wall_detected",
        "consent_wall": "consent_wall_detected",
        "consent_wall_detected": "consent_wall_detected",
        "layout_mismatch": "layout_mismatch",
        "selector_mismatch": "layout_mismatch",
        "unexpected_redirect": "unexpected_redirect",
    }
    return aliases.get(normalized, normalized)


def _normalize_screenshot_reference(source: str, row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    path = str(row.get("path") or row.get("file_path") or "").strip() or None
    url = str(row.get("url") or row.get("image_url") or "").strip() or None
    if not path and not url:
        return None
    return {
        "source": source,
        "label": str(row.get("label") or row.get("name") or "").strip() or None,
        "path": path,
        "url": url,
        "captured_at": str(row.get("captured_at") or "").strip() or None,
        "page_url": str(row.get("page_url") or "").strip() or None,
        "mime_type": str(row.get("mime_type") or "").strip() or None,
        "kind": str(row.get("kind") or "screenshot").strip() or "screenshot",
    }


def _normalize_job(source: str, row: Any, *, url_override: str | None) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    source_metadata = row.get("source_metadata") if isinstance(row.get("source_metadata"), dict) else {}
    metadata = dict(source_metadata)
    metadata.update(
        {
            "openclaw_job_id": str(row.get("job_id") or row.get("id") or "").strip() or None,
            "openclaw_status": str(row.get("status") or "").strip() or None,
            "raw": raw,
        }
    )
    screenshot_refs = []
    for item in row.get("screenshots") if isinstance(row.get("screenshots"), list) else []:
        normalized = _normalize_screenshot_reference(source, item)
        if normalized is not None:
            screenshot_refs.append(normalized)
    if screenshot_refs:
        metadata["screenshot_references"] = screenshot_refs

    source_url = (
        str(row.get("source_url") or raw.get("search_url") or row.get("search_url") or url_override or "").strip()
        or None
    )
    return {
        "source": source,
        "source_url": source_url,
        "title": row.get("title"),
        "company": row.get("company"),
        "location": row.get("location"),
        "location_normalized": row.get("location_normalized"),
        "url": row.get("url"),
        "salary_min": row.get("salary_min"),
        "salary_max": row.get("salary_max"),
        "salary_text": row.get("salary_text"),
        "salary_currency": row.get("salary_currency"),
        "experience_level": row.get("experience_level"),
        "work_mode": row.get("work_mode"),
        "posted_at": row.get("posted_at"),
        "posted_age_days": row.get("posted_age_days"),
        "scraped_at": row.get("scraped_at"),
        "description_snippet": row.get("description_snippet"),
        "source_metadata": metadata,
    }


def _build_openclaw_request(source: str, request: dict[str, Any], *, url_override: str | None) -> dict[str, Any]:
    capture_screenshots = _as_bool(request.get("openclaw_capture_screenshots"))
    if capture_screenshots is None:
        capture_screenshots = True
    max_screenshots = _as_bounded_int(
        request.get("openclaw_max_screenshots_per_source"),
        default=DEFAULT_OPENCLAW_MAX_SCREENSHOTS_PER_SOURCE,
        minimum=0,
        maximum=MAX_OPENCLAW_MAX_SCREENSHOTS_PER_SOURCE,
    )
    return {
        "source": source,
        "query": str(request.get("query") or "").strip(),
        "titles": _as_text_list(request.get("titles")),
        "keywords": _as_text_list(request.get("keywords")),
        "excluded_keywords": _as_text_list(request.get("excluded_keywords")),
        "desired_title_keywords": _as_text_list(request.get("desired_title_keywords")),
        "locations": _as_text_list(request.get("locations")),
        "work_modes": _as_text_list(request.get("work_modes")),
        "experience_levels": _as_text_list(request.get("experience_levels")),
        "result_limit_per_source": int(request.get("result_limit_per_source") or 0),
        "max_pages_per_source": int(request.get("max_pages_per_source") or 0),
        "minimum_jobs_per_source": int(request.get("minimum_jobs_per_source") or 0),
        "stop_when_minimum_reached": bool(request.get("stop_when_minimum_reached", True)),
        "collection_time_cap_seconds": request.get("collection_time_cap_seconds"),
        "board_url_override": url_override,
        "capture_screenshots": bool(capture_screenshots),
        "max_screenshots_per_source": max_screenshots,
        "profile_mode": str(request.get("profile_mode") or "").strip() or None,
    }


def _invoke_openclaw(command: list[str], payload: dict[str, Any], *, timeout_seconds: int) -> tuple[dict[str, Any], int]:
    started = time.monotonic()
    completed = subprocess.run(
        command,
        input=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
        check=False,
    )
    runtime_ms = int((time.monotonic() - started) * 1000)

    if completed.returncode != 0:
        return (
            {
                "status": "upstream_failure",
                "warnings": [],
                "errors": [f"openclaw_command_failed_exit_{completed.returncode}"],
                "source_error_type": "openclaw_command_failed",
            },
            runtime_ms,
        )

    stdout = completed.stdout.decode("utf-8", errors="replace").strip()
    if not stdout:
        return (
            {
                "status": "upstream_failure",
                "warnings": [],
                "errors": ["openclaw_command_empty_output"],
                "source_error_type": "openclaw_empty_output",
            },
            runtime_ms,
        )

    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return (
            {
                "status": "upstream_failure",
                "warnings": [],
                "errors": ["openclaw_command_invalid_json"],
                "source_error_type": "openclaw_invalid_json",
            },
            runtime_ms,
        )

    if not isinstance(parsed, dict):
        return (
            {
                "status": "upstream_failure",
                "warnings": [],
                "errors": ["openclaw_command_non_object_response"],
                "source_error_type": "openclaw_invalid_response_shape",
            },
            runtime_ms,
        )
    return parsed, runtime_ms


def collect_openclaw_source_jobs(source: str, request: dict[str, Any], *, url_override: str | None = None) -> dict[str, Any]:
    source_key = str(source).strip().lower()
    if source_key not in OPENCLAW_SUPPORTED_SOURCES:
        return {
            "status": "upstream_failure",
            "jobs": [],
            "warnings": [],
            "errors": [f"{source_key}: unsupported_openclaw_source"],
            "meta": {
                "source_status": "upstream_failure",
                "source_error_type": "unsupported_openclaw_source",
                "parsing_strategy_used": "openclaw_browser",
                "browser_fallback_used": True,
            },
        }

    command = _command_parts()
    if not command:
        return {
            "status": "upstream_failure",
            "jobs": [],
            "warnings": [],
            "errors": [f"{source_key}: openclaw_not_configured"],
            "meta": {
                "source_status": "upstream_failure",
                "source_error_type": "openclaw_not_configured",
                "parsing_strategy_used": "openclaw_browser",
                "browser_fallback_used": True,
            },
        }

    timeout_seconds = _as_bounded_int(
        request.get("openclaw_command_timeout_seconds") or os.getenv("OPENCLAW_COMMAND_TIMEOUT_SECONDS"),
        default=DEFAULT_OPENCLAW_COMMAND_TIMEOUT_SECONDS,
        minimum=5,
        maximum=MAX_OPENCLAW_COMMAND_TIMEOUT_SECONDS,
    )

    payload = _build_openclaw_request(source_key, request, url_override=url_override)
    try:
        response, runtime_ms = _invoke_openclaw(command, payload, timeout_seconds=timeout_seconds)
    except subprocess.TimeoutExpired:
        return {
            "status": "upstream_failure",
            "jobs": [],
            "warnings": [],
            "errors": [f"{source_key}: openclaw_timeout"],
            "meta": {
                "source_status": "upstream_failure",
                "source_error_type": "openclaw_timeout",
                "openclaw_runtime_ms": timeout_seconds * 1000,
                "parsing_strategy_used": "openclaw_browser",
                "browser_fallback_used": True,
            },
        }

    warnings = [str(row).strip() for row in response.get("warnings") if isinstance(row, str)] if isinstance(response.get("warnings"), list) else []
    errors = [str(row).strip() for row in response.get("errors") if isinstance(row, str)] if isinstance(response.get("errors"), list) else []
    jobs_rows = response.get("jobs") if isinstance(response.get("jobs"), list) else []
    jobs = [job for row in jobs_rows if (job := _normalize_job(source_key, row, url_override=url_override)) is not None]

    source_summary = response.get("source_summary") if isinstance(response.get("source_summary"), dict) else {}
    screenshot_references = [
        normalized
        for row in (
            response.get("screenshots")
            if isinstance(response.get("screenshots"), list)
            else response.get("screenshot_references")
            if isinstance(response.get("screenshot_references"), list)
            else []
        )
        if (normalized := _normalize_screenshot_reference(source_key, row)) is not None
    ]
    blocking_reason = _normalize_blocking_reason(
        response.get("blocking_reason") or response.get("source_error_type") or source_summary.get("blocking_reason")
    )
    status = _normalize_status(
        response.get("status") or source_summary.get("status") or response.get("source_status"),
        collected_count=len(jobs),
    )
    pages_attempted = _as_bounded_int(
        source_summary.get("pages_attempted") or response.get("pages_attempted") or response.get("pages_fetched") or 0,
        default=0,
        minimum=0,
        maximum=10_000,
    )
    queries_executed = _as_bounded_int(
        source_summary.get("queries_executed_count") or response.get("queries_executed_count") or 1,
        default=1,
        minimum=0,
        maximum=1_000,
    )
    empty_queries = _as_bounded_int(
        source_summary.get("empty_queries_count") or response.get("empty_queries_count") or 0,
        default=0,
        minimum=0,
        maximum=1_000,
    )
    requested_limit = _as_bounded_int(
        request.get("result_limit_per_source") or source_summary.get("requested_limit") or len(jobs),
        default=len(jobs),
        minimum=0,
        maximum=100_000,
    )
    query_examples = _as_text_list(source_summary.get("query_examples")) or [str(payload.get("query") or "").strip()] if str(payload.get("query") or "").strip() else []
    request_urls_tried = _as_text_list(source_summary.get("request_urls_tried")) or _as_text_list(response.get("request_urls_tried"))
    search_attempts = response.get("search_attempts") if isinstance(response.get("search_attempts"), list) else []
    if not search_attempts:
        search_attempts = [
            {
                "query": str(payload.get("query") or "").strip(),
                "location": ", ".join(payload.get("locations") or []),
                "pages_attempted": pages_attempted,
                "jobs_found": len(jobs),
                "jobs_kept": len(jobs),
                "source_status": status,
                "source_error_type": blocking_reason,
                "request_urls_tried": request_urls_tried,
            }
        ]

    meta = {
        "source_status": status,
        "source_error_type": blocking_reason,
        "blocking_reason": blocking_reason,
        "requested_limit": requested_limit,
        "discovered_raw_count": _as_bounded_int(
            source_summary.get("discovered_raw_count") or response.get("jobs_raw") or len(jobs),
            default=len(jobs),
            minimum=0,
            maximum=100_000,
        ),
        "kept_after_basic_filter_count": _as_bounded_int(
            source_summary.get("kept_after_basic_filter_count") or response.get("jobs_kept") or len(jobs),
            default=len(jobs),
            minimum=0,
            maximum=100_000,
        ),
        "dropped_by_basic_filter_count": _as_bounded_int(
            source_summary.get("dropped_by_basic_filter_count") or response.get("jobs_dropped") or 0,
            default=0,
            minimum=0,
            maximum=100_000,
        ),
        "deduped_count": _as_bounded_int(
            source_summary.get("deduped_count") or response.get("deduped_count") or 0,
            default=0,
            minimum=0,
            maximum=100_000,
        ),
        "returned_count": len(jobs),
        "jobs_found_per_source": _as_bounded_int(
            source_summary.get("jobs_found_per_source") or response.get("jobs_found_per_source") or len(jobs),
            default=len(jobs),
            minimum=0,
            maximum=100_000,
        ),
        "queries_executed_count": queries_executed,
        "empty_queries_count": empty_queries,
        "query_examples": query_examples[:10],
        "search_attempts": search_attempts,
        "request_urls_tried": request_urls_tried,
        "last_request_url": request_urls_tried[-1] if request_urls_tried else None,
        "pages_attempted": pages_attempted,
        "pages_fetched": _as_bounded_int(
            source_summary.get("pages_fetched") or response.get("pages_fetched") or pages_attempted,
            default=pages_attempted,
            minimum=0,
            maximum=10_000,
        ),
        "cards_seen": _as_bounded_int(
            source_summary.get("cards_seen") or response.get("cards_seen") or len(jobs),
            default=len(jobs),
            minimum=0,
            maximum=100_000,
        ),
        "listing_cards_seen": _as_bounded_int(
            source_summary.get("listing_cards_seen") or response.get("listing_cards_seen") or len(jobs),
            default=len(jobs),
            minimum=0,
            maximum=100_000,
        ),
        "jobs_raw": _as_bounded_int(
            source_summary.get("jobs_raw") or response.get("jobs_raw") or len(jobs),
            default=len(jobs),
            minimum=0,
            maximum=100_000,
        ),
        "jobs_kept": _as_bounded_int(
            source_summary.get("jobs_kept") or response.get("jobs_kept") or len(jobs),
            default=len(jobs),
            minimum=0,
            maximum=100_000,
        ),
        "screenshot_references": screenshot_references,
        "screenshot_count": len(screenshot_references),
        "openclaw_runtime_ms": runtime_ms,
        "parsing_strategy_used": "openclaw_browser",
        "browser_fallback_used": True,
        "collection_stop_reason": str(source_summary.get("collection_stop_reason") or response.get("collection_stop_reason") or "exhausted").strip() or "exhausted",
        "layout_mismatch_detected": status == "layout_mismatch" or blocking_reason == "layout_mismatch",
        "anti_bot_detected": status == "anti_bot_blocked" or blocking_reason == "anti_bot_detected",
        "auth_required_detected": status == "auth_blocked" or blocking_reason in {"auth_required", "login_wall"},
        "login_wall_detected": blocking_reason == "login_wall",
        "consent_wall_detected": status == "consent_blocked" or blocking_reason == "consent_wall_detected",
        "unexpected_redirect_detected": blocking_reason == "unexpected_redirect",
        "metadata_completeness_summary": source_summary.get("metadata_completeness_summary")
        if isinstance(source_summary.get("metadata_completeness_summary"), dict)
        else {
            "job_count": len(jobs),
            "missing_company": 0,
            "missing_posted_at": 0,
            "missing_source_url": 0,
            "missing_location": 0,
        },
    }
    return {
        "status": status,
        "jobs": jobs,
        "warnings": warnings,
        "errors": errors,
        "meta": meta,
    }
