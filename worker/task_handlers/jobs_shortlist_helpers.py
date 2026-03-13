from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any


def _canonical_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in text)
    return " ".join(text.split())


def _token_set(value: str) -> set[str]:
    return {token for token in _canonical_text(value).split() if token}


def _title_similarity(left: str, right: str) -> float:
    left_tokens = _token_set(left)
    right_tokens = _token_set(right)
    if not left_tokens or not right_tokens:
        return 0.0
    inter = left_tokens.intersection(right_tokens)
    union = left_tokens.union(right_tokens)
    if not union:
        return 0.0
    return len(inter) / float(len(union))


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if text.startswith("$"):
            text = text[1:]
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _freshness_factor(posted_at: Any, *, now_utc: datetime | None = None) -> float:
    posted = _parse_datetime(posted_at)
    if posted is None:
        return 0.35
    now = now_utc or datetime.now(timezone.utc)
    delta_days = max((now - posted).total_seconds() / 86400.0, 0.0)
    # Fast early decay, then gradual flattening.
    return max(0.0, min(math.exp(-delta_days / 21.0), 1.0))


def _score_100(row: dict[str, Any]) -> float:
    direct = _as_float(row.get("overall_score"))
    if direct is not None:
        return max(0.0, min(direct, 100.0))

    scaled = _as_float(row.get("score"))
    if scaled is not None:
        if scaled <= 2.5:
            return max(0.0, min(scaled * 50.0, 100.0))
        return max(0.0, min(scaled, 100.0))
    return 0.0


