import json
import os
import sys
from types import SimpleNamespace

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "worker"))

from task_handlers import job_apply_prepare_v1, openclaw_apply_draft_v1, resume_tailor_v1
from task_handlers.errors import NonRetryableTaskError


def _task(payload: dict, *, task_id: str, run_id: str, model: str = "gpt-5") -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        _run_id=run_id,
        model=model,
        max_attempts=3,
        payload_json=json.dumps(payload, separators=(",", ":"), ensure_ascii=True),
    )


def test_job_apply_prepare_extracts_shortlist_job_and_queues_resume_tailor(monkeypatch) -> None:
    monkeypatch.setattr(
        job_apply_prepare_v1,
        "fetch_upstream_result_content_json",
        lambda db, upstream: {
            "artifact_type": "jobs.shortlist.v1",
            "shortlist": [
                {
                    "job_id": "job-1",
                    "title": "Senior ML Engineer",
                    "company": "Acme AI",
                    "location": "Remote",
                    "source": "linkedin",
                    "source_url": "https://linkedin.example/jobs/1",
                    "url": "https://linkedin.example/jobs/view/1",
                    "work_mode": "remote",
                    "posted_age_days": 3,
                    "description_snippet": "Required: production ML experience. Must be strong in Python and cloud systems.",
                }
            ],
        },
    )
    monkeypatch.setattr(
        job_apply_prepare_v1,
        "resolve_profile_context",
        lambda request: {
            "applied": True,
            "source": "stored_resume_profile",
            "resume_name": "Master Resume",
            "resume_sha256": "resume-sha",
            "resume_char_count": 1234,
            "resume_text": "Candidate resume body",
        },
    )

    payload = {
        "pipeline_id": "pipe-apply-1",
        "upstream": {"task_id": "task-shortlist", "run_id": "run-shortlist", "task_type": "jobs_shortlist_v1"},
        "request": {"notify_channels": ["discord"]},
        "selection": {"job_id": "job-1"},
        "prepare_policy": {"include_cover_letter": True},
    }
    result = job_apply_prepare_v1.execute(_task(payload, task_id="task-prepare", run_id="run-prepare"), db=object())
    artifact = result["content_json"]

    assert artifact["artifact_type"] == "job.apply.prepare.v1"
    assert artifact["application_target"]["job_id"] == "job-1"
    assert artifact["candidate_profile"]["resume_source"] == "stored_resume_profile"
    assert artifact["requirements_summary"]
    assert result["next_tasks"][0]["task_type"] == "resume_tailor_v1"


def test_resume_tailor_generates_structured_drafts_and_usage(monkeypatch) -> None:
    monkeypatch.setenv("USE_LLM", "true")
    monkeypatch.setattr(
        resume_tailor_v1,
        "fetch_upstream_result_content_json",
        lambda db, upstream: {
            "artifact_type": "job.apply.prepare.v1",
            "application_target": {
                "job_id": "job-1",
                "title": "Senior ML Engineer",
                "company": "Acme AI",
                "location": "Remote",
                "source": "linkedin",
                "source_url": "https://linkedin.example/jobs/1",
                "application_url": "https://linkedin.example/jobs/view/1",
            },
            "extracted_requirements": [
                {"requirement": "Production ML systems experience", "source": "job_text", "confidence": "explicit"},
                {"requirement": "Strong Python skills", "source": "job_text", "confidence": "explicit"},
            ],
            "common_questions": [
                {"question": "Why are you interested in this role?", "answer_type": "motivation"},
                {"question": "What relevant experience do you bring?", "answer_type": "experience"},
            ],
        },
    )
    monkeypatch.setattr(
        resume_tailor_v1,
        "resolve_profile_context",
        lambda request: {
            "applied": True,
            "source": "stored_resume_profile",
            "resume_name": "Master Resume",
            "resume_sha256": "resume-sha",
            "resume_char_count": 1234,
            "resume_text": "Built ML systems in production and led backend platform work.",
            "metadata_json": {"location": "Remote"},
        },
    )
    monkeypatch.setattr(
        resume_tailor_v1,
        "run_chat_completion",
        lambda **kwargs: {
            "output_text": json.dumps(
                {
                    "resume_variant_name": "Tailored Resume - Acme AI",
                    "resume_variant_text": "Tailored resume body",
                    "resume_strategy_summary": "Emphasize production ML and backend systems impact.",
                    "requirements_alignment": [
                        {"requirement": "Production ML systems experience", "coverage": "strong", "evidence": "Directly shown in prior work."},
                        {"requirement": "Strong Python skills", "coverage": "strong", "evidence": "Python-heavy production work."},
                    ],
                    "application_answers": [
                        {"question": "Why are you interested in this role?", "answer": "It aligns with my production ML background.", "answer_type": "motivation"},
                        {"question": "What relevant experience do you bring?", "answer": "I have shipped and maintained ML systems.", "answer_type": "experience"},
                    ],
                    "cover_letter_text": "Dear Hiring Team, ...",
                    "operator_notes": ["Verify the quantified project outcomes before submission."],
                },
                separators=(",", ":"),
                ensure_ascii=True,
            ),
            "tokens_in": 700,
            "tokens_out": 420,
            "cost_usd": "0.00310000",
            "openai_request_id": "req-tailor-1",
        },
    )

    payload = {
        "pipeline_id": "pipe-apply-1",
        "upstream": {"task_id": "task-prepare", "run_id": "run-prepare", "task_type": "job_apply_prepare_v1"},
        "request": {"notify_channels": ["discord"]},
        "tailor_policy": {"include_cover_letter": True, "enqueue_openclaw_apply": True},
    }
    result = resume_tailor_v1.execute(_task(payload, task_id="task-tailor", run_id="run-tailor"), db=object())
    artifact = result["content_json"]

    assert artifact["artifact_type"] == "resume.tailor.v1"
    assert artifact["generation_mode"] == "llm_structured"
    assert artifact["resume_variant_artifact"]["resume_variant_name"] == "Tailored Resume - Acme AI"
    assert len(artifact["application_answers_artifact"]["items"]) == 2
    assert artifact["cover_letter_artifact"]["enabled"] is True
    assert result["usage"]["tokens_in"] == 700
    assert result["usage"]["openai_request_ids"] == ["req-tailor-1"]
    assert result["next_tasks"][0]["task_type"] == "openclaw_apply_draft_v1"


