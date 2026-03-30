from __future__ import annotations

from types import SimpleNamespace

from task_handlers import openclaw_jobs_collect_v1


def _task(payload: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(id="task-openclaw", _run_id="run-openclaw", payload_json=__import__("json").dumps(payload))


def test_openclaw_jobs_collect_v1_collects_and_preserves_jobs_collect_contract(monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_ENABLED", "true")
    monkeypatch.setenv("OPENCLAW_COLLECTOR_COMMAND", "/bin/true")

    def _fake_collect(source, request, *, url_override=None):
        del request, url_override
        if source == "handshake":
            return {
                "status": "success",
                "jobs": [
                    {
                        "source": "handshake",
                        "source_url": "https://joinhandshake.com/jobs",
                        "title": "ML Engineer",
                        "company": "Acme AI",
                        "location": "Remote",
                        "url": "https://joinhandshake.com/jobs/123",
                        "posted_at": "2 days ago",
                        "description_snippet": "Build production AI systems.",
                        "source_metadata": {},
                    }
                ],
                "warnings": [],
                "errors": [],
                "meta": {
                    "source_status": "success",
                    "requested_limit": 25,
                    "discovered_raw_count": 1,
                    "kept_after_basic_filter_count": 1,
                    "dropped_by_basic_filter_count": 0,
                    "deduped_count": 0,
                    "pages_attempted": 2,
                    "pages_fetched": 2,
                    "queries_executed_count": 1,
                    "empty_queries_count": 0,
                    "query_examples": ["machine learning engineer"],
                    "request_urls_tried": ["https://joinhandshake.com/jobs"],
                    "jobs_raw": 1,
                    "jobs_kept": 1,
                    "screenshot_count": 1,
                    "screenshot_references": [
                        {
                            "source": "handshake",
                            "path": "/tmp/openclaw/handshake-1.png",
                            "label": "listing page",
                        }
                    ],
                    "metadata_completeness_summary": {
                        "job_count": 1,
                        "missing_company": 0,
                        "missing_posted_at": 0,
                        "missing_source_url": 0,
                        "missing_location": 0,
                    },
                },
            }
        return {
            "status": "auth_blocked",
            "jobs": [],
            "warnings": ["login required"],
            "errors": ["auth blocked"],
            "meta": {
                "source_status": "auth_blocked",
                "source_error_type": "login_wall",
                "requested_limit": 25,
                "discovered_raw_count": 0,
                "kept_after_basic_filter_count": 0,
                "dropped_by_basic_filter_count": 0,
                "deduped_count": 0,
                "pages_attempted": 1,
                "pages_fetched": 1,
                "queries_executed_count": 1,
                "empty_queries_count": 1,
                "query_examples": ["machine learning engineer"],
                "request_urls_tried": ["https://glassdoor.com/Jobs"],
                "auth_required_detected": True,
                "login_wall_detected": True,
                "screenshot_references": [
                    {
                        "source": "glassdoor",
                        "path": "/tmp/openclaw/glassdoor-login.png",
                        "label": "login wall",
                    }
                ],
            },
        }

    monkeypatch.setattr(openclaw_jobs_collect_v1, "collect_openclaw_source_jobs", _fake_collect)

    payload = {
        "request": {
            "query": "machine learning engineer",
            "locations": ["Remote"],
            "sources": ["handshake", "glassdoor"],
            "openclaw_enabled": True,
        }
    }
    result = openclaw_jobs_collect_v1.execute(_task(payload), db=None)

    artifact = result["content_json"]
    assert artifact["artifact_type"] == "jobs.collect.v1"
    assert artifact["source_results"]["handshake"]["status"] == "success"
    assert artifact["source_results"]["glassdoor"]["status"] == "auth_blocked"
    assert artifact["collection_summary"]["collection_method"] == "openclaw"
    assert artifact["screenshot_references"][0]["source"] == "handshake"
    assert artifact["collection_observability"]["by_source"]["handshake"]["screenshot_count"] == 1
    assert artifact["collection_observability"]["active_sources_label"] == "Handshake + Glassdoor active"
    assert result["next_tasks"][0]["task_type"] == "jobs_normalize_v1"


def test_openclaw_jobs_collect_v1_skips_when_feature_gate_is_disabled(monkeypatch) -> None:
    monkeypatch.delenv("OPENCLAW_ENABLED", raising=False)
    monkeypatch.delenv("OPENCLAW_COLLECTOR_COMMAND", raising=False)

    payload = {
        "request": {
            "query": "machine learning engineer",
            "sources": ["handshake"],
        }
    }
    result = openclaw_jobs_collect_v1.execute(_task(payload), db=None)

    artifact = result["content_json"]
    assert artifact["source_results"]["handshake"]["status"] == "skipped"
    assert artifact["collection_summary"]["openclaw_enabled"] is False
    assert any("OpenClaw collection is disabled" in warning for warning in artifact["warnings"])
    assert result["next_tasks"][0]["task_type"] == "jobs_normalize_v1"
