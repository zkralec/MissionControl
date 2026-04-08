from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from task_handlers.errors import NonRetryableTaskError
from task_handlers.jobs_pipeline_common import (
    build_upstream_ref,
    new_pipeline_id,
    payload_object,
    utc_iso,
)


def _normalize_url(raw_url: Any) -> str:
    if not isinstance(raw_url, str):
        return ""
    value = raw_url.strip()
    if not value:
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return value.lower()
    query_items = [
        (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if key and not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}
    ]
    return urlunsplit(
        (
            (parts.scheme or "https").lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/") or "/",
            urlencode(sorted(query_items)),
            "",
        )
    )


def _application_idempotency_key(job: dict[str, Any]) -> str:
    company = str(job.get("company") or "").strip().lower()
    job_url = _normalize_url(job.get("application_url") or job.get("source_url") or job.get("url"))
    job_id = str(job.get("job_id") or job.get("normalized_job_id") or "").strip().lower()
    if not company or not job_url:
        raise NonRetryableTaskError("job_apply_manual_seed_v1 requires both company and application_url/source_url")
    base = f"{job_url}|{company}|{job_id or '-'}"
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]
    company_slug = "".join(ch if ch.isalnum() else "-" for ch in company)[:32].strip("-") or "company"
    return f"jobapply:{company_slug}:{digest}"


def _manual_job(payload: dict[str, Any]) -> dict[str, Any]:
    manual_job = payload.get("manual_job") if isinstance(payload.get("manual_job"), dict) else {}
    title = str(manual_job.get("title") or "").strip()
    company = str(manual_job.get("company") or "").strip()
    source = str(manual_job.get("source") or "").strip().lower()
    source_url = str(manual_job.get("source_url") or "").strip()
    application_url = str(manual_job.get("application_url") or "").strip()
    job_id = str(manual_job.get("job_id") or "").strip()
    normalized_job_id = str(manual_job.get("normalized_job_id") or "").strip()

    if not title:
        raise NonRetryableTaskError("job_apply_manual_seed_v1 requires manual_job.title")
    if not company:
        raise NonRetryableTaskError("job_apply_manual_seed_v1 requires manual_job.company")
    if not source:
        raise NonRetryableTaskError("job_apply_manual_seed_v1 requires manual_job.source")
    if not source_url:
        raise NonRetryableTaskError("job_apply_manual_seed_v1 requires manual_job.source_url")
    if not application_url:
        raise NonRetryableTaskError("job_apply_manual_seed_v1 requires manual_job.application_url")

    selected_job = dict(manual_job)
    selected_job["title"] = title
    selected_job["company"] = company
    selected_job["source"] = source
    selected_job["source_url"] = source_url
    selected_job["application_url"] = application_url
    selected_job["url"] = application_url
    if job_id:
        selected_job["job_id"] = job_id
    if normalized_job_id:
        selected_job["normalized_job_id"] = normalized_job_id
    elif job_id:
        selected_job["normalized_job_id"] = job_id
    return selected_job


def execute(task: Any, db: Any) -> dict[str, Any]:
    del db
    payload = payload_object(task.payload_json)
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    prepare_policy = payload.get("prepare_policy") if isinstance(payload.get("prepare_policy"), dict) else {}
    pipeline_id = new_pipeline_id(payload.get("pipeline_id"))
    manual_job = _manual_job(payload)

    lineage = payload.get("lineage") if isinstance(payload.get("lineage"), dict) else {}
    manual_lineage = {
        **lineage,
        "source": "manual_api",
        "entrypoint": "manual_api",
        "seed_kind": "manual_seed",
        "path": "manual_api/manual_seed",
    }

    artifact = {
        "artifact_type": "job.apply.manual_seed.v1",
        "artifact_schema": "job.apply.manual_seed.v1",
        "pipeline_id": pipeline_id,
        "seeded_at": utc_iso(),
        "request": request,
        "prepare_policy": {
            "include_cover_letter": bool(prepare_policy.get("include_cover_letter", True)),
            "enqueue_openclaw_apply": bool(prepare_policy.get("enqueue_openclaw_apply", True)),
        },
        "selected_job": manual_job,
        "manual_job": manual_job,
        "lineage": manual_lineage,
        "application_identity_preview": {
            "idempotency_key": _application_idempotency_key(manual_job),
            "job_id": manual_job.get("job_id") or manual_job.get("normalized_job_id"),
            "application_url": manual_job.get("application_url") or manual_job.get("url"),
            "company": manual_job.get("company"),
        },
    }

    next_payload = {
        "pipeline_id": pipeline_id,
        "upstream": build_upstream_ref(task, "job_apply_manual_seed_v1"),
        "request": request,
        "selected_job": manual_job,
        "prepare_policy": {
            "include_cover_letter": bool(prepare_policy.get("include_cover_letter", True)),
            "enqueue_openclaw_apply": bool(prepare_policy.get("enqueue_openclaw_apply", True)),
        },
        "lineage": manual_lineage,
    }
    next_task = {
        "task_type": "job_apply_prepare_v1",
        "payload_json": next_payload,
        "idempotency_key": _application_idempotency_key(manual_job),
    }

    return {
        "artifact_type": "job.apply.manual_seed.v1",
        "content_json": artifact,
        "next_tasks": [next_task],
    }
