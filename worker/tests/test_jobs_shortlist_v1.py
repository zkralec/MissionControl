import json
import os
import sys
from types import SimpleNamespace

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "worker"))

from task_handlers import jobs_shortlist_v1


def _task(payload: dict, *, task_id: str = "task-shortlist-1", run_id: str = "run-shortlist-1") -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        max_attempts=3,
        _run_id=run_id,
        payload_json=json.dumps(payload, separators=(",", ":"), ensure_ascii=True),
    )


def test_jobs_shortlist_v1_consumes_jobs_scored_from_rank_artifact(monkeypatch) -> None:
    monkeypatch.setattr(
        jobs_shortlist_v1,
        "fetch_upstream_result_content_json",
        lambda db, upstream: {
            "artifact_type": "jobs.rank.v1",
            "pipeline_counts": {
                "collected_count": 40,
                "normalized_count": 22,
                "deduped_count": 18,
                "duplicates_collapsed": 4,
                "scored_count": 12,
            },
            "jobs_scored_artifact": {
                "artifact_type": "jobs_scored.v1",
                "jobs_scored": [
                    {
                        "job_id": "j1",
                        "title": "Senior ML Engineer",
                        "company": "Acme",
                        "source": "linkedin",
                        "overall_score": 94,
                        "score": 1.88,
                        "duplicate_group_id": "dup-1",
                        "explanation_summary": "Top fit.",
                    },
                    {
                        "job_id": "j2",
                        "title": "Senior ML Engineer II",
                        "company": "Acme",
                        "source": "indeed",
                        "overall_score": 93,
                        "score": 1.86,
                        "duplicate_group_id": "dup-2",
                        "explanation_summary": "Strong fit.",
                    },
                    {
                        "job_id": "j3",
                        "title": "Machine Learning Engineer",
                        "company": "Beta Labs",
                        "source": "glassdoor",
                        "overall_score": 90,
                        "score": 1.8,
                        "duplicate_group_id": "dup-3",
                        "explanation_summary": "Good fit.",
                    },
                    {
                        "job_id": "j4",
                        "title": "Machine Learning Engineer",
                        "company": "Beta Labs",
                        "source": "glassdoor",
                        "overall_score": 89,
                        "score": 1.78,
                        "duplicate_group_id": "dup-4",
                        "explanation_summary": "Similar role.",
                    },
                ],
            },
            "ranked_jobs": [],
        },
    )

    payload = {
        "pipeline_id": "pipe-short-1",
        "upstream": {"task_id": "rank-task", "run_id": "rank-run", "task_type": "jobs_rank_v1"},
        "request": {"shortlist_max_items": 3, "shortlist_min_score": 0.1},
        "shortlist_policy": {"max_items": 3, "per_company_cap": 1, "per_source_cap": 3},
    }
    result = jobs_shortlist_v1.execute(_task(payload), db=object())
    artifact = result["content_json"]

    assert artifact["artifact_type"] == "jobs.shortlist.v1"
    assert artifact["jobs_top_artifact"]["artifact_type"] == "jobs_top.v1"
    assert artifact["shortlist_count"] == 2
    companies = [row.get("company") for row in artifact["shortlist"]]
    assert sorted(companies) == ["Acme", "Beta Labs"]
    assert artifact["shortlist_summary_metadata"]["upstream_artifact_type"] == "jobs.rank.v1"
    assert artifact["pipeline_counts"]["collected_count"] == 40
    assert artifact["jobs_top_artifact"]["pipeline_counts"]["deduped_count"] == 18
    assert isinstance(artifact["notification_candidates"], list)
    assert result["next_tasks"][0]["task_type"] == "jobs_digest_v2"


def test_jobs_shortlist_v1_accepts_direct_jobs_scored_artifact(monkeypatch) -> None:
    monkeypatch.setattr(
        jobs_shortlist_v1,
        "fetch_upstream_result_content_json",
        lambda db, upstream: {
            "artifact_type": "jobs_scored.v1",
            "jobs_scored": [
                {
                    "job_id": "j1",
                    "title": "AI Engineer",
                    "company": "Acme",
                    "source": "linkedin",
                    "overall_score": 92,
                    "score": 1.84,
                }
            ],
        },
    )

    payload = {
        "pipeline_id": "pipe-short-2",
        "upstream": {"task_id": "rank-task", "run_id": "rank-run", "task_type": "jobs_rank_v1"},
        "request": {"shortlist_max_items": 5, "shortlist_min_score": 0.1},
    }
    result = jobs_shortlist_v1.execute(_task(payload), db=object())
    artifact = result["content_json"]

    assert artifact["shortlist_count"] == 1
    assert artifact["shortlist_summary_metadata"]["upstream_artifact_type"] == "jobs_scored.v1"


