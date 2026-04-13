import json
import os
import sys
from types import SimpleNamespace

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "worker"))

from task_handlers import job_apply_manual_seed_v1, job_apply_prepare_v1, openclaw_apply_draft_v1, resume_tailor_v1
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
            "metadata_json": {"contact_profile": {"city": "Saint Mary's City"}},
            "contact_profile": {"city": "Saint Mary's City", "country": "United States"},
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
    assert artifact["candidate_profile"]["contact_profile"]["city"] == "Saint Mary's City"
    assert artifact["requirements_summary"]
    assert result["next_tasks"][0]["task_type"] == "resume_tailor_v1"


def test_job_apply_manual_seed_creates_prepare_followup_and_preserves_manual_lineage() -> None:
    payload = {
        "pipeline_id": "pipe-manual-1",
        "manual_job": {
            "job_id": "li-123",
            "normalized_job_id": "linkedin-li-123",
            "title": "Software Engineer, AI",
            "company": "Manual Labs",
            "source": "linkedin",
            "source_url": "https://www.linkedin.com/jobs/view/li-123?trk=public_jobs_topcard-title",
            "application_url": "https://www.linkedin.com/jobs/view/li-123?trk=public_jobs_topcard-title",
        },
        "request": {
            "profile_mode": "resume_profile",
            "notify_channels": ["discord"],
            "openclaw_apply_enabled": True,
        },
        "prepare_policy": {"include_cover_letter": True, "enqueue_openclaw_apply": True},
    }

    result = job_apply_manual_seed_v1.execute(_task(payload, task_id="task-manual", run_id="run-manual"), db=object())
    artifact = result["content_json"]
    next_payload = result["next_tasks"][0]["payload_json"]

    assert artifact["artifact_type"] == "job.apply.manual_seed.v1"
    assert artifact["lineage"]["source"] == "manual_api"
    assert artifact["lineage"]["seed_kind"] == "manual_seed"
    assert artifact["lineage"]["path"] == "manual_api/manual_seed"
    assert artifact["selected_job"]["company"] == "Manual Labs"
    assert result["next_tasks"][0]["task_type"] == "job_apply_prepare_v1"
    assert next_payload["selected_job"]["company"] == "Manual Labs"
    assert next_payload["upstream"]["task_type"] == "job_apply_manual_seed_v1"
    assert result["next_tasks"][0]["idempotency_key"].startswith("jobapply:manual-labs:")


