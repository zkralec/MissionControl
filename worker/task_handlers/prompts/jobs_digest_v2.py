from __future__ import annotations

import json
from typing import Any

DIGEST_PROMPT_VERSION = "jobs-digest-v3-structured"

DIGEST_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["executive_summary", "jobs", "notification_excerpt"],
    "properties": {
        "executive_summary": {
            "type": "object",
            "required": ["summary_text", "strongest_patterns", "best_fit_roles"],
            "properties": {
                "collected_count": {"type": ["integer", "null"], "minimum": 0},
                "deduped_count": {"type": ["integer", "null"], "minimum": 0},
                "shortlisted_count": {"type": ["integer", "null"], "minimum": 0},
                "summary_text": {"type": "string"},
                "strongest_patterns": {"type": "array", "items": {"type": "string"}},
                "best_fit_roles": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
        "jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "job_id",
                    "rank",
                    "title",
                    "company",
                    "location",
                    "salary",
                    "source",
                    "source_url",
                    "why_it_fits",
                    "tradeoffs",
                ],
                "properties": {
                    "job_id": {"type": "string"},
                    "rank": {"type": "integer", "minimum": 1},
                    "title": {"type": "string"},
                    "company": {"type": "string"},
                    "location": {"type": "string"},
                    "salary": {"type": "string"},
                    "source": {"type": "string"},
                    "source_url": {"type": "string"},
                    "why_it_fits": {"type": "string"},
                    "tradeoffs": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        "notification_excerpt": {"type": "string"},
    },
    "additionalProperties": False,
}


def _trim(value: Any, max_chars: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.strip().split())
    if not text:
        return None
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def build_digest_messages(
    *,
    top_jobs: list[dict[str, Any]],
    summary_context: dict[str, Any],
    prompt_version: str = DIGEST_PROMPT_VERSION,
    digest_format: str = "compact",
) -> list[dict[str, str]]:
    jobs_for_prompt: list[dict[str, Any]] = []
    for idx, row in enumerate(top_jobs, start=1):
        jobs_for_prompt.append(
            {
                "job_id": str(row.get("job_id") or ""),
                "rank": idx,
                "title": row.get("title"),
                "company": row.get("company"),
                "location": row.get("location"),
                "salary_text": row.get("salary_text"),
                "salary_min": row.get("salary_min"),
                "salary_max": row.get("salary_max"),
                "source": row.get("source"),
                "source_url": row.get("source_url") or row.get("url"),
                "score": row.get("score"),
                "overall_score": row.get("overall_score"),
                "fit_tier": row.get("fit_tier"),
                "explanation_summary": _trim(row.get("explanation_summary"), 180),
                "description_snippet": _trim(row.get("description_snippet"), 260),
            }
        )

    expected_job_ids = [str(row.get("job_id") or "") for row in jobs_for_prompt if str(row.get("job_id") or "").strip()]
    output_template = {
        "executive_summary": {
            "summary_text": "short summary",
            "strongest_patterns": ["pattern"],
            "best_fit_roles": ["role"],
        },
        "jobs": [
            {
                "job_id": expected_job_ids[0] if expected_job_ids else "job_id",
                "rank": 1,
                "title": "title",
                "company": "company",
                "location": "location",
                "salary": "Not listed",
                "source": "source",
                "source_url": "",
                "why_it_fits": "short reason",
                "tradeoffs": "short tradeoff",
            }
        ],
        "notification_excerpt": "short notification text",
    }

    user_payload = {
        "prompt_version": prompt_version,
        "task": (
            "Generate a concise and actionable jobs digest for UI preview and notifications. "
            "Use concrete tradeoffs for each job."
        ),
        "digest_format": digest_format,
        "summary_context": summary_context,
        "jobs": jobs_for_prompt,
        "expected_job_ids": expected_job_ids,
        "output_contract": DIGEST_OUTPUT_SCHEMA,
        "output_template": output_template,
        "rules": [
            "Return strict JSON only. No markdown fences, no prose, no trailing text.",
            "Include executive_summary.summary_text, strongest_patterns, and best_fit_roles.",
            "Include every job_id exactly once in jobs.",
            "Use only job_id values from expected_job_ids.",
            "jobs length must equal the number of input jobs.",
            "Keep why_it_fits and tradeoffs specific and under 180 characters each.",
            "Keep notification_excerpt under 500 characters.",
            "Do not fabricate salary values; use 'Not listed' when absent.",
        ],
    }

    system_prompt = (
        "You are a precise career advisor writing high-signal job digests. "
        "Your output must exactly follow the provided JSON schema."
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_payload, separators=(",", ":"), ensure_ascii=True)},
    ]
