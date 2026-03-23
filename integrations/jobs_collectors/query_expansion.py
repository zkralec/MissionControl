from __future__ import annotations

import re
from typing import Any

REMOTE_SYNONYMS = ("remote", "work from home", "distributed")
DEFAULT_MAX_QUERIES_PER_RUN = 12
MAX_MAX_QUERIES_PER_RUN = 20
DEFAULT_CONSECUTIVE_EMPTY_QUERIES_STOP = 3

_WS_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]+")
_REMOTE_RE = re.compile(r"\b(remote|work from home|wfh|distributed|anywhere)\b", re.IGNORECASE)
_TITLE_SYNONYM_MAP: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (
        re.compile(r"\bsoftware engineer\b", re.IGNORECASE),
        ("Backend Software Engineer", "Backend Engineer", "SWE", "Software Developer"),
    ),
    (
        re.compile(r"\bmachine learning engineer\b", re.IGNORECASE),
        ("ML Engineer", "Applied ML Engineer", "AI Engineer"),
    ),
    (
        re.compile(r"\bdata engineer\b", re.IGNORECASE),
        ("Analytics Engineer", "Platform Data Engineer"),
    ),
)
_ROLE_KEYWORD_PREFIXES = {
    "backend": "Backend",
    "frontend": "Frontend",
    "full stack": "Full Stack",
    "full-stack": "Full Stack",
    "platform": "Platform",
    "api": "API",
    "distributed": "Distributed Systems",
}
_SENIORITY_TEMPLATES = {
    "entry": ("Junior {title}", "{title} I", "Entry Level {title}", "Associate {title}"),
    "mid": ("{title} II", "Intermediate {title}", "Mid Level {title}"),
    "senior": ("Senior {title}", "Lead {title}", "Staff {title}"),
}


def _compact(value: str) -> str:
    return _WS_RE.sub(" ", value.strip())


