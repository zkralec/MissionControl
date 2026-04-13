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
        "size_bytes": int(row.get("size_bytes") or 0) or None,
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
        "draft_ready",
        "partial_draft",
        "not_started",
        "login_required",
        "captcha_or_bot_challenge",
        "navigation_failed",
        "upload_failed",
        "timed_out",
        "manual_review_required",
        "unavailable",
        "unsafe_submit_attempted",
        "anti_bot_blocked",
        "session_expired",
        "redirected_off_target",
        "inspect_only",
    }:
        if normalized in {"success", "draft_ready", "partial_draft"}:
            return "awaiting_review"
        return normalized
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
            "login_blocked": "login_required",
            "auth": "login_required",
            "anti_bot": "captcha_or_bot_challenge",
            "blocked_by_bot": "captcha_or_bot_challenge",
            "selector_mismatch": "unsupported_form",
            "layout_error": "unsupported_form",
            "empty_output": "upstream_failure",
            "invalid_json": "invalid_response",
            "unsafe_submit_attempted": "unsafe_submit_attempted",
        }
        return aliases.get(normalized, normalized)
    if status in {
        "auth_blocked",
        "anti_bot_blocked",
        "layout_mismatch",
        "unsupported_form",
        "login_required",
        "captcha_or_bot_challenge",
        "upload_failed",
        "navigation_failed",
        "timed_out",
        "manual_review_required",
        "unavailable",
        "unsafe_submit_attempted",
        "anti_bot_blocked",
        "session_expired",
        "redirected_off_target",
    }:
        return status
    return None


def _normalize_notify_decision(value: Any, *, awaiting_review: bool, status: str) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    channels = []
    raw_channels = payload.get("channels")
    if isinstance(raw_channels, list):
        channels = [str(item).strip() for item in raw_channels if str(item).strip()]
    should_notify = awaiting_review and bool(payload.get("should_notify", True))
    reason = str(payload.get("reason") or "").strip() or ("draft_ready_for_review" if should_notify else status)
    return {
        "should_notify": should_notify,
        "reason": reason,
        "channels": channels if should_notify else [],
    }


def _safe_auto_submit_signal(response: dict[str, Any]) -> bool:
    page_diagnostics = response.get("page_diagnostics") if isinstance(response.get("page_diagnostics"), dict) else {}
    form_diagnostics = response.get("form_diagnostics") if isinstance(response.get("form_diagnostics"), dict) else {}
    auto_submit_allowed = bool(page_diagnostics.get("auto_submit_allowed") or form_diagnostics.get("auto_submit_allowed"))
    auto_submit_succeeded = bool(page_diagnostics.get("auto_submit_succeeded") or form_diagnostics.get("auto_submit_succeeded"))
    return bool(response.get("submitted")) and auto_submit_allowed and auto_submit_succeeded


def _build_command_payload(
    application_target: dict[str, Any],
    resume_variant: dict[str, Any],
    candidate_profile: dict[str, Any],
    answer_drafts: list[dict[str, Any]],
    request: dict[str, Any],
    *,
    cover_letter_text: str,
    lineage: dict[str, Any] | None = None,
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
        "inspect_only": bool(request.get("openclaw_apply_inspect_only", request.get("inspect_only", False))),
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
        "candidate_profile": candidate_profile or {},
        "contact_profile": (
            request.get("contact_profile")
            if isinstance(request.get("contact_profile"), dict)
            else candidate_profile.get("contact_profile")
        ),
        "lineage": lineage or {},
    }


def _invoke_openclaw(command: list[str], payload: dict[str, Any], *, timeout_seconds: int) -> tuple[dict[str, Any], int, dict[str, Any]]:
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
    command_debug = {
        "command": list(command),
        "exit_code": completed.returncode,
        "stdout": completed.stdout.decode("utf-8", errors="replace").strip(),
        "stderr": completed.stderr.decode("utf-8", errors="replace").strip(),
        "runtime_ms": runtime_ms,
    }
    if completed.returncode != 0:
        return (
            {
                "status": "upstream_failure",
                "failure_category": "openclaw_apply_command_failed",
                "warnings": [],
                "errors": [f"openclaw_apply_command_failed_exit_{completed.returncode}"],
            },
            runtime_ms,
            command_debug,
        )
    stdout = command_debug["stdout"]
    if not stdout:
        return (
            {
                "status": "upstream_failure",
                "failure_category": "openclaw_apply_empty_output",
                "warnings": [],
                "errors": ["openclaw_apply_empty_output"],
            },
            runtime_ms,
            command_debug,
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
            command_debug,
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
            command_debug,
        )
    debug_json = parsed.get("debug_json") if isinstance(parsed.get("debug_json"), dict) else {}
    parsed["debug_json"] = {"draft_command": command_debug, **debug_json}
    return parsed, runtime_ms, command_debug


