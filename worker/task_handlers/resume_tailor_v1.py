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
    build_upstream_ref,
    expect_artifact_type,
    fetch_upstream_result_content_json,
    new_pipeline_id,
    payload_object,
    resolve_profile_context,
    stage_idempotency_key,
    utc_iso,
)
from task_handlers.prompts.resume_tailor_v1 import (
    RESUME_TAILOR_OUTPUT_SCHEMA,
    RESUME_TAILOR_PROMPT_VERSION,
    build_resume_tailor_messages,
)

_OUTPUT_VALIDATOR = Draft7Validator(RESUME_TAILOR_OUTPUT_SCHEMA)


def _llm_runtime_enabled() -> bool:
    return os.getenv("USE_LLM", "false").strip().lower() == "true"


def _extract_json_object(text: Any) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty_llm_output")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("llm_output_not_json") from exc
    if not isinstance(parsed, dict):
        raise ValueError("llm_output_must_be_object")
    errors = sorted(_OUTPUT_VALIDATOR.iter_errors(parsed), key=lambda err: list(err.path))
    if errors:
        raise ValueError("llm_output_schema_error")
    return parsed


def _fallback_artifacts(
    *,
    application_target: dict[str, Any],
    requirements: list[dict[str, Any]],
    common_questions: list[dict[str, Any]],
    profile_context: dict[str, Any],
    include_cover_letter: bool,
) -> dict[str, Any]:
    role = str(application_target.get("title") or "target role").strip() or "target role"
    company = str(application_target.get("company") or "target company").strip() or "target company"
    resume_text = str(profile_context.get("resume_text") or "").strip()
    top_requirements = [str(row.get("requirement") or "").strip() for row in requirements[:4] if str(row.get("requirement") or "").strip()]
    targeted_summary = (
        f"Tailored toward {role} at {company}. Focus areas: "
        + (", ".join(top_requirements) if top_requirements else "role alignment and impact.")
    )
    resume_variant_text = "\n\n".join(
        part for part in [targeted_summary, "Selected base resume profile:", resume_text] if part
    ).strip()
    application_answers = []
    for row in common_questions:
        question = str(row.get("question") or "").strip()
        if not question:
            continue
        answer_type = str(row.get("answer_type") or "custom").strip() or "custom"
        if answer_type == "motivation":
            answer = f"I’m interested in {role} at {company} because it aligns with my background and the work described in the job posting."
        elif answer_type == "experience":
            answer = f"My background includes relevant experience connected to {role}, and I would review the tailored resume draft before submission."
        else:
            answer = "I would refine this example with a concrete, truthful project from my resume before submitting."
        application_answers.append({"question": question, "answer": answer, "answer_type": answer_type})
    cover_letter_text = ""
    if include_cover_letter:
        cover_letter_text = (
            f"Dear Hiring Team,\n\n"
            f"I am excited to apply for the {role} role at {company}. "
            f"My background aligns with the role’s requirements, and I would tailor this letter further during review.\n\n"
            "Sincerely,\nCandidate"
        )
    return {
        "resume_variant_name": f"Tailored Resume - {company}",
        "resume_variant_text": resume_variant_text,
        "resume_strategy_summary": targeted_summary,
        "requirements_alignment": [
            {
                "requirement": str(row.get("requirement") or "").strip(),
                "coverage": "partial",
                "evidence": "Deterministic fallback used; review against the stored resume profile before submission.",
            }
            for row in requirements[:6]
            if str(row.get("requirement") or "").strip()
        ],
        "application_answers": application_answers,
        "cover_letter_text": cover_letter_text,
        "operator_notes": [
            "Fallback draft generated without live LLM output.",
            "Review every claim against the stored resume profile before final submission.",
        ],
    }


