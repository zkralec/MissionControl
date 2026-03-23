import json
import os
import sys
from types import SimpleNamespace

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "worker"))

from task_handlers import jobs_collect_v1
from task_handlers.jobs_pipeline_common import resolve_request


def _task(payload: dict, *, task_id: str = "task-collect-1", run_id: str = "run-collect-1") -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        max_attempts=3,
        _run_id=run_id,
        payload_json=json.dumps(payload, separators=(",", ":"), ensure_ascii=True),
    )


class _FailCollector:
    SUPPORTED_FIELDS = {"source": "linkedin"}

    @staticmethod
    def collect_jobs(request: dict, *, url_override: str | None = None) -> dict:
        del request, url_override
        return {
            "status": "failed",
            "jobs": [],
            "warnings": [],
            "errors": ["fetch_failed"],
            "meta": {"requested_limit": 5},
        }


class _SuccessCollector:
    SUPPORTED_FIELDS = {"source": "indeed"}

    @staticmethod
    def collect_jobs(request: dict, *, url_override: str | None = None) -> dict:
        del request
        return {
            "status": "success",
            "jobs": [
                {
                    "source": "indeed",
                    "source_url": url_override or "https://www.indeed.com/jobs?q=ml+engineer",
                    "title": "Machine Learning Engineer",
                    "company": "Acme",
                    "location": "Remote",
                    "url": "https://www.indeed.com/viewjob?jk=123",
                    "source_metadata": {"search_url": "https://www.indeed.com/jobs?q=ml+engineer"},
                }
            ],
            "warnings": [],
            "errors": [],
            "meta": {"requested_limit": 5, "returned_count": 1},
        }


def test_resolve_request_preserves_structured_collect_inputs() -> None:
    request = resolve_request(
        {
            "titles": ["ML Engineer"],
            "keywords": ["python", "llm"],
            "excluded_keywords": ["senior"],
            "locations": ["Remote", "New York, NY"],
            "work_mode_preference": ["remote", "hybrid"],
            "minimum_salary": 150000,
            "experience_level": "entry-level",
            "result_limit_per_source": 450,
            "max_total_jobs": 1200,
            "max_pages_per_source": 7,
            "max_queries_per_title_location_pair": 6,
            "max_queries_per_run": 14,
            "enable_query_expansion": False,
            "jobs_notification_cooldown_days": 5,
            "jobs_shortlist_repeat_penalty": 6,
            "resurface_seen_jobs": True,
            "early_stop_when_no_new_results": False,
            "enabled_sources": ["linkedin", "indeed", "glassdoor", "handshake"],
            "shortlist_count": 6,
            "shortlist_freshness_preference": "strong-prefer-recent",
        }
    )

    assert request["titles"] == ["ML Engineer"]
    assert request["keywords"] == ["python", "llm"]
    assert request["excluded_keywords"] == ["senior"]
    assert request["locations"] == ["Remote", "New York, NY"]
    assert request["work_mode_preference"] == ["remote", "hybrid"]
    assert request["minimum_salary"] == 150000.0
    assert request["experience_level"] == "entry"
    assert request["result_limit_per_source"] == 450
    assert request["max_jobs_per_source"] == 450
    assert request["max_jobs_per_board"] == 450
    assert request["max_total_jobs"] == 1200
    assert request["max_pages_per_source"] == 7
    assert request["max_queries_per_title_location_pair"] == 6
    assert request["max_queries_per_run"] == 14
    assert request["enable_query_expansion"] is False
    assert request["jobs_notification_cooldown_days"] == 5
    assert request["jobs_shortlist_repeat_penalty"] == 6.0
    assert request["resurface_seen_jobs"] is True
    assert request["early_stop_when_no_new_results"] is False
    assert request["sources"] == ["linkedin", "indeed", "glassdoor", "handshake"]
    assert request["enabled_sources"] == ["linkedin", "indeed", "glassdoor", "handshake"]
    assert request["shortlist_max_items"] == 6
    assert request["shortlist_freshness_preference"] == "strong_prefer_recent"
    assert request["shortlist_freshness_weight_enabled"] is True
    assert request["shortlist_freshness_max_bonus"] == 12.0