def normalize_scored_jobs(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    output: list[dict[str, Any]] = []
    for idx, raw in enumerate(rows, start=1):
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        source = str(raw.get("source") or "unknown").strip().lower() or "unknown"
        company = str(raw.get("company") or "").strip()
        duplicate_group_id = str(raw.get("duplicate_group_id") or "").strip() or None
        duplicate_count = int(raw.get("duplicate_count") or 1)
        duplicate_count = max(1, duplicate_count)

        item = dict(raw)
        item["job_id"] = str(raw.get("job_id") or raw.get("normalized_job_id") or f"short-{idx:06d}").strip()
        item["source"] = source
        item["company"] = company
        item["title"] = title
        item["duplicate_group_id"] = duplicate_group_id
        item["duplicate_count"] = duplicate_count
        item["_base_score_100"] = _score_100(raw)
        item["_company_key"] = _canonical_text(company) or "_unknown_company"
        item["_title_key"] = _canonical_text(title)
        item["_source_key"] = source
        item["_freshness_factor"] = _freshness_factor(raw.get("posted_at"))
        output.append(item)
    return output


def resolve_min_score_100(raw_min_score: Any) -> float:
    parsed = _as_float(raw_min_score)
    if parsed is None:
        return 37.5  # legacy 0.75 on 0..2 scale
    if parsed <= 2.5:
        return max(0.0, min(parsed * 50.0, 100.0))
    return max(0.0, min(parsed, 100.0))


def shortlist_jobs(
    scored_jobs: list[dict[str, Any]],
    *,
    max_items: int,
    min_score_100: float,
    per_source_cap: int,
    per_company_cap: int,
    source_diversity_weight: float,
    company_repetition_penalty: float,
    near_duplicate_title_similarity_threshold: float,
    freshness_weight_enabled: bool,
    freshness_max_bonus: float,
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, Any]]:
    remaining = scored_jobs[:]
    shortlist: list[dict[str, Any]] = []
    source_counts: dict[str, int] = {}
    company_counts: dict[str, int] = {}
    selected_duplicate_groups: set[str] = set()

    rejected_summary = {
        "below_min_score": 0,
        "per_source_cap": 0,
        "per_company_cap": 0,
        "duplicate_group_repeat": 0,
        "near_duplicate_company_title": 0,
        "max_items": 0,
    }

    diagnostics: dict[str, Any] = {
        "iterations": 0,
        "picked_job_ids": [],
        "weights": {
            "source_diversity_weight": source_diversity_weight,
            "company_repetition_penalty": company_repetition_penalty,
            "freshness_weight_enabled": freshness_weight_enabled,
            "freshness_max_bonus": freshness_max_bonus,
        },
    }

    while remaining and len(shortlist) < max_items:
        diagnostics["iterations"] += 1
        best_idx = -1
        best_effective = float("-inf")
        best_row: dict[str, Any] | None = None

        for idx, row in enumerate(remaining):
            base = float(row.get("_base_score_100") or 0.0)
            source_key = str(row.get("_source_key") or "unknown")
            company_key = str(row.get("_company_key") or "_unknown_company")
            duplicate_group_id = row.get("duplicate_group_id")

            if base < min_score_100:
                continue
            if source_counts.get(source_key, 0) >= per_source_cap:
                continue
            if company_counts.get(company_key, 0) >= per_company_cap:
                continue
            if isinstance(duplicate_group_id, str) and duplicate_group_id and duplicate_group_id in selected_duplicate_groups:
                continue

            source_penalty = float(source_counts.get(source_key, 0)) * source_diversity_weight
            company_penalty = float(company_counts.get(company_key, 0)) * company_repetition_penalty
            freshness_bonus = float(row.get("_freshness_factor") or 0.0) * freshness_max_bonus if freshness_weight_enabled else 0.0

            near_duplicate_penalty = 0.0
            title_key = str(row.get("_title_key") or "")
            for picked in shortlist:
                if str(picked.get("_company_key") or "") != company_key:
                    continue
                similarity = _title_similarity(title_key, str(picked.get("_title_key") or ""))
                if similarity >= near_duplicate_title_similarity_threshold:
                    near_duplicate_penalty = max(near_duplicate_penalty, 10.0 + similarity * 10.0)

            effective = base + freshness_bonus - source_penalty - company_penalty - near_duplicate_penalty
            if effective > best_effective:
                best_effective = effective
                best_idx = idx
                best_row = row

        if best_idx < 0 or best_row is None:
            break

        picked = remaining.pop(best_idx)
        source_key = str(picked.get("_source_key") or "unknown")
        company_key = str(picked.get("_company_key") or "_unknown_company")
        source_counts[source_key] = source_counts.get(source_key, 0) + 1
        company_counts[company_key] = company_counts.get(company_key, 0) + 1
        duplicate_group_id = picked.get("duplicate_group_id")
        if isinstance(duplicate_group_id, str) and duplicate_group_id:
            selected_duplicate_groups.add(duplicate_group_id)

        picked["_shortlist_effective_score"] = round(best_effective, 4)
        shortlist.append(picked)
        diagnostics["picked_job_ids"].append(picked.get("job_id"))

    for row in remaining:
        base = float(row.get("_base_score_100") or 0.0)
        source_key = str(row.get("_source_key") or "unknown")
        company_key = str(row.get("_company_key") or "_unknown_company")
        duplicate_group_id = row.get("duplicate_group_id")
        title_key = str(row.get("_title_key") or "")

        if len(shortlist) >= max_items:
            rejected_summary["max_items"] += 1
            continue
        if base < min_score_100:
            rejected_summary["below_min_score"] += 1
            continue
        if source_counts.get(source_key, 0) >= per_source_cap:
            rejected_summary["per_source_cap"] += 1
            continue
        if company_counts.get(company_key, 0) >= per_company_cap:
            rejected_summary["per_company_cap"] += 1
            continue
        if isinstance(duplicate_group_id, str) and duplicate_group_id and duplicate_group_id in selected_duplicate_groups:
            rejected_summary["duplicate_group_repeat"] += 1
            continue

        near_duplicate = False
        for picked in shortlist:
            if str(picked.get("_company_key") or "") != company_key:
                continue
            similarity = _title_similarity(title_key, str(picked.get("_title_key") or ""))
            if similarity >= near_duplicate_title_similarity_threshold:
                near_duplicate = True
                break
        if near_duplicate:
            rejected_summary["near_duplicate_company_title"] += 1
            continue
        rejected_summary["max_items"] += 1

    return shortlist, rejected_summary, diagnostics
