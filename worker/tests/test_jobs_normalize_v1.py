import json
import os
import sys
from types import SimpleNamespace

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "worker"))

from task_handlers import jobs_normalize_v1


def _task(payload: dict, *, task_id: str = "task-normalize-1", run_id: str = "run-normalize-1") -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        max_attempts=3,
        _run_id=run_id,
        payload_json=json.dumps(payload, separators=(",", ":"), ensure_ascii=True),
    )


def test_jobs_normalize_v1_enriches_and_dedupes_cross_source(monkeypatch) -> None:
    monkeypatch.setattr(
        jobs_normalize_v1,
        "fetch_upstream_result_content_json",
        lambda db, upstream: {
            "artifact_type": "jobs.collect.v1",
            "collection_summary": {
                "discovered_raw_count": 3,
                "kept_after_basic_filter_count": 3,
                "dropped_by_basic_filter_count": 0,
            },
            "collection_observability": {
                "by_source": {
                    "linkedin": {"raw_jobs_discovered": 1, "kept_after_basic_filter": 1, "jobs_dropped": 0},
                    "indeed": {"raw_jobs_discovered": 1, "kept_after_basic_filter": 1, "jobs_dropped": 0},
                    "glassdoor": {"raw_jobs_discovered": 1, "kept_after_basic_filter": 1, "jobs_dropped": 0},
                }
            },
            "raw_jobs": [
                {
                    "source": "linkedin",
                    "source_url": "https://www.linkedin.com/jobs/search/?keywords=software+engineer",
                    "title": "SENIOR SOFTWARE ENGINEER",
                    "company": "ACME, INC.",
                    "location": "New York, NY",
                    "description_snippet": "Hybrid role building ML systems. Compensation $140k - $170k annually.",
                    "url": "https://www.linkedin.com/jobs/view/111",
                },
                {
                    "source": "indeed",
                    "source_url": "https://www.indeed.com/jobs?q=software+engineer",
                    "title": "Sr Software Engineer",
                    "company": "Acme Inc",
                    "location": "New York City",
                    "salary_min": 145000,
                    "salary_max": 172000,
                    "description_snippet": "Senior engineer role for platform team.",
                    "url": "https://www.indeed.com/viewjob?jk=222",
                },
                {
                    "source": "glassdoor",
                    "source_url": "https://www.glassdoor.com/Job/jobs.htm?sc.keyword=data+engineer",
                    "title": "Data Engineer",
                    "company": "Beta Labs",
                    "location": "Remote",
                    "description_snippet": "Remote data platform role.",
                    "url": "https://www.glassdoor.com/partner/jobListing.htm?pos=101",
                },
            ],
            "warnings": [],
        },
    )

    payload = {
        "pipeline_id": "pipe-norm-1",
        "upstream": {"task_id": "collect-task", "run_id": "collect-run", "task_type": "jobs_collect_v1"},
        "request": {"query": "software engineer", "location": "United States"},
        "normalization_policy": {"fuzzy_matching": {"enabled": True, "threshold": 0.84, "ambiguous_threshold": 0.68}},
    }
    result = jobs_normalize_v1.execute(_task(payload), db=object())

    assert result["artifact_type"] == "jobs.normalize.v1"
    artifact = result["content_json"]
    assert artifact["artifact_type"] == "jobs.normalize.v1"
    assert artifact["counts"]["raw_count"] == 3
    assert artifact["counts"]["normalized_count"] == 3
    assert artifact["counts"]["deduped_count"] == 2
    assert artifact["counts"]["duplicates_collapsed"] == 1

    normalized_artifact = artifact["jobs_normalized_artifact"]
    deduped_artifact = artifact["jobs_deduped_artifact"]
    assert normalized_artifact["artifact_type"] == "jobs_normalized.v1"
    assert deduped_artifact["artifact_type"] == "jobs_deduped.v1"

    deduped_jobs = deduped_artifact["jobs"]
    acme = next(job for job in deduped_jobs if (job.get("company") or "").lower().startswith("acme"))
    assert acme["remote_type"] == "hybrid"
    assert acme["experience_level"] == "senior"
    assert acme["seniority"] == "senior"
    assert acme["salary_min"] <= acme["salary_max"]
    assert acme["source_url"].startswith("https://")
    assert acme["source_url_kind"] == "direct"
    assert acme["metadata_quality_score"] > 70
    assert sorted(acme["duplicate_sources"]) == ["indeed", "linkedin"]
    observability = artifact["normalization_observability"]
    assert observability["waterfall"]["raw_jobs_discovered"] == 3
    assert observability["waterfall"]["normalized_count"] == 3
    assert observability["waterfall"]["deduped_count"] == 2
    assert observability["by_source"]["linkedin"]["deduped_unique_groups"] == 1
    assert observability["by_source"]["indeed"]["dedupe_collapsed"] == 0
    assert "3 raw discovered, 3 kept after filtering, 2 unique after normalization." in observability["operator_questions"]["searched_enough"]
    assert result["next_tasks"][0]["task_type"] == "jobs_rank_v1"