def test_jobs_collect_v1_reports_partial_success_when_one_source_fails(monkeypatch) -> None:
    def _fake_load(source: str):
        if source == "linkedin":
            return _FailCollector
        if source == "indeed":
            return _SuccessCollector
        raise AssertionError(f"unexpected source requested: {source}")

    monkeypatch.setattr(jobs_collect_v1, "_load_collector_module", _fake_load)

    payload = {
        "pipeline_id": "pipe-partial",
        "request": {
            "collectors_enabled": True,
            "sources": ["linkedin", "indeed"],
            "titles": ["Machine Learning Engineer"],
            "locations": ["Remote"],
            "result_limit_per_source": 5,
        },
    }
    result = jobs_collect_v1.execute(_task(payload), db=None)
    artifact = result["content_json"]

    assert result["artifact_type"] == "jobs.collect.v1"
    assert artifact["artifact_type"] == "jobs.collect.v1"
    assert artifact["artifact_schema"] == "jobs_raw.v1"
    assert artifact["partial_success"] is True
    assert artifact["source_results"]["linkedin"]["status"] == "failed"
    assert artifact["source_results"]["indeed"]["status"] == "success"
    assert artifact["failed_sources"] == ["linkedin"]
    assert artifact["successful_sources"] == ["indeed"]
    assert len(artifact["raw_jobs"]) == 1
    assert artifact["raw_jobs"][0]["source"] == "indeed"
    assert artifact["raw_jobs"][0]["source_url"] == "https://www.indeed.com/jobs?q=ml+engineer"


def test_jobs_collect_v1_raises_when_all_enabled_sources_fail(monkeypatch) -> None:
    monkeypatch.setattr(jobs_collect_v1, "_load_collector_module", lambda source: _FailCollector)

    payload = {
        "pipeline_id": "pipe-fail",
        "request": {
            "collectors_enabled": True,
            "sources": ["linkedin", "indeed"],
            "titles": ["Machine Learning Engineer"],
            "result_limit_per_source": 5,
        },
    }

    with pytest.raises(RuntimeError, match="all enabled sources failed"):
        jobs_collect_v1.execute(_task(payload), db=None)


def test_jobs_collect_v1_success_multisource_fixture(monkeypatch, jobs_v2_samples) -> None:
    source_jobs = jobs_v2_samples["collect_multisource_by_source"]

    def _collector_for(source: str):
        jobs = [dict(row) for row in source_jobs[source]]

        class _Collector:
            SUPPORTED_FIELDS = {
                "source": source,
                "titles": True,
                "keywords": True,
                "excluded_keywords": True,
                "locations": True,
                "work_mode_preference": True,
                "minimum_salary": True,
                "experience_level": True,
                "result_limit_per_source": True,
                "enabled_sources": True,
                "source_metadata_fields": ["source_metadata"],
            }

            @staticmethod
            def collect_jobs(request: dict, *, url_override: str | None = None) -> dict:
                output: list[dict] = []
                for row in jobs:
                    item = dict(row)
                    if url_override:
                        item["source_url"] = url_override
                    output.append(item)
                return {
                    "status": "success",
                    "jobs": output,
                    "warnings": [],
                    "errors": [],
                    "meta": {
                        "requested_limit": int(request.get("result_limit_per_source") or 25),
                        "returned_count": len(output),
                    },
                }

        return _Collector

    def _fake_load(source: str):
        return _collector_for(source)

    monkeypatch.setattr(jobs_collect_v1, "_load_collector_module", _fake_load)

    payload = {
        "pipeline_id": "pipe-success-all",
        "request": {
            "collectors_enabled": True,
            "sources": ["linkedin", "indeed", "glassdoor", "handshake"],
            "titles": ["Machine Learning Engineer"],
            "locations": ["Remote", "New York, NY"],
            "result_limit_per_source": 10,
        },
    }
    result = jobs_collect_v1.execute(_task(payload), db=None)
    artifact = result["content_json"]

    expected_count = sum(len(rows) for rows in source_jobs.values())
    assert artifact["artifact_type"] == "jobs.collect.v1"
    assert artifact["artifact_schema"] == "jobs_raw.v1"
    assert artifact["collection_status"] == "success"
    assert artifact["partial_success"] is False
    assert len(artifact["raw_jobs"]) == expected_count
    assert artifact["collection_summary"]["discovered_raw_count"] == expected_count
    assert artifact["collection_summary"]["kept_after_basic_filter_count"] == expected_count
    assert artifact["collection_summary"]["dropped_by_basic_filter_count"] == 0
    assert artifact["collection_summary"]["deduped_count"] == 0
    assert sorted(artifact["successful_sources"]) == ["glassdoor", "handshake", "indeed", "linkedin"]
    assert artifact["failed_sources"] == []
    assert artifact["collector_errors"] == []
    assert result["next_tasks"][0]["task_type"] == "jobs_normalize_v1"
    assert all(str(job.get("source", "")).strip() for job in artifact["raw_jobs"])
    assert all(str(job.get("source_url", "")).strip() for job in artifact["raw_jobs"])
    for source in ["linkedin", "indeed", "glassdoor", "handshake"]:
        assert artifact["source_results"][source]["status"] == "success"
        assert source in artifact["supported_fields_by_source"]


