import json
import os
import sys
from types import SimpleNamespace

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "worker"))

from task_handlers import jobs_collect_v1, jobs_digest_v1, jobs_digest_v2, jobs_normalize_v1, jobs_rank_v1, jobs_shortlist_v1


def _task(payload: dict, *, task_id: str = "task-1", run_id: str = "run-1") -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        max_attempts=3,
        _run_id=run_id,
        payload_json=json.dumps(payload, separators=(",", ":"), ensure_ascii=True),
    )


def test_jobs_digest_v1_shim_forwards_to_jobs_collect_v1() -> None:
    payload = {
        "search_query": "ml engineer",
        "location": "Remote",
        "job_boards": ["linkedin", "indeed"],
        "jobs": [{"title": "ML Engineer", "company": "Acme", "source": "manual"}],
    }
    result = jobs_digest_v1.execute(_task(payload), db=None)

    assert result["artifact_type"] == "jobs.digest.v1.compat_shim"
    assert result["content_json"]["forwarded_to"] == "jobs_collect_v1"
    assert result["next_tasks"][0]["task_type"] == "jobs_collect_v1"


def test_jobs_collect_v1_builds_normalize_followup_from_manual_jobs() -> None:
    payload = {
        "pipeline_id": "pipe-1",
        "request": {
            "collectors_enabled": False,
            "sources": ["manual"],
            "manual_jobs": [
                {"title": "AI Engineer", "company": "Acme", "location": "Remote", "source": "manual"}
            ],
        },
    }
    result = jobs_collect_v1.execute(_task(payload), db=None)

    assert result["content_json"]["artifact_type"] == "jobs.collect.v1"
    assert result["content_json"]["source_counts"]["manual"] == 1
    assert result["next_tasks"][0]["task_type"] == "jobs_normalize_v1"


def test_jobs_normalize_v1_normalizes_and_dedupes(monkeypatch) -> None:
    monkeypatch.setattr(
        jobs_normalize_v1,
        "fetch_upstream_result_content_json",
        lambda db, upstream: {
            "artifact_type": "jobs.collect.v1",
            "raw_jobs": [
                {"title": "ML Engineer", "company": "Acme", "url": "https://example.com/1", "source": "linkedin"},
                {"title": "ML Engineer", "company": "Acme", "url": "https://example.com/1", "source": "linkedin"},
            ],
            "warnings": [],
        },
    )

    payload = {
        "pipeline_id": "pipe-2",
        "upstream": {"task_id": "a", "run_id": "b", "task_type": "jobs_collect_v1"},
        "request": {"query": "ml engineer", "location": "United States"},
        "normalization_policy": {"dedupe_keys": ["source", "url", "title"]},
    }
    result = jobs_normalize_v1.execute(_task(payload), db=object())

    assert result["content_json"]["artifact_type"] == "jobs.normalize.v1"
    assert result["content_json"]["dedupe_stats"]["duplicates"] == 1
    assert result["next_tasks"][0]["task_type"] == "jobs_rank_v1"


def test_jobs_rank_shortlist_digest_chain_shapes(monkeypatch) -> None:
    monkeypatch.setattr(
        jobs_rank_v1,
        "fetch_upstream_result_content_json",
        lambda db, upstream: {
            "artifact_type": "jobs.normalize.v1",
            "normalized_jobs": [
                {
                    "title": "Machine Learning Engineer",
                    "company": "Acme",
                    "source": "linkedin",
                    "salary_min": 180000,
                    "salary_max": 220000,
                    "work_mode": "remote",
                    "experience_level": "mid",
                    "description_snippet": "ml systems",
                }
            ],
        },
    )
    monkeypatch.setattr(
        jobs_rank_v1,
        "resolve_profile_context",
        lambda request: {
            "enabled": True,
            "applied": False,
            "source": "stored_profile_missing",
            "resume_name": None,
            "updated_at": None,
            "resume_char_count": 0,
            "resume_sent_char_count": 0,
            "resume_truncated": False,
            "resume_text": None,
        },
    )

    rank_payload = {
        "pipeline_id": "pipe-3",
        "upstream": {"task_id": "a", "run_id": "b", "task_type": "jobs_normalize_v1"},
        "request": {"query": "ml engineer", "location": "Remote", "desired_title_keywords": ["machine learning engineer"]},
    }
    rank_result = jobs_rank_v1.execute(_task(rank_payload), db=object())
    assert rank_result["content_json"]["artifact_type"] == "jobs.rank.v1"
    assert rank_result["next_tasks"][0]["task_type"] == "jobs_shortlist_v1"

    monkeypatch.setattr(
        jobs_shortlist_v1,
        "fetch_upstream_result_content_json",
        lambda db, upstream: rank_result["content_json"],
    )
    shortlist_payload = {
        "pipeline_id": "pipe-3",
        "upstream": {"task_id": "a", "run_id": "b", "task_type": "jobs_rank_v1"},
        "request": {"shortlist_max_items": 5, "shortlist_min_score": 0.1},
    }
    shortlist_result = jobs_shortlist_v1.execute(_task(shortlist_payload), db=object())
    assert shortlist_result["content_json"]["artifact_type"] == "jobs.shortlist.v1"
    assert shortlist_result["next_tasks"][0]["task_type"] == "jobs_digest_v2"

    monkeypatch.setattr(
        jobs_digest_v2,
        "fetch_upstream_result_content_json",
        lambda db, upstream: shortlist_result["content_json"],
    )
    digest_payload = {
        "pipeline_id": "pipe-3",
        "upstream": {"task_id": "a", "run_id": "b", "task_type": "jobs_shortlist_v1"},
        "request": {"notify_on_empty": False},
        "digest_policy": {"notify_on_empty": False},
    }
    digest_result = jobs_digest_v2.execute(_task(digest_payload), db=object())
    assert digest_result["content_json"]["artifact_type"] == "jobs.digest.v2"
    assert digest_result["content_json"]["notify_decision"]["should_notify"] is True


def test_jobs_digest_v2_skips_notify_on_empty_shortlist(monkeypatch) -> None:
    monkeypatch.setattr(
        jobs_digest_v2,
        "fetch_upstream_result_content_json",
        lambda db, upstream: {
            "artifact_type": "jobs.shortlist.v1",
            "shortlist": [],
            "action_seed": {},
        },
    )
    payload = {
        "pipeline_id": "pipe-empty",
        "upstream": {"task_id": "a", "run_id": "b", "task_type": "jobs_shortlist_v1"},
        "request": {"notify_on_empty": False},
        "digest_policy": {"notify_on_empty": False},
    }
    result = jobs_digest_v2.execute(_task(payload), db=object())

    assert result["content_json"]["notify_decision"]["should_notify"] is False
    assert result["next_tasks"] == []
