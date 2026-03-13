from __future__ import annotations

from typing import Any

from integrations.job_boards_scrape import collect_jobs_from_board

DEFAULT_QUERY = "software engineer"
DEFAULT_LOCATION = "United States"
MAX_RESULT_LIMIT_PER_SOURCE = 100
MIN_RESULT_LIMIT_PER_SOURCE = 1


def _as_text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            output.append(item.strip())
    return output


def _dedupe_text_list(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        low = value.strip().lower()
        if not low or low in seen:
            continue
        seen.add(low)
        output.append(value.strip())
    return output


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip().replace(",", "")
        if stripped.startswith("$"):
            stripped = stripped[1:]
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


def _normalize_query(request: dict[str, Any]) -> str:
    query = str(request.get("query") or request.get("search_query") or "").strip()
    titles = _as_text_list(request.get("titles"))
    keywords = _as_text_list(request.get("keywords"))

    if query:
        return query

    parts: list[str] = []
    if titles:
        parts.append(titles[0])
    if keywords:
        parts.extend(keywords[:3])
    query = " ".join(parts).strip()
    return query or DEFAULT_QUERY


def _normalize_locations(request: dict[str, Any]) -> list[str]:
    locations = _dedupe_text_list(_as_text_list(request.get("locations")))
    if locations:
        return locations

    location = str(request.get("location") or request.get("search_location") or "").strip()
    if location:
        return [location]
    return [DEFAULT_LOCATION]


def _normalize_result_limit(request: dict[str, Any]) -> int:
    try:
        max_jobs = int(
            request.get("result_limit_per_source")
            or request.get("max_jobs_per_source")
            or request.get("max_jobs_per_board")
            or 25
        )
    except (TypeError, ValueError):
        max_jobs = 25
    return max(MIN_RESULT_LIMIT_PER_SOURCE, min(max_jobs, MAX_RESULT_LIMIT_PER_SOURCE))


def _normalize_work_mode_preferences(request: dict[str, Any]) -> set[str]:
    values = _as_text_list(request.get("work_mode_preference"))
    if not values:
        single = str(request.get("work_mode_preference") or "").strip()
        if single:
            values = [single]
    if not values:
        values = _as_text_list(request.get("work_modes"))

    normalized: set[str] = set()
    for row in values:
        low = row.lower().strip()
        if low in {"remote", "hybrid", "onsite", "on-site"}:
            normalized.add("onsite" if low == "on-site" else low)
    return normalized


def _normalize_experience_preferences(request: dict[str, Any]) -> set[str]:
    values = _as_text_list(request.get("experience_levels"))
    single = str(request.get("experience_level") or "").strip()
    if single:
        values.append(single)

    normalized: set[str] = set()
    for row in values:
        low = row.lower().strip()
        if low in {"intern", "internship", "co-op", "coop"}:
            normalized.add("internship")
        elif low in {"entry", "entry-level", "junior", "new grad", "associate"}:
            normalized.add("entry")
        elif low in {"mid", "mid-level", "intermediate"}:
            normalized.add("mid")
        elif low in {"senior", "lead", "staff", "principal", "manager", "director"}:
            normalized.add("senior")
        elif low:
            normalized.add(low)
    return normalized


def _job_matches_filters(job: dict[str, Any], request: dict[str, Any]) -> bool:
    title = str(job.get("title") or "")
    description = str(job.get("description_snippet") or "")
    location = str(job.get("location") or "")
    haystack = f"{title} {description}".lower()

    include_terms = _as_text_list(request.get("titles")) + _as_text_list(request.get("keywords"))
    if include_terms and not any(term.lower() in haystack for term in include_terms):
        return False

    exclude_terms = _as_text_list(request.get("excluded_keywords")) + _as_text_list(request.get("excluded_title_keywords"))
    if exclude_terms and any(term.lower() in haystack for term in exclude_terms):
        return False

    preferred_locations = _normalize_locations(request)
    if preferred_locations:
        location_haystack = location.lower()
        if location_haystack and not any(term.lower() in location_haystack for term in preferred_locations):
            return False

    work_mode_prefs = _normalize_work_mode_preferences(request)
    if work_mode_prefs:
        work_mode = str(job.get("work_mode") or "").strip().lower()
        if work_mode == "on-site":
            work_mode = "onsite"
        if work_mode and work_mode not in work_mode_prefs:
            return False

    minimum_salary = _as_float(request.get("minimum_salary"))
    if minimum_salary is None:
        minimum_salary = _as_float(request.get("desired_salary_min"))
    if minimum_salary is not None:
        salary_max = _as_float(job.get("salary_max"))
        salary_min = _as_float(job.get("salary_min"))
        salary_anchor = salary_max if salary_max is not None else salary_min
        if salary_anchor is not None and salary_anchor < minimum_salary:
            return False

    exp_prefs = _normalize_experience_preferences(request)
    if exp_prefs:
        experience_level = str(job.get("experience_level") or "").strip().lower()
        if experience_level and experience_level not in exp_prefs:
            return False

    return True


def _dedupe_jobs(jobs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    duplicates = 0
    for row in jobs:
        source = str(row.get("source") or "").strip().lower()
        url = str(row.get("url") or "").strip()
        title = str(row.get("title") or "").strip().lower()
        key = (source, url, title)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        output.append(row)
    return output, duplicates


def _normalize_job(board: str, row: dict[str, Any], *, url_override: str | None) -> dict[str, Any]:
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    source_url = str(raw.get("search_url") or url_override or "").strip() or None
    return {
        "source": board,
        "source_url": source_url,
        "title": row.get("title"),
        "company": row.get("company"),
        "location": row.get("location"),
        "url": row.get("url"),
        "salary_min": row.get("salary_min"),
        "salary_max": row.get("salary_max"),
        "salary_currency": row.get("salary_currency"),
        "experience_level": row.get("experience_level"),
        "work_mode": row.get("work_mode"),
        "posted_at": row.get("posted_at"),
        "scraped_at": row.get("scraped_at"),
        "description_snippet": row.get("description_snippet"),
        "source_metadata": raw,
    }


def _split_warnings_and_errors(board: str, messages: list[str]) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    errors: list[str] = []
    for message in messages:
        text = str(message).strip()
        if not text:
            continue
        low = text.lower()
        prefixed = text if low.startswith(f"{board}:") else f"{board}: {text}"
        if "fetch_failed" in low or "unsupported_board" in low:
            errors.append(prefixed)
        else:
            warnings.append(prefixed)
    return warnings, errors


def supported_fields(board: str | None = None) -> dict[str, Any]:
    source = (board or "").strip().lower() or "generic"
    return {
        "source": source,
        "titles": True,
        "keywords": True,
        "excluded_keywords": True,
        "locations": True,
        "work_mode_preference": True,
        "minimum_salary": True,
        "experience_level": True,
        "result_limit_per_source": True,
        "enabled_sources": True,
        "input_mode": {
            "titles": "query_plus_post_filter",
            "keywords": "query_plus_post_filter",
            "excluded_keywords": "post_filter",
            "locations": "multi_location_search_plus_post_filter",
            "work_mode_preference": "post_filter",
            "minimum_salary": "post_filter",
            "experience_level": "post_filter",
            "result_limit_per_source": "collector_limit",
            "enabled_sources": "pipeline_routing",
        },
        "source_metadata_fields": ["search_url"],
    }


def collect_board_jobs(board: str, request: dict[str, Any], *, url_override: str | None = None) -> dict[str, Any]:
    query = _normalize_query(request)
    locations = _normalize_locations(request)
    max_jobs = _normalize_result_limit(request)

    filtered: list[dict[str, Any]] = []
    warnings: list[str] = []
    errors: list[str] = []
    locations_attempted: list[str] = []
    failed_locations: list[str] = []
    raw_count = 0
    filtered_out = 0

    for location in locations:
        if len(filtered) >= max_jobs:
            break
        remaining = max_jobs - len(filtered)
        locations_attempted.append(location)

        jobs, board_messages = collect_jobs_from_board(
            board,
            query=query,
            location=location,
            max_jobs=remaining,
            url_override=url_override,
        )
        raw_count += len(jobs)

        board_warnings, board_errors = _split_warnings_and_errors(board, board_messages)
        warnings.extend(board_warnings)
        errors.extend(board_errors)
        if board_errors:
            failed_locations.append(location)

        for row in jobs:
            if not isinstance(row, dict):
                continue
            if not _job_matches_filters(row, request):
                filtered_out += 1
                continue
            filtered.append(_normalize_job(board, row, url_override=url_override))
            if len(filtered) >= max_jobs:
                break

    deduped, duplicate_count = _dedupe_jobs(filtered)
    status = "success"
    if errors and deduped:
        status = "partial_success"
    elif errors and not deduped:
        status = "failed"

    return {
        "status": status,
        "jobs": deduped,
        "warnings": warnings,
        "errors": errors,
        "meta": {
            "query": query,
            "locations": locations,
            "locations_attempted": locations_attempted,
            "failed_locations": failed_locations,
            "requested_limit": max_jobs,
            "raw_count": raw_count,
            "filtered_out": filtered_out,
            "duplicate_count": duplicate_count,
            "returned_count": len(deduped),
        },
    }