def run_openclaw_apply_draft(
    *,
    application_target: dict[str, Any],
    resume_variant: dict[str, Any],
    candidate_profile: dict[str, Any],
    answer_drafts: list[dict[str, Any]],
    request: dict[str, Any],
    cover_letter_text: str,
    lineage: dict[str, Any] | None = None,
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
        candidate_profile,
        answer_drafts,
        request,
        cover_letter_text=cover_letter_text,
        lineage=lineage,
    )
    try:
        response, runtime_ms, command_debug = _invoke_openclaw(command, payload, timeout_seconds=timeout_seconds)
    except subprocess.TimeoutExpired:
        response = {
            "draft_status": "not_started",
            "source_status": "timed_out",
            "awaiting_review": False,
            "review_status": "blocked",
            "submitted": False,
            "failure_category": "timed_out",
            "warnings": [],
            "errors": ["openclaw_apply_timed_out"],
            "fields_filled_manifest": [],
            "screenshot_metadata_references": [],
            "checkpoint_urls": [str(application_target.get("application_url") or application_target.get("source_url") or "").strip()],
            "blocking_reason": "OpenClaw draft command timed out before reaching a safe review checkpoint.",
            "page_title": None,
            "notify_decision": {"should_notify": False, "reason": "timed_out", "channels": []},
            "debug_json": {"draft_command": {"command": list(command), "exit_code": None, "stdout": "", "stderr": "", "runtime_ms": timeout_seconds * 1000, "timed_out": True}},
        }
        runtime_ms = timeout_seconds * 1000
        command_debug = response["debug_json"]["draft_command"]

    fields_filled_manifest = []
    for row in response.get("fields_filled_manifest") if isinstance(response.get("fields_filled_manifest"), list) else []:
        normalized = _normalize_field_manifest_row(row)
        if normalized is not None:
            fields_filled_manifest.append(normalized)

    screenshots = []
    screenshot_rows = (
        response.get("screenshot_metadata_references")
        if isinstance(response.get("screenshot_metadata_references"), list)
        else response.get("screenshots")
    )
    for row in screenshot_rows if isinstance(screenshot_rows, list) else []:
        normalized = _normalize_screenshot_reference(row)
        if normalized is not None:
            screenshots.append(normalized)

    awaiting_review = bool(response.get("awaiting_review", False))
    draft_status = str(response.get("draft_status") or "").strip() or None
    source_status = str(response.get("source_status") or response.get("status") or "").strip() or None
    review_status = str(response.get("review_status") or "").strip() or None
    submitted_signal = bool(response.get("submitted")) or bool(response.get("submit_clicked")) or bool(response.get("final_submit_clicked"))
    safe_submitted = _safe_auto_submit_signal(response)
    status = _normalize_status(
        response.get("status") or review_status or draft_status or source_status,
        fields_filled_count=len(fields_filled_manifest),
    )
    if awaiting_review:
        status = "awaiting_review"
    failure_category = _normalize_failure_category(response.get("failure_category"), status=status)
    if submitted_signal and not safe_submitted:
        failure_category = "unsafe_submit_attempted"
        awaiting_review = False
        review_status = "blocked"
        source_status = "unsafe_submit_attempted"
        status = "unsafe_submit_attempted"
    if failure_category is None and status == "upstream_failure":
        failure_category = "openclaw_apply_upstream_failure"
    if source_status is None:
        source_status = failure_category or ("success" if awaiting_review else status)
    if draft_status is None:
        draft_status = "draft_ready" if awaiting_review and len(fields_filled_manifest) > 0 else ("partial_draft" if len(fields_filled_manifest) > 0 else "not_started")
    if review_status is None:
        review_status = "awaiting_review" if awaiting_review else status
    notify_decision = _normalize_notify_decision(
        response.get("notify_decision"),
        awaiting_review=(awaiting_review or status == "awaiting_review") and not submitted_signal and len(screenshots) > 0,
        status=review_status or status,
    )
    if safe_submitted:
        notify_decision = {"should_notify": False, "reason": "application_submitted", "channels": []}
        status = "success"
        source_status = source_status or "success"
        review_status = "submitted"
    elif submitted_signal:
        notify_decision = {"should_notify": False, "reason": "unsafe_submit_attempted", "channels": []}

    return {
        "status": status,
        "warnings": [str(item).strip() for item in response.get("warnings", []) if str(item).strip()],
        "errors": [str(item).strip() for item in response.get("errors", []) if str(item).strip()],
        "meta": {
            "runtime_ms": runtime_ms,
            "failure_category": failure_category,
            "safe_to_retry": bool(response.get("safe_to_retry", status == "upstream_failure")),
            "draft_status": draft_status,
            "source_status": source_status,
            "awaiting_review": bool((awaiting_review or status == "awaiting_review") and not safe_submitted),
            "review_status": review_status,
            "submitted": safe_submitted,
            "account_created": bool(response.get("account_created", False)),
            "fields_filled_manifest": fields_filled_manifest,
            "screenshots": screenshots,
            "checkpoint_urls": response.get("checkpoint_urls") if isinstance(response.get("checkpoint_urls"), list) else [],
            "blocking_reason": str(response.get("blocking_reason") or "").strip() or None,
            "page_title": str(response.get("page_title") or "").strip() or None,
            "notify_decision": notify_decision,
            "page_diagnostics": response.get("page_diagnostics") if isinstance(response.get("page_diagnostics"), dict) else {},
            "form_diagnostics": response.get("form_diagnostics") if isinstance(response.get("form_diagnostics"), dict) else {},
            "inspect_only": bool(response.get("inspect_only", False)),
            "debug_json": response.get("debug_json") if isinstance(response.get("debug_json"), dict) else {"draft_command": command_debug},
        },
    }
