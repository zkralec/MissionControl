from __future__ import annotations

import os
import time
from typing import Any

from integrations.openclaw_jobs_collect import (
    OPENCLAW_SUPPORTED_SOURCES,
    SUPPORTED_FIELDS_BY_SOURCE,
    collect_openclaw_source_jobs,
    openclaw_command_configured,
)
from task_handlers.jobs_collect_v1 import (
    DEGRADED_SOURCE_STATUSES,
    EMPTY_SOURCE_STATUSES,
    FAILED_SOURCE_STATUSES,
    HEALTHY_SOURCE_STATUSES,
    SUCCESSFUL_SOURCE_STATUSES,
    _build_collection_observability,
    _build_run_preview_messages,
    _display_source_name,
    _empty_metadata_summary,
    _meta_count,
    _normalize_source_status,
    _prefix_source_message,
    _unique_job_count,
)
from task_handlers.jobs_pipeline_common import (
    build_upstream_ref,
    new_pipeline_id,
    payload_object,
    resolve_request,
    source_counts,
    stage_idempotency_key,
    utc_iso,
)

ACTIVE_OPENCLAW_SOURCES = OPENCLAW_SUPPORTED_SOURCES


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


def _requested_openclaw_sources(raw_request: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    raw_sources = raw_request.get("sources") if isinstance(raw_request.get("sources"), list) else raw_request.get("enabled_sources")
    requested = []
    if isinstance(raw_sources, list):
        for row in raw_sources:
            if not isinstance(row, str):
                continue
            key = row.strip().lower()
            if key and key not in requested:
                requested.append(key)
    if not requested:
        requested = list(ACTIVE_OPENCLAW_SOURCES)

    sources: list[str] = []
    ignored_active_sources: list[str] = []
    unsupported_sources: list[str] = []
    for source in requested:
        if source in ACTIVE_OPENCLAW_SOURCES:
            sources.append(source)
        elif source in {"linkedin", "indeed", "manual"}:
            ignored_active_sources.append(source)
        else:
            unsupported_sources.append(source)
    if not sources:
        sources = list(ACTIVE_OPENCLAW_SOURCES)
    return sources, ignored_active_sources, unsupported_sources


def _resolve_openclaw_request(raw_request: dict[str, Any]) -> dict[str, Any]:
    request = resolve_request(raw_request)
    sources, ignored_active_sources, unsupported_sources = _requested_openclaw_sources(raw_request)
    source_configuration_notes: list[str] = []
    if ignored_active_sources:
        source_configuration_notes.append(
            "OpenClaw phase 1 only targets handshake and glassdoor. LinkedIn and Indeed stay on jobs_collect_v1."
        )
    if unsupported_sources:
        source_configuration_notes.append(
            "Unsupported OpenClaw job sources were ignored: " + ", ".join(sorted(unsupported_sources)) + "."
        )

    openclaw_enabled = _as_bool(raw_request.get("openclaw_enabled"))
    if openclaw_enabled is None:
        openclaw_enabled = _as_bool(os.getenv("OPENCLAW_ENABLED"))
    if openclaw_enabled is None:
        openclaw_enabled = False

    capture_screenshots = _as_bool(raw_request.get("openclaw_capture_screenshots"))
    if capture_screenshots is None:
        capture_screenshots = True

    request.update(
        {
            "sources": list(sources),
            "enabled_sources": list(sources),
            "requested_sources_original": list(sources + ignored_active_sources + unsupported_sources),
            "disabled_sources": [],
            "unsupported_sources": list(unsupported_sources),
            "source_configuration_notes": source_configuration_notes,
            "openclaw_enabled": bool(openclaw_enabled),
            "openclaw_capture_screenshots": bool(capture_screenshots),
            "openclaw_max_screenshots_per_source": raw_request.get("openclaw_max_screenshots_per_source"),
            "openclaw_command_timeout_seconds": raw_request.get("openclaw_command_timeout_seconds"),
        }
    )
    return request


def execute(task: Any, db: Any) -> dict[str, Any]:
    del db

    payload = payload_object(task.payload_json)
    raw_request = payload.get("request") if isinstance(payload.get("request"), dict) else payload
    request = _resolve_openclaw_request(raw_request if isinstance(raw_request, dict) else {})
    pipeline_id = new_pipeline_id(payload.get("pipeline_id"))

    raw_jobs: list[dict[str, Any]] = []
    warnings: list[str] = []
    collector_errors: list[str] = []
    source_results: dict[str, dict[str, Any]] = {}
    supported_fields_by_source: dict[str, dict[str, Any]] = {}
    sources = list(request.get("sources") or list(ACTIVE_OPENCLAW_SOURCES))
    board_url_overrides = request.get("board_url_overrides") if isinstance(request.get("board_url_overrides"), dict) else {}
    max_total_jobs = max(1, int(request.get("max_total_jobs") or len(sources) or 1))
    minimum_raw_jobs_total = max(0, int(request.get("minimum_raw_jobs_total") or 0))
    minimum_unique_jobs_total = max(0, int(request.get("minimum_unique_jobs_total") or 0))
    minimum_jobs_per_source = max(0, int(request.get("minimum_jobs_per_source") or 0))
    stop_when_minimum_reached = bool(request.get("stop_when_minimum_reached", True))
    raw_collection_time_cap = request.get("collection_time_cap_seconds")
    try:
        collection_time_cap_seconds = max(1, int(raw_collection_time_cap)) if raw_collection_time_cap not in (None, "") else None
    except (TypeError, ValueError):
        collection_time_cap_seconds = None
    collection_started_monotonic = time.monotonic()
    collection_deadline_monotonic = (
        collection_started_monotonic + collection_time_cap_seconds
        if collection_time_cap_seconds is not None
        else None
    )
    minimum_target_requested = any(
        value > 0 for value in (minimum_raw_jobs_total, minimum_unique_jobs_total, minimum_jobs_per_source)
    )
    stopped_by_minimum = False
    stopped_by_time_cap = False
    stopped_by_safety_cap = False

    def _current_discovered_raw_total() -> int:
        total = 0
        for result in source_results.values():
            meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
            jobs_count = int(result.get("jobs_count", 0) or 0)
            total += _meta_count(meta, "discovered_raw_count", jobs_count)
        return total

    def _minimum_state() -> dict[str, Any]:
        current_raw_jobs_total = _current_discovered_raw_total()
        current_unique_jobs_total = _unique_job_count(raw_jobs)
        per_source_shortfalls: dict[str, int] = {}
        if minimum_jobs_per_source > 0:
            for configured_source in [str(item).strip().lower() for item in sources if str(item).strip()]:
                current_count = int(source_results.get(configured_source, {}).get("jobs_count", 0) or 0)
                shortfall = max(minimum_jobs_per_source - current_count, 0)
                if shortfall > 0:
                    per_source_shortfalls[configured_source] = shortfall
        raw_jobs_shortfall = max(minimum_raw_jobs_total - current_raw_jobs_total, 0)
        unique_jobs_shortfall = max(minimum_unique_jobs_total - current_unique_jobs_total, 0)
        minimum_reached = not minimum_target_requested or (
            raw_jobs_shortfall <= 0
            and unique_jobs_shortfall <= 0
            and not per_source_shortfalls
        )
        shortfall_parts: list[str] = []
        if raw_jobs_shortfall > 0:
            shortfall_parts.append(f"{raw_jobs_shortfall} raw jobs")
        if unique_jobs_shortfall > 0:
            shortfall_parts.append(f"{unique_jobs_shortfall} unique jobs")
        for source_key, shortfall in per_source_shortfalls.items():
            shortfall_parts.append(f"{_display_source_name(source_key)} needs {shortfall} more")
        return {
            "minimum_target_requested": minimum_target_requested,
            "minimum_raw_jobs_total_requested": minimum_raw_jobs_total,
            "minimum_unique_jobs_total_requested": minimum_unique_jobs_total,
            "minimum_jobs_per_source_requested": minimum_jobs_per_source,
            "stop_when_minimum_reached": stop_when_minimum_reached,
            "collection_time_cap_seconds": collection_time_cap_seconds,
            "current_raw_jobs_total": current_raw_jobs_total,
            "current_unique_jobs_total": current_unique_jobs_total,
            "per_source_shortfalls": per_source_shortfalls,
            "raw_jobs_shortfall": raw_jobs_shortfall,
            "unique_jobs_shortfall": unique_jobs_shortfall,
            "minimum_reached": minimum_reached,
            "shortfall_summary": ", ".join(shortfall_parts),
        }

    for note in request.get("source_configuration_notes") if isinstance(request.get("source_configuration_notes"), list) else []:
        if isinstance(note, str) and note.strip() and note.strip() not in warnings:
            warnings.append(note.strip())

    if not bool(request.get("openclaw_enabled")):
        disabled_note = "OpenClaw collection is disabled. Set OPENCLAW_ENABLED=true or request.openclaw_enabled=true to enable it."
        warnings.append(disabled_note)
        for source_key in sources:
            source_results[source_key] = {
                "source": source_key,
                "status": "skipped",
                "jobs_count": 0,
                "warnings": [disabled_note],
                "errors": [],
                "error": None,
                "meta": {
                    "reason": "openclaw_disabled",
                    "source_status": "skipped",
                    "source_error_type": "openclaw_disabled",
                    "collection_method": "openclaw",
                    "collection_stop_reason": "disabled",
                    "parsing_strategy_used": "openclaw_browser",
                    "browser_fallback_used": True,
                    "screenshot_references": [],
                },
            }
            supported_fields_by_source[source_key] = dict(SUPPORTED_FIELDS_BY_SOURCE.get(source_key) or {})
    elif not openclaw_command_configured():
        config_note = "OpenClaw collector command is not configured. Set OPENCLAW_COLLECTOR_COMMAND to enable browser collection."
        warnings.append(config_note)
        for source_key in sources:
            source_results[source_key] = {
                "source": source_key,
                "status": "skipped",
                "jobs_count": 0,
                "warnings": [config_note],
                "errors": [],
                "error": None,
                "meta": {
                    "reason": "openclaw_not_configured",
                    "source_status": "skipped",
                    "source_error_type": "openclaw_not_configured",
                    "collection_method": "openclaw",
                    "collection_stop_reason": "disabled",
                    "parsing_strategy_used": "openclaw_browser",
                    "browser_fallback_used": True,
                    "screenshot_references": [],
                },
            }
            supported_fields_by_source[source_key] = dict(SUPPORTED_FIELDS_BY_SOURCE.get(source_key) or {})
    else:
        for source_index, source_key in enumerate(sources):
            minimum_state_before_source = _minimum_state()
            if minimum_target_requested and stop_when_minimum_reached and minimum_state_before_source["minimum_reached"]:
                stopped_by_minimum = True
                source_results[source_key] = {
                    "source": source_key,
                    "status": "skipped",
                    "jobs_count": 0,
                    "warnings": [],
                    "errors": [],
                    "error": None,
                    "meta": {
                        "reason": "minimum_reached",
                        "source_status": "skipped",
                        "collection_method": "openclaw",
                        "collection_stop_reason": "minimum_reached",
                        "parsing_strategy_used": "openclaw_browser",
                        "browser_fallback_used": True,
                        "screenshot_references": [],
                    },
                }
                supported_fields_by_source[source_key] = dict(SUPPORTED_FIELDS_BY_SOURCE.get(source_key) or {})
                continue

            if collection_deadline_monotonic is not None and time.monotonic() >= collection_deadline_monotonic:
                stopped_by_time_cap = True
                source_results[source_key] = {
                    "source": source_key,
                    "status": "skipped",
                    "jobs_count": 0,
                    "warnings": [],
                    "errors": [],
                    "error": None,
                    "meta": {
                        "reason": "time_cap_reached",
                        "source_status": "skipped",
                        "collection_method": "openclaw",
                        "collection_stop_reason": "time_cap",
                        "parsing_strategy_used": "openclaw_browser",
                        "browser_fallback_used": True,
                        "screenshot_references": [],
                    },
                }
                supported_fields_by_source[source_key] = dict(SUPPORTED_FIELDS_BY_SOURCE.get(source_key) or {})
                continue

            remaining_total_jobs = max_total_jobs - len(raw_jobs)
            if remaining_total_jobs <= 0:
                stopped_by_safety_cap = True
                source_results[source_key] = {
                    "source": source_key,
                    "status": "skipped",
                    "jobs_count": 0,
                    "warnings": [],
                    "errors": [],
                    "error": None,
                    "meta": {
                        "reason": "max_total_jobs_reached",
                        "remaining_total_jobs": 0,
                        "source_status": "skipped",
                        "collection_method": "openclaw",
                        "collection_stop_reason": "safety_cap",
                        "parsing_strategy_used": "openclaw_browser",
                        "browser_fallback_used": True,
                        "screenshot_references": [],
                    },
                }
                supported_fields_by_source[source_key] = dict(SUPPORTED_FIELDS_BY_SOURCE.get(source_key) or {})
                continue

            source_request = dict(request)
            remaining_sources_count = max(1, len([row for row in sources[source_index:] if str(row).strip()]))
            source_minimum_shortfall = int(minimum_state_before_source["per_source_shortfalls"].get(source_key, 0) or 0)
            unique_share_target = (
                (int(minimum_state_before_source["unique_jobs_shortfall"]) + remaining_sources_count - 1)
                // remaining_sources_count
                if int(minimum_state_before_source["unique_jobs_shortfall"]) > 0
                else 0
            )
            raw_share_target = (
                (int(minimum_state_before_source["raw_jobs_shortfall"]) + remaining_sources_count - 1)
                // remaining_sources_count
                if int(minimum_state_before_source["raw_jobs_shortfall"]) > 0
                else 0
            )
            configured_limit = int(source_request.get("result_limit_per_source") or remaining_total_jobs)
            per_source_limit = min(
                max(configured_limit, source_minimum_shortfall, unique_share_target, raw_share_target),
                remaining_total_jobs,
            )
            source_request["minimum_jobs_per_source"] = max(source_minimum_shortfall, unique_share_target)
            source_request["result_limit_per_source"] = per_source_limit
            source_request["max_jobs_per_source"] = per_source_limit
            source_request["max_jobs_per_board"] = per_source_limit
            if collection_deadline_monotonic is not None:
                source_request["_collection_deadline_monotonic"] = collection_deadline_monotonic

            collector_result = collect_openclaw_source_jobs(
                source_key,
                source_request,
                url_override=(str(board_url_overrides.get(source_key) or "").strip() or None),
            )
            collected = collector_result.get("jobs") if isinstance(collector_result.get("jobs"), list) else []
            source_warnings_raw = collector_result.get("warnings") if isinstance(collector_result.get("warnings"), list) else []
            source_errors_raw = collector_result.get("errors") if isinstance(collector_result.get("errors"), list) else []
            source_warnings = [_prefix_source_message(source_key, str(row)) for row in source_warnings_raw if str(row).strip()]
            source_errors = [_prefix_source_message(source_key, str(row)) for row in source_errors_raw if str(row).strip()]
            source_meta = dict(collector_result.get("meta") if isinstance(collector_result.get("meta"), dict) else {})
            source_meta["collection_method"] = "openclaw"
            source_meta.setdefault("parsing_strategy_used", "openclaw_browser")
            source_meta.setdefault("browser_fallback_used", True)
            source_truncated_by_run_limit = 0

            if len(collected) > remaining_total_jobs:
                source_truncated_by_run_limit = len(collected) - remaining_total_jobs
                stopped_by_safety_cap = True
                collected = collected[:remaining_total_jobs]
                source_warnings.append(_prefix_source_message(source_key, f"truncated_to_run_limit:{remaining_total_jobs}"))
            source_meta["truncated_by_run_limit_count"] = max(
                _meta_count(source_meta, "truncated_by_run_limit_count", 0),
                source_truncated_by_run_limit,
            )
            source_meta["jobs_found_per_source"] = _meta_count(source_meta, "jobs_found_per_source", len(collected))
            source_meta["minimum_jobs_per_source_requested"] = max(
                _meta_count(source_meta, "minimum_jobs_per_source_requested", 0),
                int(source_request.get("minimum_jobs_per_source") or 0),
            )
            source_meta["stop_when_minimum_reached"] = stop_when_minimum_reached
            if not str(source_meta.get("collection_stop_reason") or "").strip():
                source_meta["collection_stop_reason"] = "safety_cap" if source_truncated_by_run_limit > 0 else "exhausted"

            status_raw = _normalize_source_status(
                status_raw=str(collector_result.get("status") or "").strip().lower(),
                collected_count=len(collected),
                source_errors=source_errors,
                source_meta=source_meta,
            )

            raw_jobs.extend([row for row in collected if isinstance(row, dict)])
            warnings.extend(source_warnings)
            collector_errors.extend(source_errors)
            source_results[source_key] = {
                "source": source_key,
                "status": status_raw,
                "jobs_count": len(collected),
                "warnings": source_warnings,
                "errors": source_errors,
                "error": source_errors[0] if source_errors else None,
                "meta": source_meta,
            }
            supported_fields_by_source[source_key] = dict(SUPPORTED_FIELDS_BY_SOURCE.get(source_key) or {})

    successful_sources = [key for key, row in source_results.items() if row.get("status") in SUCCESSFUL_SOURCE_STATUSES]
    healthy_sources = [key for key, row in source_results.items() if row.get("status") in HEALTHY_SOURCE_STATUSES]
    empty_sources = [key for key, row in source_results.items() if row.get("status") in EMPTY_SOURCE_STATUSES]
    under_target_sources = [key for key, row in source_results.items() if row.get("status") in DEGRADED_SOURCE_STATUSES]
    failed_sources = [key for key, row in source_results.items() if row.get("status") in FAILED_SOURCE_STATUSES]
    skipped_sources = [key for key, row in source_results.items() if row.get("status") == "skipped"]
    minimum_state = _minimum_state()
    if minimum_target_requested and stop_when_minimum_reached and minimum_state["minimum_reached"]:
        stopped_by_minimum = True

    discovered_raw_count = 0
    kept_after_basic_filter_count = 0
    dropped_by_basic_filter_count = 0
    deduped_count = 0
    pages_fetched = 0
    queries_attempted: list[str] = []
    queries_executed_count = 0
    empty_queries_count = 0
    query_examples: list[str] = []
    metadata_completeness_summary = _empty_metadata_summary()
    source_metadata_quality: dict[str, dict[str, int]] = {}
    screenshot_references: list[dict[str, Any]] = []

    for source_key, result in source_results.items():
        meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
        jobs_count = int(result.get("jobs_count", 0) or 0)
        discovered_raw_count += _meta_count(meta, "discovered_raw_count", jobs_count)
        kept_after_basic_filter_count += _meta_count(meta, "kept_after_basic_filter_count", jobs_count)
        dropped_by_basic_filter_count += _meta_count(meta, "dropped_by_basic_filter_count", 0)
        deduped_count += _meta_count(meta, "deduped_count", 0)
        pages_fetched += _meta_count(meta, "pages_fetched", 0)
        queries_executed_count += _meta_count(meta, "queries_executed_count", 0)
        empty_queries_count += _meta_count(meta, "empty_queries_count", 0)
        if isinstance(meta.get("query_examples"), list):
            for value in meta.get("query_examples") or []:
                if isinstance(value, str) and value.strip() and value.strip() not in query_examples:
                    query_examples.append(value.strip())
        if isinstance(meta.get("request_urls_tried"), list):
            for value in meta.get("request_urls_tried") or []:
                if isinstance(value, str) and value.strip() and value.strip() not in queries_attempted:
                    queries_attempted.append(value.strip())
        source_summary = meta.get("metadata_completeness_summary") if isinstance(meta.get("metadata_completeness_summary"), dict) else None
        if source_summary:
            normalized_summary = {
                "job_count": _meta_count(source_summary, "job_count", jobs_count),
                "missing_company": _meta_count(source_summary, "missing_company", 0),
                "missing_posted_at": _meta_count(source_summary, "missing_posted_at", 0),
                "missing_source_url": _meta_count(source_summary, "missing_source_url", 0),
                "missing_location": _meta_count(source_summary, "missing_location", 0),
            }
            source_metadata_quality[source_key] = normalized_summary
            for key, value in normalized_summary.items():
                metadata_completeness_summary[key] += value
        if isinstance(meta.get("screenshot_references"), list):
            for row in meta.get("screenshot_references") or []:
                if isinstance(row, dict):
                    screenshot_references.append(row)

    reason_stopped = "exhausted"
    if minimum_target_requested and stop_when_minimum_reached and minimum_state["minimum_reached"]:
        reason_stopped = "minimum_reached"
    elif stopped_by_time_cap:
        reason_stopped = "time_cap"
    elif stopped_by_safety_cap or len(raw_jobs) >= max_total_jobs:
        reason_stopped = "safety_cap"
    minimum_state["reason_stopped"] = reason_stopped

    run_preview_messages = _build_run_preview_messages(
        source_results=source_results,
        focus_sources=tuple(sources),
    )
    if minimum_target_requested:
        if minimum_state["minimum_reached"]:
            run_preview_messages.append("Minimum jobs target reached before ranking.")
        elif minimum_state["shortfall_summary"]:
            run_preview_messages.append(f"Minimum jobs target shortfall: {minimum_state['shortfall_summary']}")

    collection_observability = _build_collection_observability(
        source_results=source_results,
        source_metadata_quality=source_metadata_quality,
        discovered_raw_count=discovered_raw_count,
        kept_after_basic_filter_count=kept_after_basic_filter_count,
        dropped_by_basic_filter_count=dropped_by_basic_filter_count,
        deduped_count=deduped_count,
        raw_job_count=len(raw_jobs),
        successful_sources=successful_sources,
        healthy_sources=healthy_sources,
        max_total_jobs=max_total_jobs,
        truncated_by_run_limit_count=0,
        run_preview_messages=run_preview_messages,
        minimum_targets=minimum_state,
        focus_sources=tuple(sources),
    )

    collection_status = "success"
    if skipped_sources or failed_sources or under_target_sources or (minimum_target_requested and not minimum_state["minimum_reached"]):
        collection_status = "partial_success"

    source_health_summary_artifact = {
        "artifact_type": "openclaw.source_summary.v1",
        "artifact_schema": "openclaw.source_summary.v1",
        "collected_at": utc_iso(),
        "sources": {
            key: {
                "status": str(row.get("status") or "").strip() or "unknown",
                "jobs_count": int(row.get("jobs_count", 0) or 0),
                "source_status": str(((row.get("meta") if isinstance(row.get("meta"), dict) else {}) or {}).get("source_status") or "").strip() or None,
                "source_error_type": str(((row.get("meta") if isinstance(row.get("meta"), dict) else {}) or {}).get("source_error_type") or "").strip() or None,
                "collection_method": str(((row.get("meta") if isinstance(row.get("meta"), dict) else {}) or {}).get("collection_method") or "").strip() or None,
                "pages_attempted": _meta_count((row.get("meta") if isinstance(row.get("meta"), dict) else {}) or {}, "pages_attempted", 0),
                "screenshot_count": _meta_count((row.get("meta") if isinstance(row.get("meta"), dict) else {}) or {}, "screenshot_count", 0),
            }
            for key, row in source_results.items()
        },
    }

    artifact = {
        "artifact_type": "jobs.collect.v1",
        "artifact_schema": "jobs_raw.v1",
        "pipeline_id": pipeline_id,
        "scanned_at": utc_iso(),
        "request": request,
        "raw_jobs": raw_jobs,
        "source_counts": source_counts(raw_jobs),
        "board_counts": {source: int(source_results.get(source, {}).get("jobs_count", 0) or 0) for source in ACTIVE_OPENCLAW_SOURCES},
        "source_results": source_results,
        "supported_fields_by_source": supported_fields_by_source,
        "warnings": warnings,
        "collector_errors": collector_errors,
        "collection_status": collection_status,
        "partial_success": collection_status == "partial_success",
        "successful_sources": successful_sources,
        "healthy_sources": healthy_sources,
        "empty_sources": empty_sources,
        "under_target_sources": under_target_sources,
        "failed_sources": failed_sources,
        "skipped_sources": skipped_sources,
        "collection_counts": {
            "raw_job_count": len(raw_jobs),
            "unique_job_count": minimum_state["current_unique_jobs_total"],
            "discovered_raw_count": discovered_raw_count,
            "kept_after_basic_filter_count": kept_after_basic_filter_count,
            "dropped_by_basic_filter_count": dropped_by_basic_filter_count,
            "deduped_count": deduped_count,
            "queries_executed_count": queries_executed_count,
            "empty_queries_count": empty_queries_count,
            "minimum_raw_jobs_total_requested": minimum_state["minimum_raw_jobs_total_requested"],
            "minimum_unique_jobs_total_requested": minimum_state["minimum_unique_jobs_total_requested"],
            "minimum_jobs_per_source_requested": minimum_state["minimum_jobs_per_source_requested"],
            "minimum_reached": minimum_state["minimum_reached"],
            "reason_stopped": reason_stopped,
        },
        "source_metadata_quality": source_metadata_quality,
        "metadata_completeness_summary": metadata_completeness_summary,
        "collection_observability": collection_observability,
        "collection_summary": {
            "requested_sources": sources,
            "collection_method": "openclaw",
            "openclaw_enabled": bool(request.get("openclaw_enabled")),
            "successful_source_count": len(successful_sources),
            "healthy_source_count": len(healthy_sources),
            "empty_source_count": len(empty_sources),
            "under_target_source_count": len(under_target_sources),
            "failed_source_count": len(failed_sources),
            "skipped_source_count": len(skipped_sources),
            "raw_job_count": len(raw_jobs),
            "discovered_raw_count": discovered_raw_count,
            "kept_after_basic_filter_count": kept_after_basic_filter_count,
            "dropped_by_basic_filter_count": dropped_by_basic_filter_count,
            "deduped_count": deduped_count,
            "pages_fetched": pages_fetched,
            "queries_attempted": queries_attempted,
            "queries_executed_count": queries_executed_count,
            "empty_queries_count": empty_queries_count,
            "query_examples": query_examples[:10],
            "max_total_jobs": max_total_jobs,
            "minimum_raw_jobs_total_requested": minimum_state["minimum_raw_jobs_total_requested"],
            "minimum_unique_jobs_total_requested": minimum_state["minimum_unique_jobs_total_requested"],
            "minimum_jobs_per_source_requested": minimum_state["minimum_jobs_per_source_requested"],
            "stop_when_minimum_reached": minimum_state["stop_when_minimum_reached"],
            "minimum_reached": minimum_state["minimum_reached"],
            "reason_stopped": reason_stopped,
            "minimum_shortfall_summary": minimum_state["shortfall_summary"] or None,
            "screenshot_count": len(screenshot_references),
        },
        "source_health_summary_artifact": source_health_summary_artifact,
        "screenshot_references": screenshot_references[:50],
        "lineage": payload.get("lineage") if isinstance(payload.get("lineage"), dict) else {},
    }

    debug_artifact = {
        "artifact_type": "debug.json",
        "pipeline_id": pipeline_id,
        "collection_method": "openclaw",
        "sources_attempted": sources,
        "sources_succeeded": successful_sources,
        "sources_healthy": healthy_sources,
        "sources_empty": empty_sources,
        "sources_under_target": under_target_sources,
        "sources_failed": failed_sources,
        "sources_skipped": skipped_sources,
        "per_source_job_counts": {key: int(row.get("jobs_count", 0) or 0) for key, row in source_results.items()},
        "minimum_raw_jobs_total_requested": minimum_state["minimum_raw_jobs_total_requested"],
        "minimum_unique_jobs_total_requested": minimum_state["minimum_unique_jobs_total_requested"],
        "minimum_jobs_per_source_requested": minimum_state["minimum_jobs_per_source_requested"],
        "minimum_reached": minimum_state["minimum_reached"],
        "minimum_shortfall_summary": minimum_state["shortfall_summary"] or None,
        "reason_stopped": reason_stopped,
        "screenshot_references": screenshot_references[:50],
        "per_source_status": {
            key: {
                "source": key,
                "status": str(row.get("status") or "").strip() or "unknown",
                "error": row.get("error"),
                "jobs_count": int(row.get("jobs_count", 0) or 0),
                "source_status": str(((row.get("meta") if isinstance(row.get("meta"), dict) else {}) or {}).get("source_status") or row.get("status") or "").strip() or None,
                "source_error_type": str(((row.get("meta") if isinstance(row.get("meta"), dict) else {}) or {}).get("source_error_type") or "").strip() or None,
                "collection_method": str(((row.get("meta") if isinstance(row.get("meta"), dict) else {}) or {}).get("collection_method") or "").strip() or None,
                "collection_stop_reason": str(((row.get("meta") if isinstance(row.get("meta"), dict) else {}) or {}).get("collection_stop_reason") or "").strip() or None,
                "pages_attempted": _meta_count((row.get("meta") if isinstance(row.get("meta"), dict) else {}) or {}, "pages_attempted", 0),
                "cards_seen": _meta_count((row.get("meta") if isinstance(row.get("meta"), dict) else {}) or {}, "cards_seen", 0),
                "jobs_raw": _meta_count((row.get("meta") if isinstance(row.get("meta"), dict) else {}) or {}, "jobs_raw", int(row.get("jobs_count", 0) or 0)),
                "jobs_kept": _meta_count((row.get("meta") if isinstance(row.get("meta"), dict) else {}) or {}, "jobs_kept", int(row.get("jobs_count", 0) or 0)),
                "auth_required_detected": bool(((row.get("meta") if isinstance(row.get("meta"), dict) else {}) or {}).get("auth_required_detected", False)),
                "login_wall_detected": bool(((row.get("meta") if isinstance(row.get("meta"), dict) else {}) or {}).get("login_wall_detected", False)),
                "anti_bot_detected": bool(((row.get("meta") if isinstance(row.get("meta"), dict) else {}) or {}).get("anti_bot_detected", False)),
                "layout_mismatch_detected": bool(((row.get("meta") if isinstance(row.get("meta"), dict) else {}) or {}).get("layout_mismatch_detected", False)),
                "screenshot_count": _meta_count((row.get("meta") if isinstance(row.get("meta"), dict) else {}) or {}, "screenshot_count", 0),
                "screenshot_references": [
                    item
                    for item in (
                        ((row.get("meta") if isinstance(row.get("meta"), dict) else {}) or {}).get("screenshot_references")
                        if isinstance(((row.get("meta") if isinstance(row.get("meta"), dict) else {}) or {}).get("screenshot_references"), list)
                        else []
                    )
                    if isinstance(item, dict)
                ][:10],
            }
            for key, row in source_results.items()
        },
        "warnings_count": len(warnings),
        "collector_error_count": len(collector_errors),
        "partial_success": artifact["partial_success"],
    }

    upstream = build_upstream_ref(task, "openclaw_jobs_collect_v1")
    upstream_run_id = upstream.get("run_id") or str(getattr(task, "id", ""))
    next_payload = {
        "pipeline_id": pipeline_id,
        "upstream": upstream,
        "request": request,
        "normalization_policy": {
            "dedupe_keys": ["source", "url", "title"],
            "canonicalization_version": "1.0",
        },
    }

    return {
        "artifact_type": "jobs.collect.v1",
        "content_text": (
            f"openclaw_jobs_collect_v1 collected {len(raw_jobs)} jobs across {len(successful_sources)} usable OpenClaw sources"
            f" and {len(failed_sources)} blocked or failed sources."
        ),
        "content_json": artifact,
        "debug_json": debug_artifact,
        "next_tasks": [
            {
                "task_type": "jobs_normalize_v1",
                "payload_json": next_payload,
                "idempotency_key": stage_idempotency_key(pipeline_id, "jobs_normalize_v1", upstream_run_id),
                "max_attempts": 3,
            }
        ],
    }