def test_jobs_shortlist_v1_freshness_weighting_promotes_recent_jobs(monkeypatch) -> None:
    monkeypatch.setattr(
        jobs_shortlist_v1,
        "fetch_upstream_result_content_json",
        lambda db, upstream: {
            "artifact_type": "jobs_scored.v1",
            "jobs_scored": [
                {
                    "job_id": "old-high",
                    "title": "ML Engineer",
                    "company": "OldCorp",
                    "source": "linkedin",
                    "overall_score": 90,
                    "score": 1.8,
                    "posted_at": "2024-01-01T00:00:00Z",
                },
                {
                    "job_id": "new-slightly-lower",
                    "title": "ML Engineer",
                    "company": "NewCorp",
                    "source": "indeed",
                    "overall_score": 87,
                    "score": 1.74,
                    "posted_at": "2026-03-10T00:00:00Z",
                },
            ],
        },
    )

    payload = {
        "pipeline_id": "pipe-short-3",
        "upstream": {"task_id": "rank-task", "run_id": "rank-run", "task_type": "jobs_rank_v1"},
        "request": {"shortlist_max_items": 1, "shortlist_min_score": 0.1},
        "shortlist_policy": {"max_items": 1, "freshness_weight_enabled": True, "freshness_max_bonus": 20},
    }
    result = jobs_shortlist_v1.execute(_task(payload), db=object())
    top = result["content_json"]["shortlist"][0]

    assert top["job_id"] == "new-slightly-lower"


def test_jobs_shortlist_v1_empty_input_keeps_artifact_shape(monkeypatch) -> None:
    monkeypatch.setattr(
        jobs_shortlist_v1,
        "fetch_upstream_result_content_json",
        lambda db, upstream: {
            "artifact_type": "jobs_scored.v1",
            "jobs_scored": [],
            "pipeline_counts": {"collected_count": 0, "normalized_count": 0, "deduped_count": 0, "scored_count": 0},
        },
    )

    payload = {
        "pipeline_id": "pipe-short-empty",
        "upstream": {"task_id": "rank-task", "run_id": "rank-run", "task_type": "jobs_rank_v1"},
        "request": {"shortlist_max_items": 5, "shortlist_min_score": 0.1},
    }
    result = jobs_shortlist_v1.execute(_task(payload), db=object())
    artifact = result["content_json"]

    assert artifact["artifact_type"] == "jobs.shortlist.v1"
    assert artifact["shortlist_count"] == 0
    assert artifact["shortlist"] == []
    assert artifact["jobs_top_artifact"]["top_jobs"] == []
    assert artifact["pipeline_counts"]["shortlisted_count"] == 0
    assert result["next_tasks"][0]["task_type"] == "jobs_digest_v2"


def test_jobs_shortlist_v1_duplicate_heavy_fixture_prefers_diversity(monkeypatch, jobs_v2_samples) -> None:
    scored_rows = [dict(row) for row in jobs_v2_samples["shortlist_duplicate_heavy_scored_jobs"]]
    monkeypatch.setattr(
        jobs_shortlist_v1,
        "fetch_upstream_result_content_json",
        lambda db, upstream: {
            "artifact_type": "jobs_scored.v1",
            "jobs_scored": scored_rows,
            "pipeline_counts": {"collected_count": 30, "normalized_count": 20, "deduped_count": 16, "scored_count": len(scored_rows)},
        },
    )

    payload = {
        "pipeline_id": "pipe-short-dup-heavy",
        "upstream": {"task_id": "rank-task", "run_id": "rank-run", "task_type": "jobs_rank_v1"},
        "request": {"shortlist_max_items": 3, "shortlist_min_score": 0.1},
        "shortlist_policy": {"max_items": 3, "per_company_cap": 1, "per_source_cap": 2},
    }
    result = jobs_shortlist_v1.execute(_task(payload), db=object())
    artifact = result["content_json"]

    assert artifact["shortlist_count"] == 2
    companies = sorted(str(row.get("company")) for row in artifact["shortlist"])
    assert companies == ["Acme AI", "Beta Labs"]
    assert artifact["anti_repetition_summary"]["rejected_summary"]["per_company_cap"] >= 1