def test_openclaw_apply_draft_persists_review_artifact_and_notify_followup(monkeypatch) -> None:
    monkeypatch.setattr(
        openclaw_apply_draft_v1,
        "fetch_upstream_result_content_json",
        lambda db, upstream: {
            "artifact_type": "resume.tailor.v1",
            "application_target": {
                "job_id": "job-1",
                "title": "Senior ML Engineer",
                "company": "Acme AI",
                "source": "linkedin",
                "source_url": "https://linkedin.example/jobs/1",
                "application_url": "https://linkedin.example/jobs/view/1",
            },
            "resume_variant_artifact": {
                "resume_variant_name": "Tailored Resume - Acme AI",
                "resume_variant_text": "Tailored resume body",
                "resume_file_name": "tailored_resume_acme_ai.txt",
                "base_resume_name": "Master Resume",
                "base_resume_sha256": "resume-sha",
            },
            "application_answers_artifact": {
                "items": [
                    {"question": "Why are you interested?", "answer": "Strong fit.", "answer_type": "motivation"}
                ]
            },
            "cover_letter_artifact": {"text": "Dear Hiring Team, ..."},
        },
    )
    monkeypatch.setattr(openclaw_apply_draft_v1, "openclaw_apply_enabled", lambda request: True)
    monkeypatch.setattr(openclaw_apply_draft_v1, "openclaw_apply_command_configured", lambda: True)
    monkeypatch.setattr(
        openclaw_apply_draft_v1,
        "run_openclaw_apply_draft",
        lambda **kwargs: {
            "status": "awaiting_review",
            "warnings": [],
            "errors": [],
            "meta": {
                "awaiting_review": True,
                "submitted": False,
                "account_created": True,
                "failure_category": None,
                "fields_filled_manifest": [
                    {"field_name": "first_name", "status": "filled", "value_redacted": True},
                    {"field_name": "resume_upload", "status": "uploaded", "value_redacted": True},
                ],
                "screenshots": [
                    {"label": "application-form", "path": "/tmp/app-form.png", "kind": "checkpoint"}
                ],
                "checkpoint_urls": ["https://linkedin.example/jobs/view/1"],
                "page_title": "Apply - Senior ML Engineer",
            },
        },
    )

    payload = {
        "pipeline_id": "pipe-apply-1",
        "upstream": {"task_id": "task-tailor", "run_id": "run-tailor", "task_type": "resume_tailor_v1"},
        "request": {"openclaw_apply_enabled": True, "notify_channels": ["discord"]},
        "draft_policy": {"notify_channels": ["discord"]},
    }
    result = openclaw_apply_draft_v1.execute(_task(payload, task_id="task-draft", run_id="run-draft"), db=object())
    artifact = result["content_json"]
    notify_payload = result["next_tasks"][0]["payload_json"]

    assert artifact["artifact_type"] == "openclaw.apply.draft.v1"
    assert artifact["awaiting_review"] is True
    assert artifact["submitted"] is False
    assert artifact["account_created_flag"] is True
    assert len(artifact["fields_filled_manifest"]) == 2
    assert len(artifact["screenshot_metadata_references"]) == 1
    assert notify_payload["source_task_type"] == "openclaw_apply_draft_v1"
    assert "Application draft ready for human review." in notify_payload["message"]
    assert "Submission: not attempted" in notify_payload["message"]


def test_openclaw_apply_draft_fails_clearly_when_disabled() -> None:
    payload = {
        "pipeline_id": "pipe-apply-1",
        "upstream": {"task_id": "task-tailor", "run_id": "run-tailor", "task_type": "resume_tailor_v1"},
    }
    with pytest.raises(NonRetryableTaskError, match="disabled"):
        openclaw_apply_draft_v1.execute(_task(payload, task_id="task-draft", run_id="run-draft"), db=object())
