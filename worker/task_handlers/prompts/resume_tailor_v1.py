from __future__ import annotations

import json
from typing import Any

RESUME_TAILOR_PROMPT_VERSION = "resume-tailor-v1-structured"

RESUME_TAILOR_OUTPUT_SCHEMA = {
    "type": "object",
    "required": [
        "resume_variant_name",
        "resume_variant_text",
        "resume_strategy_summary",
        "requirements_alignment",
        "application_answers",
        "operator_notes",
    ],
    "properties": {
        "resume_variant_name": {"type": "string"},
        "resume_variant_text": {"type": "string"},
        "resume_strategy_summary": {"type": "string"},
        "requirements_alignment": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["requirement", "coverage", "evidence"],
                "properties": {
                    "requirement": {"type": "string"},
                    "coverage": {"type": "string", "enum": ["strong", "partial", "gap"]},
                    "evidence": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        "application_answers": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["question", "answer", "answer_type"],
                "properties": {
                    "question": {"type": "string"},
                    "answer": {"type": "string"},
                    "answer_type": {"type": "string", "enum": ["motivation", "experience", "impact", "availability", "custom"]},
                },
                "additionalProperties": False,
            },
        },
        "cover_letter_text": {"type": "string"},
        "operator_notes": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "additionalProperties": False,
}


def _trim(value: Any, max_chars: int) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def build_resume_tailor_messages(
    *,
    candidate_profile: dict[str, Any],
    application_target: dict[str, Any],
    extracted_requirements: list[dict[str, Any]],
    common_questions: list[dict[str, Any]],
    include_cover_letter: bool,
    prompt_version: str = RESUME_TAILOR_PROMPT_VERSION,
) -> list[dict[str, str]]:
    output_template = {
        "resume_variant_name": "Tailored Resume - Example",
        "resume_variant_text": "Tailored resume text here",
        "resume_strategy_summary": "Why this tailored version fits the role",
        "requirements_alignment": [
            {
                "requirement": "Experience with production ML systems",
                "coverage": "strong",
                "evidence": "Directly reflected in the resume",
            }
        ],
        "application_answers": [
            {
                "question": "Why are you interested in this role?",
                "answer": "Short draft answer",
                "answer_type": "motivation",
            }
        ],
        "cover_letter_text": "Optional cover letter draft" if include_cover_letter else "",
        "operator_notes": ["Keep answers concise and truthful."],
    }

    user_payload = {
        "prompt_version": prompt_version,
        "task": (
            "Create a tailored resume draft and application-answer drafts for a shortlisted job. "
            "Ground everything in the candidate profile. Do not invent employers, dates, degrees, or projects."
        ),
        "candidate_profile": {
            "resume_name": candidate_profile.get("resume_name"),
            "resume_sha256": candidate_profile.get("resume_sha256"),
            "resume_text": _trim(candidate_profile.get("resume_text"), 14000),
            "metadata_json": candidate_profile.get("metadata_json"),
        },
        "application_target": application_target,
        "extracted_requirements": extracted_requirements,
        "common_questions": common_questions,
        "include_cover_letter": include_cover_letter,
        "output_contract": RESUME_TAILOR_OUTPUT_SCHEMA,
        "output_template": output_template,
        "rules": [
            "Return strict JSON only. No markdown fences, no prose outside JSON.",
            "Use only experience grounded in the provided candidate profile.",
            "Resume variant text must stay realistic and professional.",
            "Application answers should be concise, truthful draft answers suitable for human review.",
            "If the evidence is weak, mark coverage='gap' and explain the gap instead of fabricating fit.",
            "If include_cover_letter is false, set cover_letter_text to an empty string.",
        ],
    }

    system_prompt = (
        "You are a careful career materials editor. "
        "You tailor resumes and draft job application answers while preserving factual accuracy. "
        "You never fabricate credentials or claim unverified experience."
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=True)},
    ]
