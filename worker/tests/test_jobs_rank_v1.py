import json
import os
import sys
from types import SimpleNamespace

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "worker"))

from task_handlers import jobs_rank_v1
from task_handlers.prompts.jobs_rank_v1 import SCORING_OUTPUT_SCHEMA, build_scoring_messages


def _task(payload: dict, *, task_id: str = "task-rank-1", run_id: str = "run-rank-1", model: str = "gpt-5-mini") -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        model=model,
        max_attempts=3,
        _run_id=run_id,
        payload_json=json.dumps(payload, separators=(",", ":"), ensure_ascii=True),
    )


def test_jobs_rank_prompt_includes_structured_output_contract() -> None:
    messages = build_scoring_messages(
        jobs_batch=[
            {
                "normalized_job_id": "job-1",
                "title": "ML Engineer",
                "company": "Acme",
                "location": "Remote",
            }
        ],
        request={"titles": ["ml engineer"], "locations": ["Remote"]},
        profile_context={"applied": True, "resume_text": "sample resume"},
    )

    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    payload = json.loads(messages[1]["content"])
    assert payload["output_contract"] == SCORING_OUTPUT_SCHEMA
    rules = payload.get("rules") or []
    assert any("Top-level JSON must be an object with key 'scores'" in row for row in rules)
    assert any("Each scores item must include exactly" in row for row in rules)


def test_jobs_rank_v1_llm_scoring_outputs_structured_fields(monkeypatch) -> None:
    monkeypatch.setenv("USE_LLM", "true")
    monkeypatch.setattr(
        jobs_rank_v1,
        "fetch_upstream_result_content_json",
        lambda db, upstream: {
            "artifact_type": "jobs.normalize.v1",
            "normalized_jobs": [
                {
                    "normalized_job_id": "n1",
                    "title": "Senior Machine Learning Engineer",
                    "company": "Acme",
                    "location": "Remote",
                    "source": "linkedin",
                    "source_url": "https://www.linkedin.com/jobs/search",
                    "description_snippet": "Remote role building ML systems.",
                    "salary_min": 170000,
                    "salary_max": 210000,
                },
                {
                    "normalized_job_id": "n2",
                    "title": "Senior ML Engineer",
                    "company": "Acme",
                    "location": "Remote",
                    "source": "indeed",
                    "source_url": "https://www.indeed.com/jobs",
                    "description_snippet": "Senior platform role.",
                    "salary_min": 165000,
                    "salary_max": 205000,
                },
                {
                    "normalized_job_id": "n3",
                    "title": "Machine Learning Engineer",
                    "company": "Beta Labs",
                    "location": "New York, NY",
                    "source": "glassdoor",
                    "source_url": "https://www.glassdoor.com/Job/jobs.htm",
                    "description_snippet": "Hybrid applied ML role.",
                    "salary_min": 160000,
                    "salary_max": 190000,
                },
            ],
        },
    )
    monkeypatch.setattr(
        jobs_rank_v1,
        "resolve_profile_context",
        lambda request: {
            "enabled": True,
            "applied": True,
            "source": "payload",
            "resume_name": "resume.pdf",
            "updated_at": None,
            "resume_char_count": 1000,
            "resume_sent_char_count": 1000,
            "resume_truncated": False,
            "resume_text": "Experienced ML engineer with production systems background.",
        },
    )

    def _fake_llm(**kwargs):
        del kwargs
        return {
            "output_text": json.dumps(
                {
                    "scores": [
                        {
                            "job_id": "n1",
                            "resume_match_score": 92,
                            "title_match_score": 94,
                            "salary_score": 88,
                            "location_score": 98,
                            "seniority_score": 90,
                            "overall_score": 93,
                            "explanation": "Excellent alignment with ML systems experience and remote preference.",
                        },
                        {
                            "job_id": "n2",
                            "resume_match_score": 91,
                            "title_match_score": 93,
                            "salary_score": 86,
                            "location_score": 97,
                            "seniority_score": 90,
                            "overall_score": 92,
                            "explanation": "Strong overlap but similar to another Acme role.",
                        },
                        {
                            "job_id": "n3",
                            "resume_match_score": 84,
                            "title_match_score": 88,
                            "salary_score": 82,
                            "location_score": 72,
                            "seniority_score": 80,
                            "overall_score": 84,
                            "explanation": "Good skill match, slightly weaker location fit.",
                        },
                    ],
                    "summary": "Top matches are n1 and n2; n3 is viable but less aligned on location.",
                },
                separators=(",", ":"),
                ensure_ascii=True,
            ),
            "tokens_in": 1000,
            "tokens_out": 400,
            "cost_usd": "0.00150000",
            "openai_request_id": "req-1",
        }

    monkeypatch.setattr(jobs_rank_v1, "run_chat_completion", _fake_llm)

    payload = {
        "pipeline_id": "pipe-rank-1",
        "upstream": {"task_id": "norm-task", "run_id": "norm-run", "task_type": "jobs_normalize_v1"},
        "request": {
            "titles": ["machine learning engineer"],
            "locations": ["Remote", "New York, NY"],
            "work_mode_preference": ["remote", "hybrid"],
        },
        "rank_policy": {"llm_enabled": True, "llm_batch_size": 10, "max_ranked": 50},
    }
    result = jobs_rank_v1.execute(_task(payload), db=object())
    artifact = result["content_json"]

    assert artifact["artifact_type"] == "jobs.rank.v1"
    assert artifact["jobs_scored_artifact"]["artifact_type"] == "jobs_scored.v1"
    assert len(artifact["ranked_jobs"]) == 3
    top = artifact["ranked_jobs"][0]
    assert "resume_match_score" in top
    assert "title_match_score" in top
    assert "salary_score" in top
    assert "location_score" in top
    assert "seniority_score" in top
    assert "overall_score" in top
    assert "explanation_summary" in top
    assert len(top["explanation_summary"]) <= 140
    assert artifact["model_usage"]["llm_runtime_enabled"] is True
    assert result["usage"]["tokens_in"] == 1000
    assert result["usage"]["tokens_out"] == 400
    assert result["usage"]["cost_usd"] == "0.00150000"
    assert result["usage"]["openai_request_ids"] == ["req-1"]
    assert result["usage"]["ai_usage_task_run_ids"] == ["task-rank-1:run-rank-1:jobs_rank_batch_1_1"]
    assert result["next_tasks"][0]["task_type"] == "jobs_shortlist_v1"