def execute(task: Any, db: Any) -> dict[str, Any]:
    payload = payload_object(task.payload_json)
    upstream = payload.get("upstream") if isinstance(payload.get("upstream"), dict) else {}
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    tailor_policy = payload.get("tailor_policy") if isinstance(payload.get("tailor_policy"), dict) else {}
    pipeline_id = new_pipeline_id(payload.get("pipeline_id"))

    upstream_result = fetch_upstream_result_content_json(db, upstream)
    expect_artifact_type(upstream_result, "job.apply.prepare.v1")

    application_target = upstream_result.get("application_target") if isinstance(upstream_result.get("application_target"), dict) else {}
    requirements = upstream_result.get("extracted_requirements") if isinstance(upstream_result.get("extracted_requirements"), list) else []
    common_questions = upstream_result.get("common_questions") if isinstance(upstream_result.get("common_questions"), list) else []

    request_with_profile = dict(request)
    request_with_profile["profile_mode"] = "resume_profile"
    profile_context = resolve_profile_context(request_with_profile)
    if not bool(profile_context.get("applied")) or not str(profile_context.get("resume_text") or "").strip():
        raise NonRetryableTaskError("resume_tailor_v1 requires a stored Mission Control resume profile")

    include_cover_letter = bool(tailor_policy.get("include_cover_letter", True))
    runtime_llm = _llm_runtime_enabled()
    model_id = str(getattr(task, "model", "") or "").strip() or tier_model("standard")
    generation_mode = "deterministic_fallback"
    warnings: list[str] = []
    llm_meta: dict[str, Any] = {
        "runtime_enabled": runtime_llm,
        "fallback_used": True,
        "prompt_version": RESUME_TAILOR_PROMPT_VERSION,
    }
    usage = {
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd": "0.00000000",
        "openai_request_ids": [],
        "ai_usage_task_run_ids": [],
    }

    output = _fallback_artifacts(
        application_target=application_target,
        requirements=[row for row in requirements if isinstance(row, dict)],
        common_questions=[row for row in common_questions if isinstance(row, dict)],
        profile_context=profile_context,
        include_cover_letter=include_cover_letter,
    )

    if runtime_llm:
        messages = build_resume_tailor_messages(
            candidate_profile=profile_context,
            application_target=application_target,
            extracted_requirements=[row for row in requirements if isinstance(row, dict)],
            common_questions=[row for row in common_questions if isinstance(row, dict)],
            include_cover_letter=include_cover_letter,
        )
        llm_task_run_id = f"{getattr(task, 'id', '')}:{getattr(task, '_run_id', '')}:resume_tailor_v1_1"
        try:
            llm_result = run_chat_completion(
                model=model_id,
                messages=messages,
                temperature=0.2,
                max_completion_tokens=2400,
                task_run_id=llm_task_run_id,
                agent_name="resume_tailor_v1",
            )
            usage = {
                "tokens_in": int(llm_result.get("tokens_in") or 0),
                "tokens_out": int(llm_result.get("tokens_out") or 0),
                "cost_usd": str(Decimal(str(llm_result.get("cost_usd") or "0")).quantize(Decimal("0.00000001"))),
                "openai_request_ids": [str(llm_result.get("openai_request_id")).strip()] if str(llm_result.get("openai_request_id") or "").strip() else [],
                "ai_usage_task_run_ids": [llm_task_run_id],
            }
            output = _extract_json_object(llm_result.get("output_text"))
            generation_mode = "llm_structured"
            llm_meta["fallback_used"] = False
        except Exception as exc:
            warnings.append(f"resume_tailor_llm_fallback:{type(exc).__name__}")
            llm_meta["fallback_used"] = True
            llm_meta["fallback_reason"] = f"{type(exc).__name__}: {exc}"
    else:
        warnings.append("resume_tailor_llm_disabled_at_runtime")

    resume_variant_artifact = {
        "artifact_type": "resume.variant.v1",
        "resume_variant_name": output.get("resume_variant_name"),
        "resume_variant_text": output.get("resume_variant_text"),
        "resume_file_name": f"{str(output.get('resume_variant_name') or 'tailored_resume').replace(' ', '_').lower()}.txt",
        "base_resume_name": profile_context.get("resume_name"),
        "base_resume_sha256": profile_context.get("resume_sha256"),
        "generated_at": utc_iso(),
    }
    application_answers_artifact = {
        "artifact_type": "application.answers.v1",
        "items": output.get("application_answers") if isinstance(output.get("application_answers"), list) else [],
        "generated_at": utc_iso(),
    }
    cover_letter_text = str(output.get("cover_letter_text") or "").strip()
    cover_letter_artifact = {
        "artifact_type": "cover_letter.draft.v1",
        "enabled": include_cover_letter,
        "text": cover_letter_text,
        "generated_at": utc_iso(),
    }

    artifact = {
        "artifact_type": "resume.tailor.v1",
        "artifact_schema": "resume.tailor.v1",
        "pipeline_id": pipeline_id,
        "generated_at": utc_iso(),
        "request": request,
        "tailor_policy": {
            "include_cover_letter": include_cover_letter,
            "enqueue_openclaw_apply": bool(tailor_policy.get("enqueue_openclaw_apply", True)),
        },
        "application_target": application_target,
        "candidate_profile": {
            "resume_source": profile_context.get("source"),
            "resume_name": profile_context.get("resume_name"),
            "resume_sha256": profile_context.get("resume_sha256"),
            "resume_char_count": profile_context.get("resume_char_count"),
        },
        "extracted_requirements": requirements,
        "requirements_alignment": output.get("requirements_alignment"),
        "resume_strategy_summary": output.get("resume_strategy_summary"),
        "resume_variant_artifact": resume_variant_artifact,
        "application_answers_artifact": application_answers_artifact,
        "cover_letter_artifact": cover_letter_artifact,
        "generation_mode": generation_mode,
        "warnings": warnings,
        "model_usage": llm_meta,
        "awaiting_review": False,
        "review_status": "draft_materials_ready",
        "upstream": upstream,
    }

    next_tasks: list[dict[str, Any]] = []
    if bool(tailor_policy.get("enqueue_openclaw_apply", True)):
        next_tasks.append(
            {
                "task_type": "openclaw_apply_draft_v1",
                "payload_json": {
                    "pipeline_id": pipeline_id,
                    "upstream": build_upstream_ref(task, "resume_tailor_v1"),
                    "request": request,
                    "draft_policy": {
                        "notify_channels": request.get("notify_channels"),
                    },
                    "lineage": payload.get("lineage") if isinstance(payload.get("lineage"), dict) else {},
                },
                "idempotency_key": stage_idempotency_key(
                    pipeline_id,
                    "openclaw_apply_draft_v1",
                    str(getattr(task, "_run_id", "") or ""),
                    prefix="jobapply",
                ),
            }
        )

    return {
        "artifact_type": "resume.tailor.v1",
        "content_json": artifact,
        "usage": usage,
        "next_tasks": next_tasks,
        "debug_json": {
            "generation_mode": generation_mode,
            "llm_runtime_enabled": runtime_llm,
            "warnings": warnings,
            "prompt_version": RESUME_TAILOR_PROMPT_VERSION,
            "application_answers_count": len(application_answers_artifact["items"]),
        },
    }