def test_jobs_collect_v1_empty_success_when_sources_return_no_jobs(monkeypatch) -> None:
    class _EmptyCollector:
        SUPPORTED_FIELDS = {"source": "empty"}

        @staticmethod
        def collect_jobs(request: dict, *, url_override: str | None = None) -> dict:
            del request, url_override
            return {"status": "success", "jobs": [], "warnings": [], "errors": [], "meta": {"returned_count": 0}}

    monkeypatch.setattr(jobs_collect_v1, "_load_collector_module", lambda source: _EmptyCollector)

    payload = {
        "pipeline_id": "pipe-empty-success",
        "request": {
            "collectors_enabled": True,
            "sources": ["linkedin", "indeed"],
            "titles": ["ML Engineer"],
            "result_limit_per_source": 5,
        },
    }
    result = jobs_collect_v1.execute(_task(payload), db=None)
    artifact = result["content_json"]

    assert artifact["collection_status"] == "success"
    assert artifact["partial_success"] is False
    assert artifact["raw_jobs"] == []
    assert sorted(artifact["successful_sources"]) == ["indeed", "linkedin"]
    assert artifact["failed_sources"] == []
    assert artifact["collection_summary"]["raw_job_count"] == 0
    assert artifact["collection_summary"]["discovered_raw_count"] == 0
    assert artifact["collection_summary"]["kept_after_basic_filter_count"] == 0
    assert artifact["collection_summary"]["dropped_by_basic_filter_count"] == 0
    assert artifact["collection_summary"]["deduped_count"] == 0
    assert result["next_tasks"][0]["task_type"] == "jobs_normalize_v1"


def test_jobs_collect_v1_surfaces_source_metadata_quality(monkeypatch) -> None:
    class _MetadataCollector:
        SUPPORTED_FIELDS = {"source": "indeed"}

        @staticmethod
        def collect_jobs(request: dict, *, url_override: str | None = None) -> dict:
            del request, url_override
            return {
                "status": "success",
                "jobs": [
                    {
                        "source": "indeed",
                        "source_url": "https://www.indeed.com/viewjob?jk=123",
                        "title": "Senior Software Engineer",
                        "company": None,
                        "location": "Remote",
                        "url": "https://www.indeed.com/viewjob?jk=123",
                        "metadata_diagnostics": {
                            "missing_company": True,
                            "missing_posted_at": True,
                            "missing_source_url": False,
                            "missing_location": False,
                        },
                        "source_metadata": {"search_url": "https://www.indeed.com/jobs?q=senior+software+engineer"},
                    }
                ],
                "warnings": [],
                "errors": [],
                "meta": {
                    "returned_count": 1,
                    "metadata_completeness_summary": {
                        "job_count": 1,
                        "missing_company": 1,
                        "missing_posted_at": 1,
                        "missing_source_url": 0,
                        "missing_location": 0,
                    },
                },
            }

    monkeypatch.setattr(jobs_collect_v1, "_load_collector_module", lambda source: _MetadataCollector)

    payload = {
        "pipeline_id": "pipe-metadata-quality",
        "request": {
            "collectors_enabled": True,
            "sources": ["indeed"],
            "titles": ["Senior Software Engineer"],
            "result_limit_per_source": 5,
        },
    }
    result = jobs_collect_v1.execute(_task(payload), db=None)
    artifact = result["content_json"]

    assert artifact["source_metadata_quality"]["indeed"]["missing_company"] == 1
    assert artifact["source_metadata_quality"]["indeed"]["missing_posted_at"] == 1
    assert artifact["metadata_completeness_summary"]["missing_company"] == 1
    assert artifact["collection_summary"]["missing_company"] == 1
    assert artifact["collection_summary"]["missing_posted_at"] == 1
    observability = artifact["collection_observability"]
    assert observability["waterfall"]["raw_jobs_discovered"] == 1
    assert observability["by_source"]["indeed"]["raw_jobs_discovered"] == 1
    assert observability["by_source"]["indeed"]["kept_after_basic_filter"] == 1
    assert observability["by_source"]["indeed"]["jobs_dropped"] == 0
    assert observability["by_source"]["indeed"]["missing_rates"]["missing_company_rate"] == 100.0
    assert "Weakest metadata source: indeed." in observability["operator_questions"]["which_source_is_weak"]


