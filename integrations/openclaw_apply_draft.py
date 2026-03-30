from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from typing import Any

DEFAULT_OPENCLAW_APPLY_TIMEOUT_SECONDS = 240
MAX_OPENCLAW_APPLY_TIMEOUT_SECONDS = 1200
DEFAULT_OPENCLAW_APPLY_MAX_SCREENSHOTS = 8
MAX_OPENCLAW_APPLY_MAX_SCREENSHOTS = 20


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"1", "true", "yes"}:
            return True
        if low in {"0", "false", "no"}:
            return False
    return None


def _as_bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _command_parts() -> list[str]:
    raw = str(os.getenv("OPENCLAW_APPLY_DRAFT_COMMAND", "")).strip()
    if not raw:
        return []
    return [part for part in shlex.split(raw) if part.strip()]


def openclaw_apply_command_configured() -> bool:
    return bool(_command_parts())


def openclaw_apply_enabled(request: dict[str, Any] | None = None) -> bool:
    request = request or {}
    parsed = _as_bool(request.get("openclaw_apply_enabled"))
    if parsed is not None:
        return parsed
    env_parsed = _as_bool(os.getenv("OPENCLAW_APPLY_DRAFT_ENABLED"))
    return bool(env_parsed)


def _normalize_screenshot_reference(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    path = str(row.get("path") or row.get("file_path") or "").strip() or None
    url = str(row.get("url") or row.get("image_url") or "").strip() or None
    if not path and not url:
        return None
    return {
        "label": str(row.get("label") or row.get("name") or "").strip() or None,
        "path": path,
        "url": url,
        "captured_at": str(row.get("captured_at") or "").strip() or None,
        "page_url": str(row.get("page_url") or "").strip() or None,
        "mime_type": str(row.get("mime_type") or "").strip() or None,
        "kind": str(row.get("kind") or "screenshot").strip() or "screenshot",
    }


def _normalize_field_manifest_row(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    field_name = str(row.get("field_name") or row.get("name") or row.get("id") or "").strip()
    if not field_name:
        return None
    label = str(row.get("label") or "").strip() or None
    field_type = str(row.get("field_type") or row.get("type") or "").strip() or None
    status = str(row.get("status") or "filled").strip() or "filled"
    sensitive_hint = any(
        token in f"{field_name} {label or ''} {field_type or ''}".lower()
        for token in ("password", "secret", "token", "otp", "passcode")
    )
    value_preview = None if sensitive_hint else str(row.get("value_preview") or "").strip() or None
    return {
        "field_name": field_name,
        "label": label,
        "field_type": field_type,
        "status": status,
        "value_preview": value_preview,
        "value_redacted": bool(row.get("value_redacted", True) or sensitive_hint),
        "required": bool(row.get("required", False)),
    }


def _normalize_status(value: Any, *, fields_filled_count: int) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {
        "awaiting_review",
        "success",
        "auth_blocked",
        "anti_bot_blocked",
        "layout_mismatch",
        "unsupported_form",
        "upstream_failure",
        "skipped",
    }:
        return "awaiting_review" if normalized == "success" else normalized
    if normalized in {"auth", "login_required", "login_blocked"}:
        return "auth_blocked"
    if normalized in {"anti_bot", "blocked_by_bot"}:
        return "anti_bot_blocked"
    if normalized in {"selector_mismatch", "layout_error"}:
        return "layout_mismatch"
    if fields_filled_count > 0:
        return "awaiting_review"
    return "upstream_failure"


def _normalize_failure_category(value: Any, *, status: str) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized:
        aliases = {
            "login_required": "auth_blocked",
            "login_blocked": "auth_blocked",
            "auth": "auth_blocked",
            "anti_bot": "anti_bot_blocked",
            "blocked_by_bot": "anti_bot_blocked",
            "selector_mismatch": "layout_mismatch",
            "layout_error": "layout_mismatch",
            "empty_output": "upstream_failure",
            "invalid_json": "invalid_response",
        }
        return aliases.get(normalized, normalized)
    if status in {"auth_blocked", "anti_bot_blocked", "layout_mismatch", "unsupported_form"}:
        return status
    return None


def _build_command_payload(
    application_target: dict[str, Any],
    resume_variant: dict[str, Any],
    answer_drafts: list[dict[str, Any]],
    request: dict[str, Any],
    *,
    cover_letter_text: str,
) -> dict[str, Any]:
    capture_screenshots = _as_bool(request.get("openclaw_apply_capture_screenshots"))
    if capture_screenshots is None:
        capture_screenshots = True
    max_screenshots = _as_bounded_int(
        request.get("openclaw_apply_max_screenshots") or os.getenv("OPENCLAW_APPLY_MAX_SCREENSHOTS"),
        default=DEFAULT_OPENCLAW_APPLY_MAX_SCREENSHOTS,
        minimum=0,
        maximum=MAX_OPENCLAW_APPLY_MAX_SCREENSHOTS,
    )
    return {
        "action": "apply_draft",
        "submit": False,
        "stop_before_submit": True,
        "application_target": application_target,
        "resume_variant": {
            "resume_variant_name": resume_variant.get("resume_variant_name"),
            "resume_variant_text": resume_variant.get("resume_variant_text"),
            "resume_upload_path": request.get("resume_upload_path"),
            "resume_file_name": resume_variant.get("resume_file_name"),
        },
        "application_answers": answer_drafts,
        "cover_letter_text": cover_letter_text or "",
        "capture_screenshots": bool(capture_screenshots),
        "max_screenshots": max_screenshots,
        "create_account_if_needed": bool(request.get("openclaw_allow_account_creation", False)),
        "profile_mode": str(request.get("profile_mode") or "").strip() or None,
    }


def _invoke_openclaw(command: list[str], payload: dict[str, Any], *, timeout_seconds: int) -> tuple[dict[str, Any], int]:
    started = time.monotonic()
    completed = subprocess.run(
        command,
        input=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
        check=False,
    )
    runtime_ms = int((time.monotonic() - started) * 1000)
    if completed.returncode != 0:
        return (
            {
                "status": "upstream_failure",
                "failure_category": "openclaw_apply_command_failed",
                "warnings": [],
                "errors": [f"openclaw_apply_command_failed_exit_{completed.returncode}"],
            },
            runtime_ms,
        )
    stdout = completed.stdout.decode("utf-8", errors="replace").strip()
    if not stdout:
        return (
            {
                "status": "upstream_failure",
                "failure_category": "openclaw_apply_empty_output",
                "warnings": [],
                "errors": ["openclaw_apply_empty_output"],
            },
            runtime_ms,
        )
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return (
            {
                "status": "upstream_failure",
                "failure_category": "openclaw_apply_invalid_json",
                "warnings": [],
                "errors": ["openclaw_apply_invalid_json"],
            },
            runtime_ms,
        )
    if not isinstance(parsed, dict):
        return (
            {
                "status": "upstream_failure",
                "failure_category": "openclaw_apply_invalid_response_shape",
                "warnings": [],
                "errors": ["openclaw_apply_invalid_response_shape"],
            },
            runtime_ms,
        )
    return parsed, runtime_ms


def run_openclaw_apply_draft(
    *,
    application_target: dict[str, Any],
    resume_variant: dict[str, Any],
    answer_drafts: list[dict[str, Any]],
    request: dict[str, Any],
    cover_letter_text: str,
) -> dict[str, Any]:
    command = _command_parts()
    if not command:
        return {
            "status": "skipped",
            "warnings": ["OpenClaw apply draft command is not configured."],
            "errors": [],
            "meta": {
                "failure_category": "openclaw_apply_not_configured",
                "safe_to_retry": False,
                "screenshots": [],
                "fields_filled_manifest": [],
            },
        }

    timeout_seconds = _as_bounded_int(
        request.get("openclaw_apply_timeout_seconds") or os.getenv("OPENCLAW_APPLY_DRAFT_TIMEOUT_SECONDS"),
        default=DEFAULT_OPENCLAW_APPLY_TIMEOUT_SECONDS,
        minimum=5,
        maximum=MAX_OPENCLAW_APPLY_TIMEOUT_SECONDS,
    )
    payload = _build_command_payload(
        application_target,
        resume_variant,
        answer_drafts,
        request,
        cover_letter_text=cover_letter_text,
    )
    response, runtime_ms = _invoke_openclaw(command, payload, timeout_seconds=timeout_seconds)

    fields_filled_manifest = []
    for row in response.get("fields_filled_manifest") if isinstance(response.get("fields_filled_manifest"), list) else []:
        normalized = _normalize_field_manifest_row(row)
        if normalized is not None:
            fields_filled_manifest.append(normalized)

    screenshots = []
    for row in response.get("screenshots") if isinstance(response.get("screenshots"), list) else []:
        normalized = _normalize_screenshot_reference(row)
        if normalized is not None:
            screenshots.append(normalized)

    status = _normalize_status(response.get("status"), fields_filled_count=len(fields_filled_manifest))
    failure_category = _normalize_failure_category(response.get("failure_category"), status=status)
    if failure_category is None and status == "upstream_failure":
        failure_category = "openclaw_apply_upstream_failure"

    return {
        "status": status,
        "warnings": [str(item).strip() for item in response.get("warnings", []) if str(item).strip()],
        "errors": [str(item).strip() for item in response.get("errors", []) if str(item).strip()],
        "meta": {
            "runtime_ms": runtime_ms,
            "failure_category": failure_category,
            "safe_to_retry": bool(response.get("safe_to_retry", status == "upstream_failure")),
            "awaiting_review": status == "awaiting_review",
            "submitted": False,
            "account_created": bool(response.get("account_created", False)),
            "fields_filled_manifest": fields_filled_manifest,
            "screenshots": screenshots,
            "checkpoint_urls": response.get("checkpoint_urls") if isinstance(response.get("checkpoint_urls"), list) else [],
            "blocking_reason": str(response.get("blocking_reason") or "").strip() or None,
            "page_title": str(response.get("page_title") or "").strip() or None,
        },
    }