def test_jobs_rank_v1_retries_malformed_llm_output(monkeypatch) -> None:
    monkeypatch.setenv("USE_LLM", "true")
    monkeypatch.setattr(
        jobs_rank_v1,
        "fetch_upstream_result_content_json",
        lambda db, upstream: {
            "artifact_type": "jobs.normalize.v1",
            "normalized_jobs": [
                {
                    "normalized_job_id": "n1",
                    "title": "ML Engineer",
                    "company": "Acme",
                    "location": "Remote",
                    "source": "linkedin",
                }
            ],
        },
    )
    monkeypatch.setattr(
        jobs_rank_v1,
        "resolve_profile_context",
        lambda request: {
            "enabled": True,
            "applied": True,
            "source": "payload",
            "resume_name": "resume.pdf",
            "updated_at": None,
            "resume_char_count": 200,
            "resume_sent_char_count": 200,
            "resume_truncated": False,
            "resume_text": "ML engineer resume.",
        },
    )

    calls = {"count": 0}

    def _flaky_llm(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return {
                "output_text": "not-json",
                "tokens_in": 100,
                "tokens_out": 50,
                "cost_usd": "0.00030000",
                "openai_request_id": "req-malformed",
            }
        return {
            "output_text": json.dumps(
                {
                    "scores": [
                        {
                            "job_id": "n1",
                            "resume_match_score": 90,
                            "title_match_score": 92,
                            "salary_score": 80,
                            "location_score": 95,
                            "seniority_score": 85,
                            "overall_score": 90,
                            "explanation": "Strong fit.",
                        }
                    ]
                },
                separators=(",", ":"),
                ensure_ascii=True,
            ),
            "tokens_in": 100,
            "tokens_out": 50,
            "cost_usd": "0.00030000",
            "openai_request_id": "req-ok",
        }

    monkeypatch.setattr(jobs_rank_v1, "run_chat_completion", _flaky_llm)

    payload = {
        "pipeline_id": "pipe-rank-2",
        "upstream": {"task_id": "norm-task", "run_id": "norm-run", "task_type": "jobs_normalize_v1"},
        "request": {"titles": ["ml engineer"]},
        "rank_policy": {"llm_enabled": True, "llm_max_retries": 3, "strict_llm_output": True},
    }
    result = jobs_rank_v1.execute(_task(payload), db=object())
    artifact = result["content_json"]

    assert calls["count"] == 2
    assert artifact["ranked_jobs"][0]["resume_match_score"] == 90.0
    assert artifact["model_usage"]["llm_runtime_enabled"] is True
    assert result["usage"]["tokens_in"] == 200
    assert result["usage"]["tokens_out"] == 100
    assert result["usage"]["cost_usd"] == "0.00060000"
    assert result["usage"]["ai_usage_task_run_ids"] == [
        "task-rank-1:run-rank-1:jobs_rank_batch_1_1",
        "task-rank-1:run-rank-1:jobs_rank_batch_1_2",
    ]


def test_jobs_rank_v1_retries_when_schema_required_field_missing(monkeypatch) -> None:
    monkeypatch.setenv("USE_LLM", "true")
    monkeypatch.setattr(
        jobs_rank_v1,
        "fetch_upstream_result_content_json",
        lambda db, upstream: {
            "artifact_type": "jobs.normalize.v1",
            "normalized_jobs": [
                {
                    "normalized_job_id": "n1",
                    "title": "ML Engineer",
                    "company": "Acme",
                    "location": "Remote",
                    "source": "linkedin",
                }
            ],
        },
    )
    monkeypatch.setattr(
        jobs_rank_v1,
        "resolve_profile_context",
        lambda request: {
            "enabled": True,
            "applied": True,
            "source": "payload",
            "resume_name": "resume.pdf",
            "updated_at": None,
            "resume_char_count": 200,
            "resume_sent_char_count": 200,
            "resume_truncated": False,
            "resume_text": "ML engineer resume.",
        },
    )

    calls = {"count": 0}

    def _flaky_schema_llm(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return {
                "output_text": json.dumps(
                    {
                        "scores": [
                            {
                                "job_id": "n1",
                                "resume_match_score": 90,
                                "title_match_score": 92,
                                "salary_score": 80,
                                "location_score": 95,
                                "seniority_score": 85,
                                "overall_score": 90,
                            }
                        ]
                    },
                    separators=(",", ":"),
                    ensure_ascii=True,
                ),
                "tokens_in": 100,
                "tokens_out": 50,
                "cost_usd": "0.00030000",
                "openai_request_id": "req-schema-missing",
            }
        return {
            "output_text": json.dumps(
                {
                    "scores": [
                        {
                            "job_id": "n1",
                            "resume_match_score": 91,
                            "title_match_score": 93,
                            "salary_score": 81,
                            "location_score": 96,
                            "seniority_score": 86,
                            "overall_score": 91,
                            "explanation": "Strong fit with title and location preferences.",
                        }
                    ],
                    "summary": "n1 is a strong fit.",
                },
                separators=(",", ":"),
                ensure_ascii=True,
            ),
            "tokens_in": 100,
            "tokens_out": 50,
            "cost_usd": "0.00030000",
            "openai_request_id": "req-schema-ok",
        }

    monkeypatch.setattr(jobs_rank_v1, "run_chat_completion", _flaky_schema_llm)

    payload = {
        "pipeline_id": "pipe-rank-schema-retry",
        "upstream": {"task_id": "norm-task", "run_id": "norm-run", "task_type": "jobs_normalize_v1"},
        "request": {"titles": ["ml engineer"]},
        "rank_policy": {"llm_enabled": True, "llm_max_retries": 3, "strict_llm_output": True},
    }
    result = jobs_rank_v1.execute(_task(payload), db=object())
    artifact = result["content_json"]

    assert calls["count"] == 2
    assert artifact["ranked_jobs"][0]["resume_match_score"] == 91.0
    assert artifact["model_usage"]["llm_runtime_enabled"] is True


def test_jobs_rank_v1_forwards_shortlist_freshness_controls(monkeypatch) -> None:
    monkeypatch.setenv("USE_LLM", "false")
    monkeypatch.setattr(
        jobs_rank_v1,
        "fetch_upstream_result_content_json",
        lambda db, upstream: {
            "artifact_type": "jobs.normalize.v1",
            "normalized_jobs": [
                {
                    "normalized_job_id": "n1",
                    "title": "ML Engineer",
                    "company": "Acme",
                    "location": "Remote",
                    "source": "linkedin",
                }
            ],
        },
    )
    monkeypatch.setattr(
        jobs_rank_v1,
        "resolve_profile_context",
        lambda request: {
            "enabled": False,
            "applied": False,
            "source": "none",
            "resume_name": None,
            "updated_at": None,
            "resume_char_count": 0,
            "resume_sent_char_count": 0,
            "resume_truncated": False,
            "resume_text": "",
        },
    )

    payload = {
        "pipeline_id": "pipe-rank-3",
        "upstream": {"task_id": "norm-task", "run_id": "norm-run", "task_type": "jobs_normalize_v1"},
        "request": {
            "titles": ["ml engineer"],
            "shortlist_max_items": 4,
            "shortlist_freshness_preference": "prefer_recent",
            "shortlist_freshness_weight_enabled": True,
            "shortlist_freshness_max_bonus": 6.0,
        },
    }
    result = jobs_rank_v1.execute(_task(payload), db=object())
    shortlist_policy = result["next_tasks"][0]["payload_json"]["shortlist_policy"]

    assert shortlist_policy["max_items"] == 4
    assert shortlist_policy["freshness_preference"] == "prefer_recent"
    assert shortlist_policy["freshness_weight_enabled"] is True
    assert shortlist_policy["freshness_max_bonus"] == 6.0


def test_jobs_rank_v1_empty_input_produces_empty_ranked_jobs(monkeypatch) -> None:
    monkeypatch.setenv("USE_LLM", "false")
    monkeypatch.setattr(
        jobs_rank_v1,
        "fetch_upstream_result_content_json",
        lambda db, upstream: {
            "artifact_type": "jobs.normalize.v1",
            "normalized_jobs": [],
            "counts": {"raw_count": 0, "normalized_count": 0, "deduped_count": 0, "duplicates_collapsed": 0},
        },
    )
    monkeypatch.setattr(
        jobs_rank_v1,
        "resolve_profile_context",
        lambda request: {
            "enabled": False,
            "applied": False,
            "source": "none",
            "resume_name": None,
            "updated_at": None,
            "resume_char_count": 0,
            "resume_sent_char_count": 0,
            "resume_truncated": False,
            "resume_text": "",
        },
    )

    payload = {
        "pipeline_id": "pipe-rank-empty",
        "upstream": {"task_id": "norm-task", "run_id": "norm-run", "task_type": "jobs_normalize_v1"},
        "request": {"titles": ["ml engineer"]},
    }
    result = jobs_rank_v1.execute(_task(payload), db=object())
    artifact = result["content_json"]

    assert artifact["artifact_type"] == "jobs.rank.v1"
    assert artifact["ranked_jobs"] == []
    assert artifact["jobs_scored_artifact"]["jobs_scored"] == []
    assert artifact["pipeline_counts"]["scored_count"] == 0
    assert result["next_tasks"][0]["task_type"] == "jobs_shortlist_v1"


def test_jobs_rank_v1_strict_mode_raises_when_llm_malformed_all_retries(monkeypatch) -> None:
    monkeypatch.setenv("USE_LLM", "true")
    monkeypatch.setattr(
        jobs_rank_v1,
        "fetch_upstream_result_content_json",
        lambda db, upstream: {
            "artifact_type": "jobs.normalize.v1",
            "normalized_jobs": [
                {
                    "normalized_job_id": "n1",
                    "title": "ML Engineer",
                    "company": "Acme",
                    "location": "Remote",
                    "source": "linkedin",
                }
            ],
        },
    )
    monkeypatch.setattr(
        jobs_rank_v1,
        "resolve_profile_context",
        lambda request: {
            "enabled": True,
            "applied": True,
            "source": "payload",
            "resume_name": "resume.pdf",
            "updated_at": None,
            "resume_char_count": 100,
            "resume_sent_char_count": 100,
            "resume_truncated": False,
            "resume_text": "resume",
        },
    )
    monkeypatch.setattr(
        jobs_rank_v1,
        "run_chat_completion",
        lambda **kwargs: {
            "output_text": "not-json",
            "tokens_in": 10,
            "tokens_out": 10,
            "cost_usd": "0.00010000",
            "openai_request_id": "req-rank-malformed",
        },
    )

    payload = {
        "pipeline_id": "pipe-rank-strict",
        "upstream": {"task_id": "norm-task", "run_id": "norm-run", "task_type": "jobs_normalize_v1"},
        "request": {"titles": ["ml engineer"]},
        "rank_policy": {"llm_enabled": True, "llm_max_retries": 2, "strict_llm_output": True},
    }
    with pytest.raises(RuntimeError, match="temporary llm scoring failure") as exc_info:
        jobs_rank_v1.execute(_task(payload), db=object())

    usage = getattr(exc_info.value, "usage", {})
    assert usage.get("tokens_in") == 20
    assert usage.get("tokens_out") == 20
    assert usage.get("cost_usd") == "0.00020000"
    assert usage.get("ai_usage_task_run_ids") == [
        "task-rank-1:run-rank-1:jobs_rank_batch_1_1",
        "task-rank-1:run-rank-1:jobs_rank_batch_1_2",
    ]


def test_jobs_rank_v1_non_strict_default_falls_back_when_llm_batches_fail(monkeypatch) -> None:
    monkeypatch.setenv("USE_LLM", "true")
    monkeypatch.setattr(
        jobs_rank_v1,
        "fetch_upstream_result_content_json",
        lambda db, upstream: {
            "artifact_type": "jobs.normalize.v1",
            "normalized_jobs": [
                {
                    "normalized_job_id": "n1",
                    "title": "ML Engineer",
                    "company": "Acme",
                    "location": "Remote",
                    "source": "linkedin",
                }
            ],
        },
    )
    monkeypatch.setattr(
        jobs_rank_v1,
        "resolve_profile_context",
        lambda request: {
            "enabled": True,
            "applied": True,
            "source": "payload",
            "resume_name": "resume.pdf",
            "updated_at": None,
            "resume_char_count": 100,
            "resume_sent_char_count": 100,
            "resume_truncated": False,
            "resume_text": "resume",
        },
    )
    monkeypatch.setattr(
        jobs_rank_v1,
        "run_chat_completion",
        lambda **kwargs: {
            "output_text": "not-json",
            "tokens_in": 10,
            "tokens_out": 10,
            "cost_usd": "0.00010000",
            "openai_request_id": "req-rank-malformed-default",
        },
    )

    payload = {
        "pipeline_id": "pipe-rank-nonstrict-default",
        "upstream": {"task_id": "norm-task", "run_id": "norm-run", "task_type": "jobs_normalize_v1"},
        "request": {"titles": ["ml engineer"]},
        "rank_policy": {"llm_enabled": True, "llm_max_retries": 1},
    }
    result = jobs_rank_v1.execute(_task(payload), db=object())
    artifact = result["content_json"]

    assert len(artifact["ranked_jobs"]) == 1
    assert artifact["ranked_jobs"][0]["scoring_mode"] == "deterministic_fallback"
    warnings = artifact.get("warnings") or []
    assert any("llm_batch_1_failed" in row for row in warnings)


def test_jobs_rank_v1_fast_fallback_after_repeated_malformed_output(monkeypatch) -> None:
    monkeypatch.setenv("USE_LLM", "true")
    monkeypatch.setattr(
        jobs_rank_v1,
        "fetch_upstream_result_content_json",
        lambda db, upstream: {
            "artifact_type": "jobs.normalize.v1",
            "normalized_jobs": [
                {"normalized_job_id": "n1", "title": "ML Engineer", "company": "Acme", "location": "Remote"},
                {"normalized_job_id": "n2", "title": "MLE", "company": "Beta", "location": "Remote"},
                {"normalized_job_id": "n3", "title": "AI Engineer", "company": "Gamma", "location": "Remote"},
            ],
        },
    )
    monkeypatch.setattr(
        jobs_rank_v1,
        "resolve_profile_context",
        lambda request: {
            "enabled": True,
            "applied": True,
            "source": "payload",
            "resume_name": "resume.pdf",
            "updated_at": None,
            "resume_char_count": 100,
            "resume_sent_char_count": 100,
            "resume_truncated": False,
            "resume_text": "resume",
        },
    )
    calls = {"count": 0}
    def _always_bad_llm(**kwargs):
        del kwargs
        calls["count"] += 1
        return {
            "output_text": "not-json",
            "tokens_in": 10,
            "tokens_out": 10,
            "cost_usd": "0.00010000",
            "openai_request_id": f"req-rank-fast-fallback-{calls['count']}",
        }

    monkeypatch.setattr(jobs_rank_v1, "run_chat_completion", _always_bad_llm)

    payload = {
        "pipeline_id": "pipe-rank-fast-fallback",
        "upstream": {"task_id": "norm-task", "run_id": "norm-run", "task_type": "jobs_normalize_v1"},
        "request": {"titles": ["ml engineer"]},
        "rank_policy": {"llm_enabled": True, "llm_max_retries": 4, "strict_llm_output": False},
    }
    result = jobs_rank_v1.execute(_task(payload), db=object())
    artifact = result["content_json"]

    assert calls["count"] == 2
    assert all(row["scoring_mode"] == "deterministic_fallback" for row in artifact["ranked_jobs"])
    assert artifact["jobs_scored_artifact"]["llm"]["attempts_total"] == 2
    warnings = artifact.get("warnings") or []
    assert any("llm_batch_1_stop_reason:fast_fail_repeated_output_pattern" in row for row in warnings)
    debug_json = result.get("debug_json") or {}
    assert debug_json.get("fallback_used") is True
    assert debug_json.get("strict_llm_output") is False
