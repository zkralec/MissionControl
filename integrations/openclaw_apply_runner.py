from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
OPENCLAW_APPLY_ROOT = ROOT / "data" / "openclaw_apply_drafts"
DEFAULT_RECEIPT_DIR = OPENCLAW_APPLY_ROOT / "receipts"
DEFAULT_SCREENSHOT_DIR = OPENCLAW_APPLY_ROOT / "screenshots"
DEFAULT_RESUME_DIR = OPENCLAW_APPLY_ROOT / "resume_uploads"

DEFAULT_TIMEOUT_SECONDS = 240
MAX_TIMEOUT_SECONDS = 1200
DEFAULT_MAX_STEPS = 24
MAX_MAX_STEPS = 200
DEFAULT_MAX_SCREENSHOTS = 8
MAX_MAX_SCREENSHOTS = 20

MEANINGFUL_FIELD_STATUSES = {
    "filled",
    "uploaded",
    "selected",
    "answered",
    "checked",
    "attached",
    "prefilled_verified",
}

FAILURE_BLOCKING_REASONS = {
    "login_required": "The application flow requires a logged-in session that is not currently available.",
    "captcha_or_bot_challenge": "The page presented a captcha or bot challenge that should be handled manually.",
    "unsupported_form": "The form structure could not be safely automated in draft-only mode.",
    "upload_failed": "The tailored resume could not be uploaded successfully.",
    "navigation_failed": "The application page could not be opened or safely progressed.",
    "timed_out": "The browser runner hit its time budget before reaching a safe review checkpoint.",
    "manual_review_required": "Automation stopped at a manual review checkpoint without enough confidence to continue.",
    "tool_unavailable": "No OpenClaw adapter is currently available for Mission Control to invoke.",
    "invalid_input": "Mission Control sent an invalid apply-draft payload.",
}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _slug(value: str, *, fallback: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return text or fallback


def _dedupe_text(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        trimmed = str(value or "").strip()
        if not trimmed or trimmed in seen:
            continue
        seen.add(trimmed)
        output.append(trimmed)
    return output


def _as_bool(value: Any, *, default: bool | None = None) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _as_bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _as_text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _looks_sensitive(name: str | None, label: str | None, field_type: str | None) -> bool:
    text = " ".join(filter(None, [name, label, field_type])).lower()
    return any(token in text for token in ("password", "secret", "token", "otp", "passcode"))


def _file_ext(file_name: str | None, *, default: str = ".txt") -> str:
    suffix = Path(str(file_name or "").strip()).suffix
    return suffix if suffix else default


@dataclass(frozen=True)
class RunnerConfig:
    adapter: str
    tool_command: str | None
    python_entrypoint: str | None
    headless: bool
    screenshot_root: Path
    receipt_root: Path
    resume_root: Path
    timeout_seconds: int
    max_steps: int
    log_level: str
    auth_strategy: str | None
    storage_state_path: str | None
    browser_profile_path: str | None


@dataclass(frozen=True)
class ArtifactPaths:
    run_key: str
    screenshot_dir: Path
    receipt_path: Path
    generated_resume_path: Path


class ApplyAdapter(Protocol):
    def run(self, request: dict[str, Any]) -> dict[str, Any]:
        ...


class UnavailableAdapter:
    def __init__(self, reason: str) -> None:
        self._reason = reason

    def run(self, request: dict[str, Any]) -> dict[str, Any]:
        return {
            "draft_status": "not_started",
            "source_status": "unavailable",
            "awaiting_review": False,
            "review_status": "blocked",
            "submitted": False,
            "failure_category": "tool_unavailable",
            "blocking_reason": self._reason,
            "fields_filled_manifest": [],
            "screenshot_metadata_references": [],
            "checkpoint_urls": [
                str(
                    (
                        (request.get("application_target") if isinstance(request.get("application_target"), dict) else {})
                    ).get("application_url")
                    or ""
                ).strip()
            ],
            "page_title": None,
            "warnings": ["openclaw_adapter_unavailable"],
            "errors": [],
            "notify_decision": {"should_notify": False, "reason": "tool_unavailable", "channels": []},
            "account_created": False,
            "safe_to_retry": False,
        }


class CommandAdapter:
    def __init__(self, config: RunnerConfig) -> None:
        self._config = config
        self._command = [part for part in shlex.split(str(config.tool_command or "")) if part.strip()]

    def run(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self._command:
            return UnavailableAdapter("OPENCLAW_APPLY_TOOL_COMMAND is not configured.").run(request)
        completed = subprocess.run(
            self._command,
            input=json.dumps(request, ensure_ascii=True).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self._config.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            return {
                "draft_status": "not_started",
                "source_status": "command_failed",
                "awaiting_review": False,
                "review_status": "blocked",
                "submitted": False,
                "failure_category": "navigation_failed",
                "blocking_reason": f"OpenClaw command exited with code {completed.returncode}.",
                "fields_filled_manifest": [],
                "screenshot_metadata_references": [],
                "checkpoint_urls": [],
                "page_title": None,
                "warnings": [],
                "errors": [f"openclaw_command_failed_exit_{completed.returncode}"],
                "notify_decision": {"should_notify": False, "reason": "navigation_failed", "channels": []},
                "account_created": False,
                "safe_to_retry": True,
            }
        stdout = completed.stdout.decode("utf-8", errors="replace").strip()
        if not stdout:
            return {
                "draft_status": "not_started",
                "source_status": "empty_output",
                "awaiting_review": False,
                "review_status": "blocked",
                "submitted": False,
                "failure_category": "navigation_failed",
                "blocking_reason": "OpenClaw command returned no JSON output.",
                "fields_filled_manifest": [],
                "screenshot_metadata_references": [],
                "checkpoint_urls": [],
                "page_title": None,
                "warnings": [],
                "errors": ["openclaw_command_empty_output"],
                "notify_decision": {"should_notify": False, "reason": "navigation_failed", "channels": []},
                "account_created": False,
                "safe_to_retry": True,
            }
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            return {
                "draft_status": "not_started",
                "source_status": "invalid_output",
                "awaiting_review": False,
                "review_status": "blocked",
                "submitted": False,
                "failure_category": "navigation_failed",
                "blocking_reason": "OpenClaw command returned invalid JSON.",
                "fields_filled_manifest": [],
                "screenshot_metadata_references": [],
                "checkpoint_urls": [],
                "page_title": None,
                "warnings": [],
                "errors": ["openclaw_command_invalid_json"],
                "notify_decision": {"should_notify": False, "reason": "navigation_failed", "channels": []},
                "account_created": False,
                "safe_to_retry": True,
            }
        if not isinstance(parsed, dict):
            return {
                "draft_status": "not_started",
                "source_status": "invalid_output",
                "awaiting_review": False,
                "review_status": "blocked",
                "submitted": False,
                "failure_category": "navigation_failed",
                "blocking_reason": "OpenClaw command returned a non-object JSON payload.",
                "fields_filled_manifest": [],
                "screenshot_metadata_references": [],
                "checkpoint_urls": [],
                "page_title": None,
                "warnings": [],
                "errors": ["openclaw_command_invalid_response_shape"],
                "notify_decision": {"should_notify": False, "reason": "navigation_failed", "channels": []},
                "account_created": False,
                "safe_to_retry": True,
            }
        return parsed


class PythonEntrypointAdapter:
    def __init__(self, callable_ref: Any) -> None:
        self._callable = callable_ref

    def run(self, request: dict[str, Any]) -> dict[str, Any]:
        result = self._callable(request)
        if not isinstance(result, dict):
            raise TypeError("OpenClaw Python entrypoint must return a dict.")
        return result


def build_config_from_env() -> RunnerConfig:
    return RunnerConfig(
        adapter=str(os.getenv("OPENCLAW_APPLY_ADAPTER") or "auto").strip().lower() or "auto",
        tool_command=str(os.getenv("OPENCLAW_APPLY_TOOL_COMMAND") or "").strip() or None,
        python_entrypoint=str(os.getenv("OPENCLAW_APPLY_PYTHON_ENTRYPOINT") or "").strip() or None,
        headless=bool(_as_bool(os.getenv("OPENCLAW_APPLY_HEADLESS"), default=True)),
        screenshot_root=Path(
            str(os.getenv("OPENCLAW_APPLY_SCREENSHOT_DIR") or DEFAULT_SCREENSHOT_DIR).strip() or DEFAULT_SCREENSHOT_DIR
        ),
        receipt_root=Path(
            str(os.getenv("OPENCLAW_APPLY_RECEIPT_DIR") or DEFAULT_RECEIPT_DIR).strip() or DEFAULT_RECEIPT_DIR
        ),
        resume_root=Path(
            str(os.getenv("OPENCLAW_APPLY_RESUME_DIR") or DEFAULT_RESUME_DIR).strip() or DEFAULT_RESUME_DIR
        ),
        timeout_seconds=_as_bounded_int(
            os.getenv("OPENCLAW_APPLY_TIMEOUT_SECONDS"),
            default=DEFAULT_TIMEOUT_SECONDS,
            minimum=5,
            maximum=MAX_TIMEOUT_SECONDS,
        ),
        max_steps=_as_bounded_int(
            os.getenv("OPENCLAW_APPLY_MAX_STEPS"),
            default=DEFAULT_MAX_STEPS,
            minimum=1,
            maximum=MAX_MAX_STEPS,
        ),
        log_level=str(os.getenv("OPENCLAW_APPLY_LOG_LEVEL") or "INFO").strip().upper() or "INFO",
        auth_strategy=str(os.getenv("OPENCLAW_APPLY_AUTH_STRATEGY") or "").strip() or None,
        storage_state_path=str(os.getenv("OPENCLAW_APPLY_STORAGE_STATE_PATH") or "").strip() or None,
        browser_profile_path=str(os.getenv("OPENCLAW_APPLY_BROWSER_PROFILE_PATH") or "").strip() or None,
    )


def _resolve_python_entrypoint(entrypoint: str | None) -> Any | None:
    candidates = [entrypoint] if entrypoint else [
        "openclaw:run_apply_draft",
        "openclaw:apply_draft",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        module_name, sep, attr_path = candidate.partition(":")
        if not sep or not module_name or not attr_path:
            continue
        try:
            current = import_module(module_name)
        except Exception:
            continue
        try:
            for segment in attr_path.split("."):
                current = getattr(current, segment)
        except AttributeError:
            continue
        if callable(current):
            return current
    return None


def resolve_adapter(config: RunnerConfig) -> ApplyAdapter:
    if config.adapter in {"auto", "python"}:
        python_callable = _resolve_python_entrypoint(config.python_entrypoint)
        if python_callable is not None:
            return PythonEntrypointAdapter(python_callable)
        if config.adapter == "python":
            return UnavailableAdapter(
                "OPENCLAW_APPLY_ADAPTER=python was requested, but no callable could be imported from "
                "OPENCLAW_APPLY_PYTHON_ENTRYPOINT or the default openclaw module."
            )
    if config.adapter in {"auto", "command"}:
        if config.tool_command:
            return CommandAdapter(config)
        if config.adapter == "command":
            return UnavailableAdapter("OPENCLAW_APPLY_ADAPTER=command was requested, but OPENCLAW_APPLY_TOOL_COMMAND is empty.")
    return UnavailableAdapter(
        "No OpenClaw Python entrypoint or external tool command is configured for Mission Control."
    )


def _configure_logging(level_name: str) -> logging.Logger:
    logger = logging.getLogger("openclaw_apply_runner")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(getattr(logging, level_name, logging.INFO))
    logger.propagate = False
    return logger


def read_payload(input_json_file: str | None = None) -> dict[str, Any]:
    if input_json_file:
        raw = Path(input_json_file).read_text(encoding="utf-8")
    else:
        raw = sys.stdin.read()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid_json:{exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("payload_must_be_object")
    return parsed


def _require_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key}_must_be_object")
    return value


def _application_url(payload: dict[str, Any]) -> str:
    target = payload.get("application_target") if isinstance(payload.get("application_target"), dict) else {}
    return str(target.get("application_url") or target.get("source_url") or "").strip()


def _run_key(payload: dict[str, Any]) -> str:
    lineage = payload.get("lineage") if isinstance(payload.get("lineage"), dict) else {}
    target = payload.get("application_target") if isinstance(payload.get("application_target"), dict) else {}
    parts = [
        str(lineage.get("pipeline_id") or "").strip(),
        str(lineage.get("task_id") or "").strip(),
        str(lineage.get("run_id") or "").strip(),
        str(target.get("job_id") or "").strip(),
        str(target.get("company") or "").strip(),
        str(target.get("title") or "").strip(),
    ]
    slugged = [_slug(part, fallback="") for part in parts if str(part).strip()]
    return "-".join(part for part in slugged if part) or "application-draft"


def build_artifact_paths(payload: dict[str, Any], config: RunnerConfig) -> ArtifactPaths:
    run_key = _run_key(payload)
    screenshot_dir = config.screenshot_root / run_key
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    config.receipt_root.mkdir(parents=True, exist_ok=True)
    config.resume_root.mkdir(parents=True, exist_ok=True)
    resume_variant = payload.get("resume_variant") if isinstance(payload.get("resume_variant"), dict) else {}
    generated_resume_path = config.resume_root / f"{run_key}{_file_ext(str(resume_variant.get('resume_file_name') or ''), default='.txt')}"
    receipt_path = config.receipt_root / f"{run_key}.json"
    return ArtifactPaths(
        run_key=run_key,
        screenshot_dir=screenshot_dir,
        receipt_path=receipt_path,
        generated_resume_path=generated_resume_path,
    )


def _build_auth_context(logger: logging.Logger, config: RunnerConfig) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    storage_state_path = None
    if config.storage_state_path:
        storage_candidate = Path(config.storage_state_path)
        if storage_candidate.exists():
            storage_state_path = str(storage_candidate.resolve())
        else:
            warnings.append("openclaw_auth_storage_state_missing")
            logger.warning("Configured storage state file is missing; login automation will stay disabled.")

    browser_profile_path = None
    if config.browser_profile_path:
        profile_candidate = Path(config.browser_profile_path)
        if profile_candidate.exists():
            browser_profile_path = str(profile_candidate.resolve())
        else:
            warnings.append("openclaw_browser_profile_missing")
            logger.warning("Configured browser profile path is missing; login automation will stay disabled.")

    session_available = bool(storage_state_path or browser_profile_path or config.auth_strategy == "existing_session")
    return (
        {
            "session_available": session_available,
            "strategy": config.auth_strategy,
            "storage_state_path": storage_state_path,
            "browser_profile_path": browser_profile_path,
        },
        warnings,
    )


def _materialize_resume_file(payload: dict[str, Any], paths: ArtifactPaths, logger: logging.Logger) -> tuple[str | None, list[str]]:
    warnings: list[str] = []
    resume_variant = _require_dict(payload, "resume_variant")
    requested_path = str(resume_variant.get("resume_upload_path") or "").strip()
    if requested_path:
        candidate = Path(requested_path).expanduser()
        if candidate.exists():
            resolved = str(candidate.resolve())
            logger.info("Using existing resume upload file %s", Path(resolved).name)
            return resolved, warnings
        warnings.append("configured_resume_upload_path_missing")

    resume_text = str(resume_variant.get("resume_variant_text") or "").strip()
    if not resume_text:
        return None, warnings

    paths.generated_resume_path.write_text(resume_text + "\n", encoding="utf-8")
    logger.info("Materialized tailored resume upload file %s", paths.generated_resume_path.name)
    return str(paths.generated_resume_path.resolve()), warnings


def _sanitize_request_for_receipt(payload: dict[str, Any], *, materialized_resume_path: str | None) -> dict[str, Any]:
    target = payload.get("application_target") if isinstance(payload.get("application_target"), dict) else {}
    resume_variant = payload.get("resume_variant") if isinstance(payload.get("resume_variant"), dict) else {}
    answers = payload.get("application_answers") if isinstance(payload.get("application_answers"), list) else []
    lineage = payload.get("lineage") if isinstance(payload.get("lineage"), dict) else {}
    application_url = str(target.get("application_url") or target.get("source_url") or "").strip()
    parsed_url = urlparse(application_url) if application_url else None
    return {
        "recorded_at": _utc_iso(),
        "action": str(payload.get("action") or "").strip() or "apply_draft",
        "stop_before_submit": True,
        "application_target": {
            "job_id": target.get("job_id"),
            "title": target.get("title"),
            "company": target.get("company"),
            "source": target.get("source"),
            "application_url": application_url,
            "application_host": parsed_url.netloc if parsed_url else None,
        },
        "resume_variant": {
            "resume_variant_name": resume_variant.get("resume_variant_name"),
            "resume_file_name": resume_variant.get("resume_file_name"),
            "resume_upload_path": materialized_resume_path,
            "resume_text_present": bool(str(resume_variant.get("resume_variant_text") or "").strip()),
        },
        "answer_count": len(answers),
        "cover_letter_present": bool(str(payload.get("cover_letter_text") or "").strip()),
        "capture_screenshots": bool(payload.get("capture_screenshots", True)),
        "max_screenshots": payload.get("max_screenshots"),
        "profile_mode": payload.get("profile_mode"),
        "lineage": {
            "pipeline_id": lineage.get("pipeline_id"),
            "task_id": lineage.get("task_id"),
            "run_id": lineage.get("run_id"),
        },
    }


def _normalize_field_manifest_row(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    field_name = str(row.get("field_name") or row.get("name") or row.get("id") or "").strip()
    if not field_name:
        return None
    label = str(row.get("label") or "").strip() or None
    field_type = str(row.get("field_type") or row.get("type") or "").strip() or None
    sensitive = _looks_sensitive(field_name, label, field_type)
    value_preview = None if sensitive else str(row.get("value_preview") or "").strip() or None
    status = str(row.get("status") or "filled").strip().lower() or "filled"
    return {
        "field_name": field_name,
        "label": label,
        "field_type": field_type,
        "status": status,
        "value_preview": value_preview,
        "value_redacted": bool(row.get("value_redacted", True) or sensitive),
        "required": bool(row.get("required", False)),
    }


def _normalize_screenshot_reference(
    row: Any,
    *,
    screenshot_dir: Path,
    fallback_captured_at: str,
) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    raw_path = str(row.get("path") or row.get("file_path") or "").strip()
    raw_url = str(row.get("url") or row.get("image_url") or "").strip()
    if not raw_path and not raw_url:
        return None
    resolved_path = None
    if raw_path:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = screenshot_dir / candidate
        resolved_path = str(candidate.resolve())
    return {
        "label": str(row.get("label") or row.get("name") or "").strip() or None,
        "path": resolved_path,
        "url": raw_url or None,
        "captured_at": str(row.get("captured_at") or "").strip() or fallback_captured_at,
        "page_url": str(row.get("page_url") or "").strip() or None,
        "mime_type": str(row.get("mime_type") or "").strip() or "image/png",
        "kind": str(row.get("kind") or "screenshot").strip() or "screenshot",
        "size_bytes": int(row.get("size_bytes") or 0) or None,
    }


def _meaningful_progress(fields_filled_manifest: list[dict[str, Any]], raw_result: dict[str, Any]) -> bool:
    if bool(raw_result.get("meaningful_progress")):
        return True
    for row in fields_filled_manifest:
        if str(row.get("status") or "").strip().lower() in MEANINGFUL_FIELD_STATUSES:
            return True
    raw_draft_status = str(raw_result.get("draft_status") or "").strip().lower()
    if raw_draft_status in {"draft_ready", "partial_draft"}:
        return True
    return False


def _default_blocking_reason(failure_category: str | None) -> str | None:
    if not failure_category:
        return None
    return FAILURE_BLOCKING_REASONS.get(failure_category, failure_category.replace("_", " "))


def _normalize_result(
    payload: dict[str, Any],
    raw_result: dict[str, Any],
    *,
    paths: ArtifactPaths,
    preflight_warnings: list[str],
) -> dict[str, Any]:
    captured_at = _utc_iso()
    warnings = _dedupe_text(preflight_warnings + _as_text_list(raw_result.get("warnings")))
    errors = _dedupe_text(_as_text_list(raw_result.get("errors")))

    fields_filled_manifest: list[dict[str, Any]] = []
    for row in raw_result.get("fields_filled_manifest") if isinstance(raw_result.get("fields_filled_manifest"), list) else []:
        normalized = _normalize_field_manifest_row(row)
        if normalized is not None:
            fields_filled_manifest.append(normalized)

    screenshot_rows = raw_result.get("screenshot_metadata_references")
    if not isinstance(screenshot_rows, list):
        screenshot_rows = raw_result.get("screenshots")
    screenshots: list[dict[str, Any]] = []
    for row in screenshot_rows if isinstance(screenshot_rows, list) else []:
        normalized = _normalize_screenshot_reference(row, screenshot_dir=paths.screenshot_dir, fallback_captured_at=captured_at)
        if normalized is not None:
            screenshots.append(normalized)

    meaningful_progress = _meaningful_progress(fields_filled_manifest, raw_result)
    failure_category = str(raw_result.get("failure_category") or "").strip() or None
    blocking_reason = str(raw_result.get("blocking_reason") or "").strip() or _default_blocking_reason(failure_category)

    submitted_signal = bool(raw_result.get("submitted")) or bool(raw_result.get("submit_clicked")) or bool(raw_result.get("final_submit_clicked"))
    if submitted_signal:
        warnings.append("submit_signal_detected_and_overridden")
        errors.append("adapter_reported_submit_signal")
        failure_category = failure_category or "manual_review_required"
        blocking_reason = blocking_reason or "OpenClaw reported a submit signal, so Mission Control forced a manual review outcome."

    source_status = str(raw_result.get("source_status") or raw_result.get("status") or "").strip().lower()
    if not source_status:
        if failure_category:
            source_status = failure_category
        elif meaningful_progress:
            source_status = "success"
        else:
            source_status = "navigation_failed"

    awaiting_review_raw = bool(raw_result.get("awaiting_review"))
    review_status_raw = str(raw_result.get("review_status") or "").strip().lower()
    awaiting_review = meaningful_progress and (
        awaiting_review_raw or review_status_raw == "awaiting_review" or source_status == "success"
    )
    draft_status = str(raw_result.get("draft_status") or "").strip().lower()
    if not draft_status:
        if awaiting_review and not failure_category:
            draft_status = "draft_ready"
        elif meaningful_progress:
            draft_status = "partial_draft"
        else:
            draft_status = "not_started"
    review_status = review_status_raw or ("awaiting_review" if awaiting_review else "blocked")

    checkpoint_urls = _dedupe_text(
        [str(raw_result.get("current_url") or "").strip()]
        + _as_text_list(raw_result.get("checkpoint_urls"))
        + [_application_url(payload)]
    )

    notify_decision = raw_result.get("notify_decision") if isinstance(raw_result.get("notify_decision"), dict) else {}
    notify_reason = str(notify_decision.get("reason") or "").strip() or (
        "draft_ready_for_review" if awaiting_review else (review_status or source_status or "blocked")
    )
    should_notify = awaiting_review and bool(notify_decision.get("should_notify", True))
    normalized = {
        "status": "awaiting_review" if awaiting_review else (source_status or "upstream_failure"),
        "draft_status": draft_status,
        "source_status": source_status,
        "awaiting_review": awaiting_review,
        "review_status": "awaiting_review" if awaiting_review else review_status,
        "submitted": False,
        "failure_category": failure_category,
        "blocking_reason": blocking_reason,
        "fields_filled_manifest": fields_filled_manifest,
        "screenshot_metadata_references": screenshots,
        "screenshots": screenshots,
        "checkpoint_urls": checkpoint_urls,
        "page_title": str(raw_result.get("page_title") or "").strip() or None,
        "warnings": _dedupe_text(warnings),
        "errors": _dedupe_text(errors),
        "notify_decision": {
            "should_notify": should_notify,
            "reason": notify_reason,
            "channels": _as_text_list(notify_decision.get("channels")),
        },
        "account_created": bool(raw_result.get("account_created", False)),
        "safe_to_retry": bool(raw_result.get("safe_to_retry", source_status in {"timed_out", "navigation_failed"})),
    }
    return normalized


def _write_receipt(receipt_path: Path, request_summary: dict[str, Any], result: dict[str, Any]) -> None:
    receipt_path.write_text(
        json.dumps(
            {
                "request": request_summary,
                "result_summary": {
                    "recorded_at": _utc_iso(),
                    "draft_status": result.get("draft_status"),
                    "source_status": result.get("source_status"),
                    "awaiting_review": result.get("awaiting_review"),
                    "review_status": result.get("review_status"),
                    "submitted": False,
                    "failure_category": result.get("failure_category"),
                    "blocking_reason": result.get("blocking_reason"),
                    "fields_filled_count": len(result.get("fields_filled_manifest") or []),
                    "screenshots_count": len(result.get("screenshot_metadata_references") or []),
                    "checkpoint_urls": result.get("checkpoint_urls") or [],
                    "warnings": result.get("warnings") or [],
                    "errors": result.get("errors") or [],
                },
            },
            indent=2,
            ensure_ascii=True,
        )
        + "\n",
        encoding="utf-8",
    )


def invalid_input_result(errors: list[str], *, failure_category: str = "invalid_input") -> dict[str, Any]:
    blocking_reason = _default_blocking_reason(failure_category)
    return {
        "status": "upstream_failure",
        "draft_status": "not_started",
        "source_status": failure_category,
        "awaiting_review": False,
        "review_status": "blocked",
        "submitted": False,
        "failure_category": failure_category,
        "blocking_reason": blocking_reason,
        "fields_filled_manifest": [],
        "screenshot_metadata_references": [],
        "screenshots": [],
        "checkpoint_urls": [],
        "page_title": None,
        "warnings": [],
        "errors": errors,
        "notify_decision": {"should_notify": False, "reason": failure_category, "channels": []},
        "account_created": False,
        "safe_to_retry": False,
    }


def execute_apply_draft(
    payload: dict[str, Any],
    *,
    config: RunnerConfig | None = None,
    adapter: ApplyAdapter | None = None,
) -> dict[str, Any]:
    config = config or build_config_from_env()
    logger = _configure_logging(config.log_level)

    target = _require_dict(payload, "application_target")
    _require_dict(payload, "resume_variant")
    application_url = str(target.get("application_url") or target.get("source_url") or "").strip()
    if not application_url:
        return invalid_input_result(["missing_application_url"])

    paths = build_artifact_paths(payload, config)
    auth_context, auth_warnings = _build_auth_context(logger, config)
    materialized_resume_path, resume_warnings = _materialize_resume_file(payload, paths, logger)
    request_summary = _sanitize_request_for_receipt(payload, materialized_resume_path=materialized_resume_path)

    logger.info(
        "Running OpenClaw apply draft for %s at %s with adapter=%s headless=%s",
        paths.run_key,
        urlparse(application_url).netloc or application_url,
        config.adapter,
        config.headless,
    )

    resolved_adapter = adapter or resolve_adapter(config)
    request = {
        "action": "apply_draft",
        "submit": False,
        "stop_before_submit": True,
        "application_target": target,
        "resume_variant": {
            **_require_dict(payload, "resume_variant"),
            "resume_upload_path": materialized_resume_path,
        },
        "application_answers": payload.get("application_answers") if isinstance(payload.get("application_answers"), list) else [],
        "cover_letter_text": str(payload.get("cover_letter_text") or "").strip(),
        "capture_screenshots": bool(payload.get("capture_screenshots", True)),
        "max_screenshots": _as_bounded_int(
            payload.get("max_screenshots"),
            default=DEFAULT_MAX_SCREENSHOTS,
            minimum=0,
            maximum=MAX_MAX_SCREENSHOTS,
        ),
        "profile_mode": str(payload.get("profile_mode") or "").strip() or None,
        "constraints": {
            "stop_before_submit": True,
            "submit": False,
            "headless": config.headless,
            "timeout_seconds": config.timeout_seconds,
            "max_steps": config.max_steps,
            "allow_account_creation": False,
        },
        "auth": auth_context,
        "artifacts": {
            "run_key": paths.run_key,
            "receipt_path": str(paths.receipt_path.resolve()),
            "screenshot_dir": str(paths.screenshot_dir.resolve()),
            "resume_upload_path": materialized_resume_path,
        },
        "lineage": payload.get("lineage") if isinstance(payload.get("lineage"), dict) else {},
    }

    preflight_warnings = auth_warnings + resume_warnings
    try:
        raw_result = resolved_adapter.run(request)
    except subprocess.TimeoutExpired:
        raw_result = {
            "draft_status": "not_started",
            "source_status": "timed_out",
            "awaiting_review": False,
            "review_status": "blocked",
            "submitted": False,
            "failure_category": "timed_out",
            "blocking_reason": _default_blocking_reason("timed_out"),
            "fields_filled_manifest": [],
            "screenshot_metadata_references": [],
            "checkpoint_urls": [application_url],
            "page_title": None,
            "warnings": [],
            "errors": ["openclaw_apply_timed_out"],
            "notify_decision": {"should_notify": False, "reason": "timed_out", "channels": []},
            "account_created": False,
            "safe_to_retry": True,
        }
    except Exception as exc:
        logger.exception("OpenClaw adapter raised an exception for %s", paths.run_key)
        raw_result = {
            "draft_status": "not_started",
            "source_status": "navigation_failed",
            "awaiting_review": False,
            "review_status": "blocked",
            "submitted": False,
            "failure_category": "navigation_failed",
            "blocking_reason": f"OpenClaw adapter error: {type(exc).__name__}",
            "fields_filled_manifest": [],
            "screenshot_metadata_references": [],
            "checkpoint_urls": [application_url],
            "page_title": None,
            "warnings": [],
            "errors": [f"openclaw_adapter_exception:{type(exc).__name__}"],
            "notify_decision": {"should_notify": False, "reason": "navigation_failed", "channels": []},
            "account_created": False,
            "safe_to_retry": True,
        }

    normalized = _normalize_result(payload, raw_result, paths=paths, preflight_warnings=preflight_warnings)
    _write_receipt(paths.receipt_path, request_summary, normalized)
    return normalized


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for the Mission Control OpenClaw draft runner.

    Example invocations:
    - `python3 scripts/openclaw_apply_draft.py < payload.json`
    - `python3 scripts/openclaw_apply_draft.py --input-json-file payload.json`
    """

    parser = argparse.ArgumentParser(description="Mission Control draft-only OpenClaw runner")
    parser.add_argument("--input-json-file", dest="input_json_file", help="Path to an input JSON payload file.")
    args = parser.parse_args(argv)

    try:
        payload = read_payload(args.input_json_file)
        result = execute_apply_draft(payload)
    except ValueError as exc:
        result = invalid_input_result([str(exc)])
    print(json.dumps(result, ensure_ascii=True))
    return 0
