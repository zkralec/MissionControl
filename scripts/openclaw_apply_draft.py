#!/usr/bin/env python3
"""Starter OpenClaw apply-draft command for Mission Control.

Purpose:
- Reads a JSON request from stdin.
- Validates the minimum apply-draft contract Mission Control sends.
- Writes a small sanitized receipt file under ./data/openclaw_apply_drafts/.
- Returns a JSON response in the shape expected by integrations/openclaw_apply_draft.py.

Important:
- This is a scaffold, not real browser automation.
- It never submits applications.
- It currently reports `skipped` with a clear warning so operators do not confuse
  the stub for a working browser-runner.

Replace the body of this script later with a real OpenClaw / browser automation
implementation once you are ready.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RECEIPT_DIR = ROOT / "data" / "openclaw_apply_drafts"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _slug(value: str, *, fallback: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return text or fallback


def _read_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(json.dumps({"status": "upstream_failure", "errors": [f"invalid_json:{exc.msg}"]}))
    if not isinstance(parsed, dict):
        raise SystemExit(json.dumps({"status": "upstream_failure", "errors": ["payload_must_be_object"]}))
    return parsed


def _require_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise SystemExit(json.dumps({"status": "upstream_failure", "errors": [f"{key}_must_be_object"]}))
    return value


def _write_receipt(payload: dict[str, Any]) -> str:
    action = str(payload.get("action") or "").strip()
    target = _require_dict(payload, "application_target")
    resume_variant = _require_dict(payload, "resume_variant")
    title = str(target.get("title") or "").strip()
    company = str(target.get("company") or "").strip()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    receipt_name = f"{timestamp}-{_slug(company, fallback='company')}-{_slug(title, fallback='role')}.json"
    RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
    receipt_path = RECEIPT_DIR / receipt_name
    sanitized = {
        "recorded_at": _utc_iso(),
        "action": action,
        "submit": bool(payload.get("submit", False)),
        "stop_before_submit": bool(payload.get("stop_before_submit", True)),
        "application_target": {
            "job_id": target.get("job_id"),
            "title": title,
            "company": company,
            "source": target.get("source"),
            "application_url": target.get("application_url"),
            "source_url": target.get("source_url"),
        },
        "resume_variant": {
            "resume_variant_name": resume_variant.get("resume_variant_name"),
            "resume_file_name": resume_variant.get("resume_file_name"),
            "resume_upload_path": resume_variant.get("resume_upload_path"),
        },
        "application_answers_count": len(payload.get("application_answers") or []),
        "cover_letter_included": bool(str(payload.get("cover_letter_text") or "").strip()),
        "capture_screenshots": bool(payload.get("capture_screenshots", True)),
        "max_screenshots": payload.get("max_screenshots"),
        "create_account_if_needed": bool(payload.get("create_account_if_needed", False)),
        "profile_mode": payload.get("profile_mode"),
    }
    receipt_path.write_text(json.dumps(sanitized, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return str(receipt_path)


def main() -> int:
    payload = _read_payload()
    target = _require_dict(payload, "application_target")
    _require_dict(payload, "resume_variant")

    application_url = str(target.get("application_url") or target.get("source_url") or "").strip()
    if not application_url:
        print(
            json.dumps(
                {
                    "status": "upstream_failure",
                    "warnings": [],
                    "errors": ["missing_application_url"],
                    "failure_category": "missing_application_url",
                    "safe_to_retry": False,
                },
                ensure_ascii=True,
            )
        )
        return 0

    receipt_path = _write_receipt(payload)
    print(
        json.dumps(
            {
                "status": "skipped",
                "warnings": [
                    "stub_openclaw_apply_draft_no_browser_automation",
                    f"sanitized_receipt_written:{receipt_path}",
                ],
                "errors": [],
                "failure_category": "stub_no_browser_automation",
                "safe_to_retry": False,
                "account_created": False,
                "fields_filled_manifest": [],
                "screenshots": [],
                "checkpoint_urls": [application_url],
                "page_title": None,
                "blocking_reason": "Replace scripts/openclaw_apply_draft.py with a real OpenClaw browser runner.",
            },
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