def _canonical(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = _NON_ALNUM_RE.sub(" ", text)
    return _compact(text)


def _dedupe(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _compact(value)
        key = normalized.lower()
        if not normalized or key in seen:
            continue
        seen.add(key)
        output.append(normalized)
    return output


def _is_remoteish(location: str) -> bool:
    return bool(_REMOTE_RE.search(location))


def _location_variants(location: str, *, work_mode_preferences: set[str]) -> list[str]:
    normalized = _compact(location)
    if _is_remoteish(normalized):
        return list(REMOTE_SYNONYMS)
    return [normalized]


def _title_synonyms(title: str) -> list[str]:
    output: list[str] = []
    normalized = _compact(title)
    for pattern, replacements in _TITLE_SYNONYM_MAP:
        if pattern.search(normalized):
            output.extend(replacements)
    return _dedupe(output)


def _keyword_title_variants(title: str, keywords: list[str]) -> list[str]:
    output: list[str] = []
    canonical_title = _canonical(title)
    for keyword in keywords:
        normalized = _canonical(keyword)
        if not normalized or normalized in canonical_title:
            continue
        prefix = _ROLE_KEYWORD_PREFIXES.get(normalized)
        if not prefix:
            continue
        output.append(f"{prefix} {title}")
    return _dedupe(output)


def _seniority_variants(title: str, experience_levels: list[str]) -> list[str]:
    output: list[str] = []
    canonical_title = _canonical(title)
    for experience in experience_levels:
        for template in _SENIORITY_TEMPLATES.get(experience, ()):
            candidate = template.format(title=title)
            if _canonical(candidate) == canonical_title:
                continue
            output.append(candidate)
    return _dedupe(output)


def _title_variants(
    *,
    title_seed: str,
    keywords: list[str],
    experience_levels: list[str],
    max_queries_per_title_location_pair: int,
    enable_query_expansion: bool,
) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = [{"title_variant": _compact(title_seed), "expansion_type": "base_title"}]
    if enable_query_expansion:
        buckets: list[tuple[str, list[str]]] = [
            ("seniority", _seniority_variants(title_seed, experience_levels)),
            ("title_synonym", _title_synonyms(title_seed)),
            ("role_specialization", _keyword_title_variants(title_seed, keywords)),
        ]
        while any(values for _label, values in buckets):
            for label, values in buckets:
                if not values:
                    continue
                candidates.append({"title_variant": values.pop(0), "expansion_type": label})

    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in candidates:
        value = _compact(row.get("title_variant") or "")
        key = value.lower()
        if not value or key in seen:
            continue
        seen.add(key)
        deduped.append({"title_variant": value, "expansion_type": str(row.get("expansion_type") or "base_title")})
        if len(deduped) >= max_queries_per_title_location_pair:
            break
    return deduped


def build_query_plan(
    *,
    explicit_query: str,
    title_seeds: list[str],
    locations: list[str],
    keywords: list[str],
    experience_levels: list[str],
    work_mode_preferences: set[str],
    max_queries_per_run: int,
    max_queries_per_title_location_pair: int,
    enable_query_expansion: bool,
) -> list[dict[str, str]]:
    max_queries = max(1, min(int(max_queries_per_run), MAX_MAX_QUERIES_PER_RUN))
    normalized_titles = _dedupe(title_seeds)
    normalized_locations = _dedupe(locations)
    if "remote" in work_mode_preferences and not any(_is_remoteish(location) for location in normalized_locations):
        normalized_locations.append("Remote")

    queries: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(query: str, *, location: str, title_seed: str, expansion_type: str) -> None:
        normalized_query = _compact(query)
        key = normalized_query.lower()
        if not normalized_query or key in seen or len(queries) >= max_queries:
            return
        seen.add(key)
        queries.append(
            {
                "query": normalized_query,
                "location": _compact(location),
                "title_seed": _compact(title_seed),
                "expansion_type": expansion_type,
            }
        )

    explicit = _compact(explicit_query)
    location_variants_by_location = {
        location: _location_variants(location, work_mode_preferences=work_mode_preferences) for location in normalized_locations
    }

    if explicit:
        for location in normalized_locations:
            location_variants = location_variants_by_location.get(location) or [location]
            primary_location_variant = location_variants[0]
            candidate = (
                explicit if _is_remoteish(primary_location_variant) and _is_remoteish(explicit) else f"{explicit} {primary_location_variant}"
            )
            add(
                candidate,
                location=location,
                title_seed=explicit or (normalized_titles[0] if normalized_titles else explicit),
                expansion_type="explicit_query",
            )

    title_variants_by_seed = {
        title_seed: _title_variants(
            title_seed=title_seed,
            keywords=keywords,
            experience_levels=experience_levels,
            max_queries_per_title_location_pair=max_queries_per_title_location_pair,
            enable_query_expansion=enable_query_expansion,
        )
        for title_seed in normalized_titles
    }

    for location in normalized_locations:
        location_variants = location_variants_by_location.get(location) or [location]
        primary_location_variant = location_variants[0]
        for title_seed in normalized_titles:
            if len(queries) >= max_queries:
                break
            for row in title_variants_by_seed.get(title_seed, []):
                if len(queries) >= max_queries:
                    break
                title_variant = row["title_variant"]
                candidate = (
                    title_variant
                    if _is_remoteish(primary_location_variant) and _is_remoteish(title_variant)
                    else f"{title_variant} {primary_location_variant}"
                )
                add(
                    candidate,
                    location=location,
                    title_seed=title_seed,
                    expansion_type=str(row.get("expansion_type") or "base_title"),
                )

    if enable_query_expansion:
        for location in normalized_locations:
            location_variants = location_variants_by_location.get(location) or [location]
            secondary_location_variants = location_variants[1:]
            if not secondary_location_variants:
                continue
            for title_seed in normalized_titles:
                if len(queries) >= max_queries:
                    break
                title_rows = title_variants_by_seed.get(title_seed, [])
                if not title_rows:
                    continue
                primary_title_variant = title_rows[0]["title_variant"]
                primary_expansion_type = str(title_rows[0].get("expansion_type") or "base_title")
                for location_variant in secondary_location_variants:
                    if len(queries) >= max_queries:
                        break
                    candidate = (
                        primary_title_variant
                        if _is_remoteish(location_variant) and _is_remoteish(primary_title_variant)
                        else f"{primary_title_variant} {location_variant}"
                    )
                    add(
                        candidate,
                        location=location,
                        title_seed=title_seed,
                        expansion_type=f"{primary_expansion_type}_location_synonym",
                    )

    return queries[:max_queries]