def test_job_apply_prepare_accepts_manual_seed_upstream_without_shortlist_artifact(monkeypatch) -> None:
    monkeypatch.setattr(
        job_apply_prepare_v1,
        "fetch_upstream_result_content_json",
        lambda db, upstream: {
            "artifact_type": "job.apply.manual_seed.v1",
            "selected_job": {
                "job_id": "manual-123",
                "normalized_job_id": "linkedin-manual-123",
                "title": "Machine Learning Engineer",
                "company": "Manual Labs",
                "source": "linkedin",
                "source_url": "https://www.linkedin.com/jobs/view/manual-123",
                "application_url": "https://www.linkedin.com/jobs/view/manual-123",
                "description_snippet": "Required: strong Python and ML systems experience.",
            },
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
        "pipeline_id": "pipe-manual-2",
        "upstream": {"task_id": "task-manual", "run_id": "run-manual", "task_type": "job_apply_manual_seed_v1"},
        "request": {"notify_channels": ["discord"]},
        "prepare_policy": {"include_cover_letter": True},
    }
    result = job_apply_prepare_v1.execute(_task(payload, task_id="task-prepare", run_id="run-prepare"), db=object())
    artifact = result["content_json"]

    assert artifact["application_target"]["job_id"] == "manual-123"
    assert artifact["application_target"]["company"] == "Manual Labs"
    assert artifact["selected_job_source"] == "manual_seed_artifact"
    assert result["next_tasks"][0]["task_type"] == "resume_tailor_v1"


def test_job_apply_prepare_propagates_manual_selected_job_fields(monkeypatch) -> None:
    monkeypatch.setattr(
        job_apply_prepare_v1,
        "fetch_upstream_result_content_json",
        lambda db, upstream: {"artifact_type": "job.apply.manual_seed.v1", "selected_job": {"title": "Unused upstream job"}},
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
        "pipeline_id": "pipe-manual-3",
        "upstream": {"task_id": "task-manual", "run_id": "run-manual", "task_type": "job_apply_manual_seed_v1"},
        "request": {"notify_channels": ["discord"]},
        "selected_job": {
            "job_id": "manual-789",
            "normalized_job_id": "linkedin-manual-789",
            "title": "AI Infrastructure Engineer",
            "company": "Propagation Labs",
            "source": "linkedin",
            "source_url": "https://www.linkedin.com/jobs/view/manual-789",
            "application_url": "https://www.linkedin.com/jobs/view/manual-789?refId=abc",
            "location": "Remote",
            "work_mode": "remote",
            "description_snippet": "Must have distributed systems and Python experience.",
        },
        "prepare_policy": {"include_cover_letter": False},
    }
    result = job_apply_prepare_v1.execute(_task(payload, task_id="task-prepare", run_id="run-prepare"), db=object())
    artifact = result["content_json"]

    assert artifact["selected_job"]["company"] == "Propagation Labs"
    assert artifact["selected_job_source"] == "payload_selected_job"
    assert artifact["application_target"]["application_url"] == "https://www.linkedin.com/jobs/view/manual-789?refId=abc"
    assert artifact["application_target"]["source_url"] == "https://www.linkedin.com/jobs/view/manual-789"


def test_job_apply_prepare_does_not_fallback_to_upstream_shortlist_job_when_payload_selected_job_exists(monkeypatch) -> None:
    monkeypatch.setattr(
        job_apply_prepare_v1,
        "fetch_upstream_result_content_json",
        lambda db, upstream: {
            "artifact_type": "jobs.shortlist.v1",
            "shortlist": [
                {
                    "job_id": "shortlist-1",
                    "title": "Wrong Upstream Role",
                    "company": "Wrong Upstream Co",
                    "source": "linkedin",
                    "source_url": "https://www.linkedin.com/jobs/view/shortlist-1",
                    "url": "https://www.linkedin.com/jobs/view/shortlist-1",
                    "description_snippet": "Required: upstream shortlist role.",
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
        "pipeline_id": "pipe-manual-4",
        "upstream": {"task_id": "task-shortlist", "run_id": "run-shortlist", "task_type": "jobs_shortlist_v1"},
        "request": {"notify_channels": ["discord"]},
        "selected_job": {
            "job_id": "manual-override-1",
            "title": "Manual Override Role",
            "company": "Correct Manual Co",
            "source": "linkedin",
            "source_url": "https://www.linkedin.com/jobs/view/manual-override-1",
            "application_url": "https://www.linkedin.com/jobs/view/manual-override-1",
            "description_snippet": "Must have production ML experience.",
        },
    }
    result = job_apply_prepare_v1.execute(_task(payload, task_id="task-prepare", run_id="run-prepare"), db=object())
    artifact = result["content_json"]

    assert artifact["selected_job_source"] == "payload_selected_job"
    assert artifact["application_target"]["job_id"] == "manual-override-1"
    assert artifact["application_target"]["company"] == "Correct Manual Co"
    assert artifact["application_target"]["company"] != "Wrong Upstream Co"


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
            "metadata_json": {
                "location": "Remote",
                "contact_profile": {"city": "Saint Mary's City", "country": "United States"},
            },
            "contact_profile": {"city": "Saint Mary's City", "country": "United States"},
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
    assert artifact["candidate_profile"]["contact_profile"]["city"] == "Saint Mary's City"
    assert artifact["resume_variant_artifact"]["resume_variant_name"] == "Tailored Resume - Acme AI"
    assert len(artifact["application_answers_artifact"]["items"]) == 2
    assert artifact["cover_letter_artifact"]["enabled"] is True
    assert result["usage"]["tokens_in"] == 700
    assert result["usage"]["openai_request_ids"] == ["req-tailor-1"]
    assert result["next_tasks"][0]["task_type"] == "openclaw_apply_draft_v1"


def test_openclaw_apply_draft_passes_contact_profile_to_runner(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("APPLICATION_DRAFT_STATE_DB_PATH", str(tmp_path / "application_draft_state.sqlite3"))
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
            "candidate_profile": {
                "resume_source": "stored_resume_profile",
                "resume_name": "Master Resume",
                "contact_profile": {
                    "city": "Saint Mary's City",
                    "state_or_province": "MD",
                    "postal_code": "20686",
                    "country": "United States",
                    "primary_phone_number": "240-555-0101",
                    "phone_type": "mobile",
                },
            },
            "resume_variant_artifact": {
                "resume_variant_name": "Tailored Resume - Acme AI",
                "resume_variant_text": "Tailored resume body",
                "resume_file_name": "tailored_resume_acme_ai.txt",
            },
            "application_answers_artifact": {"items": []},
            "cover_letter_artifact": {"text": ""},
        },
    )
    monkeypatch.setattr(openclaw_apply_draft_v1, "openclaw_apply_enabled", lambda request: True)
    monkeypatch.setattr(openclaw_apply_draft_v1, "openclaw_apply_command_configured", lambda: True)
    captured: dict[str, Any] = {}

    def _fake_run(**kwargs):
        captured.update(kwargs)
        return {
            "status": "awaiting_review",
            "warnings": [],
            "errors": [],
            "meta": {
                "draft_status": "draft_ready",
                "source_status": "success",
                "awaiting_review": True,
                "review_status": "awaiting_review",
                "submitted": False,
                "failure_category": None,
                "fields_filled_manifest": [
                    {"field_name": "city", "status": "filled", "value_redacted": True}
                ],
                "screenshots": [
                    {"label": "application-form", "path": str(tmp_path / "app-form.png"), "kind": "checkpoint"}
                ],
                "checkpoint_urls": ["https://linkedin.example/jobs/view/1"],
                "notify_decision": {"should_notify": True, "reason": "draft_ready_for_review", "channels": ["discord"]},
            },
        }

    monkeypatch.setattr(openclaw_apply_draft_v1, "run_openclaw_apply_draft", _fake_run)

    payload = {
        "pipeline_id": "pipe-apply-1",
        "upstream": {"task_id": "task-tailor", "run_id": "run-tailor", "task_type": "resume_tailor_v1"},
        "request": {"openclaw_apply_enabled": True, "notify_channels": ["discord"]},
        "draft_policy": {"notify_channels": ["discord"]},
    }
    result = openclaw_apply_draft_v1.execute(_task(payload, task_id="task-draft", run_id="run-draft"), db=object())

    assert captured["candidate_profile"]["contact_profile"]["city"] == "Saint Mary's City"
    assert result["content_json"]["draft_status"] == "draft_ready"


def test_openclaw_apply_draft_persists_review_artifact_and_notify_followup(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("APPLICATION_DRAFT_STATE_DB_PATH", str(tmp_path / "application_draft_state.sqlite3"))
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
                "draft_status": "draft_ready",
                "source_status": "success",
                "awaiting_review": True,
                "review_status": "awaiting_review",
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
                "notify_decision": {"should_notify": True, "reason": "draft_ready_for_review", "channels": ["discord"]},
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
    assert artifact["draft_status"] == "draft_ready"
    assert artifact["source_status"] == "success"
    assert artifact["awaiting_review"] is True
    assert artifact["review_status"] == "awaiting_review"
    assert artifact["submitted"] is False
    assert artifact["account_created_flag"] is True
    assert len(artifact["fields_filled_manifest"]) == 2
    assert len(artifact["screenshot_metadata_references"]) == 1
    assert notify_payload["source_task_type"] == "openclaw_apply_draft_v1"
    assert "Application draft ready for human review." in notify_payload["message"]
    assert "Submission: not attempted" in notify_payload["message"]


def test_openclaw_apply_draft_enqueues_submit_digest_after_successful_auto_submit(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("APPLICATION_DRAFT_STATE_DB_PATH", str(tmp_path / "application_draft_state.sqlite3"))
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
            },
            "application_answers_artifact": {"items": []},
            "cover_letter_artifact": {"text": ""},
        },
    )
    monkeypatch.setattr(openclaw_apply_draft_v1, "openclaw_apply_enabled", lambda request: True)
    monkeypatch.setattr(openclaw_apply_draft_v1, "openclaw_apply_command_configured", lambda: True)
    monkeypatch.setattr(
        openclaw_apply_draft_v1,
        "run_openclaw_apply_draft",
        lambda **kwargs: {
            "status": "success",
            "warnings": [],
            "errors": [],
            "meta": {
                "draft_status": "draft_ready",
                "source_status": "success",
                "awaiting_review": False,
                "review_status": "submitted",
                "submitted": True,
                "failure_category": None,
                "fields_filled_manifest": [
                    {"field_name": "first_name", "status": "filled", "value_redacted": True},
                ],
                "screenshots": [
                    {"label": "application-form", "path": "/tmp/app-form.png", "kind": "checkpoint"}
                ],
                "checkpoint_urls": ["https://linkedin.example/jobs/view/1"],
                "page_title": "Application submitted",
                "page_diagnostics": {
                    "auto_submit_allowed": True,
                    "auto_submit_attempted": True,
                    "auto_submit_succeeded": True,
                    "submit_confidence": "high",
                    "fallback_answers_used": [{"label": "Veteran status"}],
                },
                "form_diagnostics": {
                    "submit_confidence": "high",
                    "fallback_answers_used": [{"label": "Veteran status"}],
                },
                "notify_decision": {"should_notify": False, "reason": "application_submitted", "channels": []},
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

    assert artifact["submitted"] is True
    assert artifact["awaiting_review"] is False
    assert artifact["review_status"] == "submitted"
    assert artifact["notify_decision"]["should_notify"] is True
    assert notify_payload["source_task_type"] == "openclaw_apply_draft_v1"
    assert "Status: submitted" in notify_payload["message"]
    assert "Confidence: high" in notify_payload["message"]
    assert "Fallbacks: Veteran status" in notify_payload["message"]
    assert notify_payload["include_header"] is False
    assert notify_payload["include_metadata"] is False


def test_openclaw_apply_draft_fails_clearly_when_disabled() -> None:
    payload = {
        "pipeline_id": "pipe-apply-1",
        "upstream": {"task_id": "task-tailor", "run_id": "run-tailor", "task_type": "resume_tailor_v1"},
    }
    with pytest.raises(NonRetryableTaskError, match="disabled"):
        openclaw_apply_draft_v1.execute(_task(payload, task_id="task-draft", run_id="run-draft"), db=object())


def test_openclaw_apply_draft_normalizes_malformed_review_ready_output(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("APPLICATION_DRAFT_STATE_DB_PATH", str(tmp_path / "application_draft_state.sqlite3"))
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
            },
            "application_answers_artifact": {"items": []},
            "cover_letter_artifact": {"text": ""},
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
                "draft_status": "draft_ready",
                "source_status": "success",
                "awaiting_review": True,
                "review_status": "awaiting_review",
                "submitted": False,
                "failure_category": None,
                "fields_filled_manifest": [
                    {"field_name": "first_name", "status": "filled", "value_redacted": True}
                ],
                "screenshots": [],
                "checkpoint_urls": [],
                "notify_decision": {"should_notify": True, "reason": "draft_ready_for_review", "channels": ["discord"]},
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

    assert artifact["awaiting_review"] is False
    assert artifact["review_status"] == "blocked"
    assert artifact["draft_status"] == "partial_draft"
    assert artifact["failure_category"] == "manual_review_required"
    assert artifact["notify_decision"]["should_notify"] is False
    assert result["next_tasks"] == []


def test_openclaw_apply_draft_recovers_timeout_progress_diagnostics(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("APPLICATION_DRAFT_STATE_DB_PATH", str(tmp_path / "application_draft_state.sqlite3"))
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
            },
            "application_answers_artifact": {"items": []},
            "cover_letter_artifact": {"text": ""},
        },
    )
    monkeypatch.setattr(openclaw_apply_draft_v1, "openclaw_apply_enabled", lambda request: True)
    monkeypatch.setattr(openclaw_apply_draft_v1, "openclaw_apply_command_configured", lambda: True)
    monkeypatch.setattr(
        openclaw_apply_draft_v1,
        "run_openclaw_apply_draft",
        lambda **kwargs: {
            "status": "timed_out",
            "warnings": [],
            "errors": ["openclaw_apply_timed_out"],
            "meta": {
                "draft_status": "not_started",
                "source_status": "timed_out",
                "awaiting_review": False,
                "review_status": "blocked",
                "submitted": False,
                "failure_category": "timed_out",
                "fields_filled_manifest": [],
                "screenshots": [],
                "checkpoint_urls": ["https://linkedin.example/jobs/view/1"],
                "page_diagnostics": {},
                "form_diagnostics": {},
                "debug_json": {
                    "draft_progress": {
                        "progress_stage": "later_step_diagnostics",
                        "page_diagnostics": {
                            "review_step_detected": True,
                            "submit_button_present": False,
                            "later_step_decision": "continue_flow",
                        },
                        "form_diagnostics": {
                            "later_step_decision": "continue_flow",
                        },
                    }
                },
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

    assert artifact["source_status"] == "timed_out"
    assert artifact["page_diagnostics"]["review_step_detected"] is True
    assert artifact["page_diagnostics"]["later_step_decision"] == "continue_flow"
    assert artifact["form_diagnostics"]["later_step_decision"] == "continue_flow"


def test_openclaw_apply_draft_prevents_duplicate_redraft(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("APPLICATION_DRAFT_STATE_DB_PATH", str(tmp_path / "application_draft_state.sqlite3"))
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
            },
            "application_answers_artifact": {"items": []},
            "cover_letter_artifact": {"text": ""},
        },
    )
    monkeypatch.setattr(openclaw_apply_draft_v1, "openclaw_apply_enabled", lambda request: True)
    monkeypatch.setattr(openclaw_apply_draft_v1, "openclaw_apply_command_configured", lambda: True)
    calls = {"count": 0}

    def _fake_run(**kwargs):
        calls["count"] += 1
        return {
            "status": "awaiting_review",
            "warnings": [],
            "errors": [],
            "meta": {
                "draft_status": "draft_ready",
                "source_status": "success",
                "awaiting_review": True,
                "review_status": "awaiting_review",
                "submitted": False,
                "failure_category": None,
                "fields_filled_manifest": [
                    {"field_name": "first_name", "status": "filled", "value_redacted": True}
                ],
                "screenshots": [
                    {"label": "application-form", "path": str(tmp_path / "app-form.png"), "kind": "checkpoint"}
                ],
                "checkpoint_urls": ["https://linkedin.example/jobs/view/1"],
                "notify_decision": {"should_notify": True, "reason": "draft_ready_for_review", "channels": ["discord"]},
            },
        }

    monkeypatch.setattr(openclaw_apply_draft_v1, "run_openclaw_apply_draft", _fake_run)

    payload = {
        "pipeline_id": "pipe-apply-1",
        "upstream": {"task_id": "task-tailor", "run_id": "run-tailor", "task_type": "resume_tailor_v1"},
        "request": {"openclaw_apply_enabled": True, "notify_channels": ["discord"]},
        "draft_policy": {"notify_channels": ["discord"]},
    }

    first = openclaw_apply_draft_v1.execute(_task(payload, task_id="task-draft-1", run_id="run-draft-1"), db=object())
    second = openclaw_apply_draft_v1.execute(_task(payload, task_id="task-draft-2", run_id="run-draft-2"), db=object())

    assert calls["count"] == 1
    assert first["content_json"]["awaiting_review"] is True
    assert second["content_json"]["awaiting_review"] is False
    assert second["content_json"]["failure_category"] == "manual_review_required"
    assert "Duplicate application draft prevented" in second["content_json"]["blocking_reason"]
    assert second["content_json"]["notify_decision"]["should_notify"] is False