def test_jobs_normalize_v1_applies_safe_enrichment_and_quality_flags(monkeypatch) -> None:
    monkeypatch.setattr(
        jobs_normalize_v1,
        "fetch_upstream_result_content_json",
        lambda db, upstream: {
            "artifact_type": "jobs.collect.v1",
            "raw_jobs": [
                {
                    "source": "indeed",
                    "source_url": "https://www.indeed.com/jobs?q=senior+software+engineer",
                    "title": "SENIOR SOFTWARE ENGINEER",
                    "company": "Unknown company",
                    "location": "new york city",
                    "description_snippet": "Remote senior role shipping backend systems.",
                    "url": "https://www.indeed.com/viewjob?jk=123",
                    "posted_at": "2 days ago",
                    "scraped_at": "2026-03-20T10:30:00Z",
                    "salary_text": "From $150k a year",
                }
            ],
            "warnings": [],
        },
    )

    payload = {
        "pipeline_id": "pipe-norm-quality",
        "upstream": {"task_id": "collect-task", "run_id": "collect-run", "task_type": "jobs_collect_v1"},
        "request": {"query": "senior software engineer", "location": "United States"},
    }
    result = jobs_normalize_v1.execute(_task(payload), db=object())

    job = result["content_json"]["normalized_jobs"][0]
    assert job["title"] == "Senior Software Engineer"
    assert job["company"] is None
    assert job["location"] == "New York, NY"
    assert job["remote_type"] == "remote"
    assert job["work_mode"] == "remote"
    assert job["experience_level"] == "senior"
    assert job["seniority"] == "senior"
    assert job["source_url"] == "https://www.indeed.com/viewjob?jk=123"
    assert job["source_url_kind"] == "direct"
    assert job["posted_at"] == "2026-03-18T10:30:00Z"
    assert job["posted_at_raw"] == "2 days ago"
    assert job["salary_min"] == 150000
    assert job["salary_max"] is None
    assert job["salary_text"] == "$150,000"
    assert job["missing_company"] is True
    assert job["missing_source_url"] is False
    assert job["missing_posted_at"] is False
    assert job["metadata_quality_score"] < 90


def test_jobs_normalize_v1_reports_ambiguous_duplicate_cases(monkeypatch) -> None:
    monkeypatch.setattr(
        jobs_normalize_v1,
        "fetch_upstream_result_content_json",
        lambda db, upstream: {
            "artifact_type": "jobs.collect.v1",
            "raw_jobs": [
                {
                    "source": "linkedin",
                    "title": "Machine Learning Platform Engineer",
                    "company": "Gamma AI",
                    "location": "Seattle, WA",
                },
                {
                    "source": "indeed",
                    "title": "Machine Learning Engineer",
                    "company": "Gamma AI",
                    "location": "Seattle, WA",
                },
            ],
            "warnings": [],
        },
    )

    payload = {
        "pipeline_id": "pipe-norm-2",
        "upstream": {"task_id": "collect-task", "run_id": "collect-run", "task_type": "jobs_collect_v1"},
        "request": {"query": "machine learning engineer", "location": "United States"},
        "normalization_policy": {"fuzzy_matching": {"enabled": True, "threshold": 0.9, "ambiguous_threshold": 0.7}},
    }
    result = jobs_normalize_v1.execute(_task(payload), db=object())
    artifact = result["content_json"]

    assert artifact["counts"]["raw_count"] == 2
    assert artifact["counts"]["deduped_count"] == 2
    assert artifact["counts"]["duplicates_collapsed"] == 0
    assert len(artifact["ambiguous_duplicate_cases"]) == 1