def test_jobs_collect_v1_aggregates_query_observability_and_run_cap(monkeypatch) -> None:
    requested_limits: dict[str, int] = {}

    def _collector_for(source: str):
        class _Collector:
            SUPPORTED_FIELDS = {"source": source}

            @staticmethod
            def collect_jobs(request: dict, *, url_override: str | None = None) -> dict:
                del url_override
                requested_limits[source] = int(request.get("result_limit_per_source") or 0)
                limit = requested_limits[source]
                jobs = [
                    {
                        "source": source,
                        "source_url": f"https://example.test/{source}/{index}",
                        "title": f"Software Engineer {source} {index}",
                        "company": f"{source.title()} Corp",
                        "location": "Remote",
                        "url": f"https://example.test/{source}/{index}",
                        "source_metadata": {"search_url": f"https://example.test/{source}/search"},
                    }
                    for index in range(limit)
                ]
                return {
                    "status": "success",
                    "jobs": jobs,
                    "warnings": [],
                    "errors": [],
                    "meta": {
                        "requested_limit": limit,
                        "returned_count": len(jobs),
                        "discovered_raw_count": len(jobs),
                        "kept_after_basic_filter_count": len(jobs),
                        "dropped_by_basic_filter_count": 0,
                        "deduped_count": 0,
                        "queries_attempted": [f"{source} base", f"{source} expansion"],
                        "queries_executed_count": 2,
                        "empty_queries_count": 1 if source == "indeed" else 0,
                        "query_examples": [f"{source} base", f"{source} expansion"],
                        "search_attempts": [
                            {
                                "query": f"{source} base",
                                "location": "Remote",
                                "expansion_type": "base_title",
                                "jobs_found": max(limit - 1, 0),
                                "new_unique_jobs": max(limit - 1, 0),
                                "returned_count": max(limit - 1, 0),
                                "stop_reason": "max_pages_reached",
                            },
                            {
                                "query": f"{source} expansion",
                                "location": "Remote",
                                "expansion_type": "title_synonym",
                                "jobs_found": 1 if limit else 0,
                                "new_unique_jobs": 1 if limit else 0,
                                "returned_count": 1 if limit else 0,
                                "stop_reason": "max_pages_reached",
                            },
                        ],
                    },
                }

        return _Collector

    monkeypatch.setattr(jobs_collect_v1, "_load_collector_module", _collector_for)

    payload = {
        "pipeline_id": "pipe-query-observability",
        "request": {
            "collectors_enabled": True,
            "sources": ["linkedin", "indeed", "glassdoor"],
            "titles": ["Software Engineer"],
            "locations": ["Remote"],
            "result_limit_per_source": 3,
            "max_total_jobs": 4,
            "max_queries_per_run": 6,
            "enable_query_expansion": True,
        },
    }
    result = jobs_collect_v1.execute(_task(payload), db=None)
    artifact = result["content_json"]

    assert requested_limits == {"linkedin": 3, "indeed": 1}
    assert artifact["skipped_sources"] == ["glassdoor"]
    assert len(artifact["raw_jobs"]) == 4
    assert artifact["collection_summary"]["max_total_jobs"] == 4
    assert artifact["collection_summary"]["truncated_by_run_limit_count"] == 0
    assert artifact["collection_summary"]["queries_executed_count"] == 4
    assert artifact["collection_summary"]["empty_queries_count"] == 1
    assert artifact["collection_summary"]["query_examples"] == [
        "linkedin base",
        "linkedin expansion",
        "indeed base",
        "indeed expansion",
    ]
    observability = artifact["collection_observability"]
    assert observability["query_summary"]["queries_executed"] == 4
    assert observability["query_summary"]["empty_queries_count"] == 1
    assert observability["query_summary"]["max_total_jobs"] == 4
    assert observability["query_summary"]["query_examples"] == [
        "linkedin base",
        "linkedin expansion",
        "indeed base",
        "indeed expansion",
    ]
    assert observability["query_summary"]["query_runs"][0]["source"] == "linkedin"
    assert observability["by_source"]["linkedin"]["queries_executed_count"] == 2
    assert observability["by_source"]["indeed"]["queries_executed_count"] == 2
    assert observability["by_source"]["indeed"]["jobs_found_per_source"] == 1
    assert artifact["source_results"]["glassdoor"]["meta"]["reason"] == "max_total_jobs_reached"