def test_jobs_normalize_v1_empty_raw_jobs_keeps_contract(monkeypatch) -> None:
    monkeypatch.setattr(
        jobs_normalize_v1,
        "fetch_upstream_result_content_json",
        lambda db, upstream: {
            "artifact_type": "jobs.collect.v1",
            "raw_jobs": [],
            "warnings": [],
        },
    )

    payload = {
        "pipeline_id": "pipe-norm-empty",
        "upstream": {"task_id": "collect-task", "run_id": "collect-run", "task_type": "jobs_collect_v1"},
        "request": {"query": "ml engineer"},
    }
    result = jobs_normalize_v1.execute(_task(payload), db=object())
    artifact = result["content_json"]

    assert artifact["artifact_type"] == "jobs.normalize.v1"
    assert artifact["counts"]["raw_count"] == 0
    assert artifact["counts"]["normalized_count"] == 0
    assert artifact["counts"]["deduped_count"] == 0
    assert artifact["jobs_normalized_artifact"]["jobs"] == []
    assert artifact["jobs_deduped_artifact"]["jobs"] == []
    assert artifact["normalized_jobs"] == []
    assert result["next_tasks"][0]["task_type"] == "jobs_rank_v1"


def test_jobs_normalize_v1_same_run_duplicates_still_collapse_with_canonical_key(monkeypatch) -> None:
    monkeypatch.setattr(
        jobs_normalize_v1,
        "fetch_upstream_result_content_json",
        lambda db, upstream: {
            "artifact_type": "jobs.collect.v1",
            "raw_jobs": [
                {
                    "source": "linkedin",
                    "title": "Software Engineer",
                    "company": "Acme",
                    "location": "Remote",
                    "url": "https://example.test/jobs/1",
                },
                {
                    "source": "indeed",
                    "title": "Software Engineer",
                    "company": "Acme",
                    "location": "Remote",
                    "url": "https://example.test/jobs/2",
                },
            ],
            "warnings": [],
        },
    )

    result = jobs_normalize_v1.execute(
        _task(
            {
                "pipeline_id": "pipe-norm-same-run-dup",
                "upstream": {"task_id": "collect-task", "run_id": "collect-run", "task_type": "jobs_collect_v1"},
                "request": {"query": "software engineer"},
            }
        ),
        db=object(),
    )

    artifact = result["content_json"]
    assert artifact["counts"]["deduped_count"] == 1
    assert artifact["counts"]["duplicates_collapsed"] == 1
    assert artifact["normalized_jobs"][0]["canonical_job_key"] == "job:acme|software engineer|remote"


def test_jobs_normalize_v1_duplicate_heavy_fixture(monkeypatch, jobs_v2_samples) -> None:
    monkeypatch.setattr(
        jobs_normalize_v1,
        "fetch_upstream_result_content_json",
        lambda db, upstream: {
            "artifact_type": "jobs.collect.v1",
            "raw_jobs": [dict(row) for row in jobs_v2_samples["normalize_duplicate_heavy_raw_jobs"]],
            "warnings": [],
        },
    )

    payload = {
        "pipeline_id": "pipe-norm-dup-heavy",
        "upstream": {"task_id": "collect-task", "run_id": "collect-run", "task_type": "jobs_collect_v1"},
        "request": {"query": "senior machine learning engineer"},
        "normalization_policy": {
            "fuzzy_matching": {"enabled": True, "threshold": 0.84, "ambiguous_threshold": 0.68},
        },
    }
    result = jobs_normalize_v1.execute(_task(payload), db=object())
    artifact = result["content_json"]

    assert artifact["counts"]["raw_count"] == 5
    assert artifact["counts"]["normalized_count"] == 5
    assert artifact["counts"]["deduped_count"] <= 5
    assert (
        artifact["counts"]["duplicates_collapsed"] >= 1
        or len(artifact["ambiguous_duplicate_cases"]) >= 1
    )
    assert artifact["jobs_deduped_artifact"]["duplicate_groups"]
    assert artifact["drop_reasons"]["duplicate"] == artifact["counts"]["duplicates_collapsed"]
