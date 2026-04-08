from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from integrations.openclaw_apply_answer_profile import (
    DEFAULT_AUTO_SUBMIT_MIN_CONFIDENCE,
    DEFAULT_FILL_MIN_CONFIDENCE,
    build_default_answer_profile,
    is_self_id_key,
    motivation_answer,
    normalize_canonical_key,
    resolve_default_answer,
)


DEFAULT_BROWSER_COMMAND = "/opt/openclaw/npm-global/bin/openclaw browser"
DEFAULT_TIMEOUT_MS = 30_000
DEFAULT_MAX_SNAPSHOT_CHARS = 12_000
DEFAULT_HOST_GATEWAY_ALIAS = "host.docker.internal"
DEFAULT_LINKEDIN_LATER_STEP_MAX_ACTIONS_PER_SIGNATURE = 12
DEFAULT_LINKEDIN_LATER_STEP_MAX_REPEATED_SIGNATURES = 2
UPLOAD_STAGING_DIR = Path("/tmp/openclaw/uploads")
LINKEDIN_ALLOWED_RESUME_EXTENSIONS = (".pdf", ".docx", ".doc")
LOGIN_HINTS = ("sign in", "log in", "login", "authenticate", "continue with", "create account")
CAPTCHA_HINTS = ("captcha", "recaptcha", "hcaptcha", "i am human", "verify you are human")
ANTI_BOT_HINTS = ("unusual traffic", "access denied", "bot detection", "blocked", "security check")
SUBMIT_HINTS = ("submit", "apply now", "finish application", "send application")
LINKEDIN_LOGIN_PAGE_HINTS = (
    "sign in",
    "log in",
    "forgot password",
    "new to linkedin",
    "join now",
    "continue to linkedin",
)
LINKEDIN_CHECKPOINT_HINTS = (
    "checkpoint",
    "security verification",
    "verify it's you",
    "enter the code",
    "two-step verification",
    "verification code",
    "challenge",
)
LINKEDIN_DIALOG_HINTS = (
    'dialog "apply to',
    'heading "contact info"',
    'heading "resume"',
    'heading "review your application"',
    'heading "work experience"',
    'heading "education"',
)
LINKEDIN_NAV_HINTS = ("my network", "messaging", "notifications", "for business", "me")
LINKEDIN_JOB_PAGE_HINTS = (
    "easy apply",
    "show more",
    "meet the hiring team",
    "about the job",
    "jobs you may be interested in",
)
CONTACT_TEXT_FIELD_KEYWORDS: dict[str, tuple[str, ...]] = {
    "first_name": ("first name", "given name"),
    "last_name": ("last name", "family name", "surname"),
    "email_address": ("email address", "email"),
    "city": ("city",),
    "state_or_province": ("state or province", "state", "province", "region"),
    "postal_code": ("zip postal code", "zip code", "postal code", "postcode"),
    "country": ("country",),
    "primary_phone_number": ("primary phone number", "phone number", "phone"),
    "phone_type": ("phone type", "type"),
}
PHONE_TYPE_OPTIONS = {
    "mobile": {"mobile", "cell", "cell phone"},
    "home": {"home"},
    "work": {"work", "office"},
}
INFERRED_REQUIRED_CANONICAL_KEYS = {
    "first_name",
    "last_name",
    "email",
    "city",
    "state_or_province",
    "postal_code",
    "country",
    "primary_phone_number",
    "phone_type",
    "work_authorized_us",
    "sponsorship_required",
    "background_check_ok",
    "drug_screen_ok",
    "accommodation_capability",
}
SNAPSHOT_CONTROL_HINTS = (
    "textbox",
    "textarea",
    "input",
    "field",
    "group",
    "fieldset",
    "legend",
    "combobox",
    "select",
    "dropdown",
    "option",
    "menuitem",
    "listitem",
    "listbox",
    "radio",
    "checkbox",
    "upload",
    "attach",
    "file",
)
SELF_ID_HINT_TOKENS = (
    "veteran",
    "disability",
    "gender",
    "ethnicity",
    "race",
    "hispanic",
    "latino",
    "self identify",
    "voluntary self identification",
)
KNOWN_QUESTION_HINT_TOKENS = (
    "authorized to work",
    "legally authorized",
    "sponsorship",
    "security clearance",
    "polygraph",
    "background check",
    "drug screen",
    "worked here before",
    "affiliate",
    "interviewed here before",
    "hear about us",
    "relocate",
    "travel",
    "salary",
    "start date",
    "essential functions",
    "accommodation",
    "text message",
    "sms",
    "additional information",
    "anything else",
)
EMAIL_PATTERN = re.compile(r"([A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,})", re.IGNORECASE)
PHONE_PATTERN = re.compile(r"(\+?\d[\d(). \-]{8,}\d)")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _current_form_date() -> str:
    return datetime.now().astimezone().strftime("%m/%d/%Y")


def _result(
    *,
    draft_status: str,
    source_status: str,
    awaiting_review: bool,
    review_status: str,
    failure_category: str | None,
    blocking_reason: str | None,
    fields_filled_manifest: list[dict[str, Any]] | None = None,
    screenshot_metadata_references: list[dict[str, Any]] | None = None,
    checkpoint_urls: list[str] | None = None,
    page_title: str | None = None,
    warnings: list[str] | None = None,
    errors: list[str] | None = None,
    notify_reason: str | None = None,
    page_diagnostics: dict[str, Any] | None = None,
    form_diagnostics: dict[str, Any] | None = None,
    debug_json: dict[str, Any] | None = None,
    inspect_only: bool = False,
) -> dict[str, Any]:
    should_notify = (
        awaiting_review
        and not inspect_only
        and len(screenshot_metadata_references or []) > 0
    )
    return {
        "draft_status": draft_status,
        "source_status": source_status,
        "awaiting_review": awaiting_review,
        "review_status": review_status,
        "submitted": False,
        "failure_category": failure_category,
        "blocking_reason": blocking_reason,
        "fields_filled_manifest": fields_filled_manifest or [],
        "screenshot_metadata_references": screenshot_metadata_references or [],
        "checkpoint_urls": checkpoint_urls or [],
        "page_title": page_title,
        "warnings": warnings or [],
        "errors": errors or [],
        "notify_decision": {
            "should_notify": should_notify,
            "reason": notify_reason or ("draft_ready_for_review" if should_notify else (review_status or source_status)),
            "channels": [],
        },
        "page_diagnostics": page_diagnostics or {},
        "form_diagnostics": form_diagnostics or {},
        "debug_json": debug_json or {},
        "inspect_only": inspect_only,
    }


def invalid_input_result(errors: list[str], *, failure_category: str = "invalid_input") -> dict[str, Any]:
    return _result(
        draft_status="not_started",
        source_status=failure_category,
        awaiting_review=False,
        review_status="blocked",
        failure_category=failure_category,
        blocking_reason="Mission Control sent an invalid apply-draft payload.",
        errors=errors,
    )


def read_payload(input_json_file: str | None = None) -> dict[str, Any]:
    raw = Path(input_json_file).read_text(encoding="utf-8") if input_json_file else sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid_json:{exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError("payload_must_be_object")
    return payload


def _as_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _text(value: Any) -> str:
    return str(value or "").strip()


def _dedupe(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        trimmed = _text(value)
        if not trimmed or trimmed in seen:
            continue
        seen.add(trimmed)
        output.append(trimmed)
    return output


def _combine_text(*values: str | None) -> str:
    return " ".join(_text(value) for value in values if _text(value)).lower()


@dataclass(frozen=True)
class SnapshotRef:
    ref: str
    label: str
    field_type: str | None
    raw_line: str
    context_text: str = ""


@dataclass(frozen=True)
class ContactFieldCandidate:
    ref: SnapshotRef
    field_name: str
    field_type: str
    label: str
    prefilled: bool
    required: bool


@dataclass(frozen=True)
class ContactFieldAction:
    candidate: ContactFieldCandidate
    action: str
    value: str | bool | None = None
    reason: str | None = None


@dataclass(frozen=True)
class BrowserRuntimeConfig:
    command: str
    run_on_host: bool
    attach_mode: bool
    skip_browser_start: bool
    allow_browser_start: bool
    gateway_url: str | None
    cdp_url: str | None
    gateway_token_present: bool
    host_gateway_alias: str
    running_in_docker: bool


class BrowserCommandError(RuntimeError):
    def __init__(
        self,
        *,
        failure_category: str,
        blocking_reason: str,
        errors: list[str] | None = None,
        safe_to_retry: bool = False,
        stage: str | None = None,
        error_kind: str | None = None,
        command_debug: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(blocking_reason)
        self.failure_category = failure_category
        self.blocking_reason = blocking_reason
        self.errors = errors or []
        self.safe_to_retry = safe_to_retry
        self.stage = stage
        self.error_kind = error_kind
        self.command_debug = command_debug or {}


def _looks_like_running_in_docker() -> bool:
    return Path("/.dockerenv").exists() or _as_bool(os.getenv("OPENCLAW_APPLY_RUNNING_IN_DOCKER"), default=False)


def _container_safe_url(raw_url: str | None, *, host_gateway_alias: str, running_in_docker: bool) -> str | None:
    url_text = _text(raw_url)
    if not url_text:
        return None
    if not running_in_docker:
        return url_text
    parsed = urlparse(url_text)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        return url_text
    netloc = host_gateway_alias
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    if parsed.username:
        credentials = parsed.username
        if parsed.password:
            credentials += f":{parsed.password}"
        netloc = f"{credentials}@{netloc}"
    return urlunparse(parsed._replace(netloc=netloc))


def _runtime_bool(payload_browser: dict[str, Any], key: str, env_key: str, *, default: bool | None = None) -> bool | None:
    payload_value = _as_bool(payload_browser.get(key), default=False) if key in payload_browser else None
    if payload_value is not None:
        return payload_value
    return _as_bool(os.getenv(env_key), default=default)


def _take_flag_value(parts: list[str], index: int) -> tuple[str | None, int]:
    part = parts[index]
    if "=" in part:
        return part.split("=", 1)[1], 1
    next_index = index + 1
    if next_index < len(parts):
        return parts[next_index], 2
    return None, 1


def _normalize_browser_base_command(command_text: str, *, gateway_url: str | None, gateway_token: str | None) -> str:
    configured_parts = [part for part in shlex.split(command_text) if part.strip()]
    if not configured_parts:
        configured_parts = [part for part in shlex.split(DEFAULT_BROWSER_COMMAND) if part.strip()]

    executable = configured_parts[0]
    remainder = configured_parts[1:]
    browser_index = remainder.index("browser") if "browser" in remainder else -1
    top_level_parts = remainder[:browser_index] if browser_index >= 0 else remainder
    browser_parts = remainder[browser_index + 1 :] if browser_index >= 0 else []

    moved_browser_parts: list[str] = []
    preserved_top_level_parts: list[str] = []
    index = 0
    while index < len(top_level_parts):
        part = top_level_parts[index]
        if part.startswith("--url"):
            value, consumed = _take_flag_value(top_level_parts, index)
            if value is not None:
                moved_browser_parts.extend(["--url", value])
            else:
                moved_browser_parts.append(part)
            index += consumed
            continue
        if part.startswith("--token"):
            value, consumed = _take_flag_value(top_level_parts, index)
            if value is not None:
                moved_browser_parts.extend(["--token", value])
            else:
                moved_browser_parts.append(part)
            index += consumed
            continue
        preserved_top_level_parts.append(part)
        index += 1

    normalized_browser_parts = [*moved_browser_parts, *browser_parts]

    def _strip_flag(parts: list[str], flag: str) -> list[str]:
        output: list[str] = []
        index = 0
        while index < len(parts):
            part = parts[index]
            if part == flag:
                index += 2
                continue
            if part.startswith(f"{flag}="):
                index += 1
                continue
            output.append(part)
            index += 1
        return output

    if gateway_url:
        normalized_browser_parts = _strip_flag(normalized_browser_parts, "--url")
        normalized_browser_parts = ["--url", gateway_url, *normalized_browser_parts]
    if gateway_token:
        normalized_browser_parts = _strip_flag(normalized_browser_parts, "--token")
        insert_at = 2 if gateway_url else 0
        normalized_browser_parts = [
            *normalized_browser_parts[:insert_at],
            "--token",
            gateway_token,
            *normalized_browser_parts[insert_at:],
        ]

    parts = [executable, *preserved_top_level_parts, "browser", *normalized_browser_parts]
    return " ".join(shlex.quote(part) for part in parts if part)


def _resolve_runtime_config(payload: dict[str, Any]) -> BrowserRuntimeConfig:
    payload_browser = payload.get("browser") if isinstance(payload.get("browser"), dict) else {}
    running_in_docker = _looks_like_running_in_docker()
    host_gateway_alias = _text(payload_browser.get("host_gateway_alias") or os.getenv("OPENCLAW_APPLY_HOST_GATEWAY_ALIAS")) or DEFAULT_HOST_GATEWAY_ALIAS
    run_on_host = bool(_runtime_bool(payload_browser, "run_on_host", "OPENCLAW_APPLY_RUN_ON_HOST", default=False))
    attach_mode = bool(_runtime_bool(payload_browser, "attach_mode", "OPENCLAW_APPLY_BROWSER_ATTACH_MODE", default=False))
    skip_browser_start = _runtime_bool(payload_browser, "skip_browser_start", "OPENCLAW_APPLY_SKIP_BROWSER_START")
    if skip_browser_start is None:
        skip_browser_start = attach_mode
    allow_browser_start = _runtime_bool(payload_browser, "allow_browser_start", "OPENCLAW_APPLY_ALLOW_BROWSER_START")
    if allow_browser_start is None:
        allow_browser_start = not attach_mode

    raw_gateway_url = _text(payload_browser.get("gateway_url") or os.getenv("OPENCLAW_APPLY_GATEWAY_URL"))
    raw_cdp_url = _text(payload_browser.get("cdp_url") or os.getenv("OPENCLAW_APPLY_CDP_URL"))
    gateway_url = raw_gateway_url if run_on_host else _container_safe_url(
        raw_gateway_url,
        host_gateway_alias=host_gateway_alias,
        running_in_docker=running_in_docker,
    )
    cdp_url = raw_cdp_url if run_on_host else _container_safe_url(
        raw_cdp_url,
        host_gateway_alias=host_gateway_alias,
        running_in_docker=running_in_docker,
    )
    gateway_token = _text(payload_browser.get("gateway_token") or os.getenv("OPENCLAW_APPLY_GATEWAY_TOKEN") or os.getenv("OPENCLAW_BROWSER_GATEWAY_TOKEN"))

    explicit_command = _text(payload_browser.get("command"))
    configured_command = _text(os.getenv("OPENCLAW_BROWSER_BASE_COMMAND"))
    command = _normalize_browser_base_command(
        explicit_command or configured_command or DEFAULT_BROWSER_COMMAND,
        gateway_url=gateway_url,
        gateway_token=gateway_token,
    )

    return BrowserRuntimeConfig(
        command=command,
        run_on_host=run_on_host,
        attach_mode=attach_mode,
        skip_browser_start=bool(skip_browser_start),
        allow_browser_start=bool(allow_browser_start),
        gateway_url=gateway_url,
        cdp_url=cdp_url,
        gateway_token_present=bool(gateway_token),
        host_gateway_alias=host_gateway_alias,
        running_in_docker=running_in_docker,
    )


def _redact_command(parts: list[str]) -> list[str]:
    redacted: list[str] = []
    skip_next = False
    for index, part in enumerate(parts):
        if skip_next:
            redacted.append("<redacted>")
            skip_next = False
            continue
        if part == "--token":
            redacted.append(part)
            skip_next = True
            continue
        if part.startswith("--token="):
            redacted.append("--token=<redacted>")
            continue
        if index > 0 and parts[index - 1] == "--token":
            redacted.append("<redacted>")
            continue
        redacted.append(part)
    return redacted


def _classify_failure_kind(stage: str | None, stdout: str, stderr: str) -> str:
    combined = _combine_text(stdout, stderr)
    if (
        "pairing required" in combined
        or "gateway connect failed" in combined
        or "gateway closed" in combined
        or "gateway target:" in combined
        or "econnrefused" in combined
        or "connection refused" in combined
        or "name or service not known" in combined
        or "temporary failure in name resolution" in combined
    ):
        return "gateway_connectivity_failure"
    if (
        "cdp" in combined
        or "devtools" in combined
        or "9222" in combined
        or ("attach" in combined and "chrome" in combined)
        or ("attach" in combined and "browser" in combined)
    ):
        return "cdp_attach_failure"
    if stage == "browser_start":
        return "browser_start_failure"
    if stage and stage.startswith("screenshot"):
        return "screenshot_failure"
    return "navigation_failure"


def _browser_command_failure(
    *,
    stage: str,
    stdout: str,
    stderr: str,
    returncode: int | None,
    timed_out: bool,
    command_debug: dict[str, Any],
) -> BrowserCommandError:
    if timed_out:
        return BrowserCommandError(
            failure_category="timed_out",
            blocking_reason="OpenClaw browser command timed out.",
            errors=["openclaw_browser_command_timeout"],
            safe_to_retry=True,
            stage=stage,
            error_kind="command_timeout",
            command_debug=command_debug,
        )
    failure_kind = _classify_failure_kind(stage, stdout, stderr)
    if failure_kind == "gateway_connectivity_failure":
        return BrowserCommandError(
            failure_category="manual_review_required",
            blocking_reason="OpenClaw browser gateway is not paired or reachable from the worker.",
            errors=["openclaw_browser_gateway_unavailable"],
            safe_to_retry=False,
            stage=stage,
            error_kind=failure_kind,
            command_debug=command_debug,
        )
    if failure_kind == "cdp_attach_failure":
        return BrowserCommandError(
            failure_category="manual_review_required",
            blocking_reason="OpenClaw could not attach to the configured Chrome CDP endpoint.",
            errors=["openclaw_browser_cdp_attach_failed"],
            safe_to_retry=False,
            stage=stage,
            error_kind=failure_kind,
            command_debug=command_debug,
        )
    return BrowserCommandError(
        failure_category="navigation_failed",
        blocking_reason=f"OpenClaw browser command failed: {' '.join(command_debug.get('args') or []) or stage or 'unknown'}",
        errors=[f"openclaw_browser_command_failed:{returncode or 'unknown'}"],
        safe_to_retry=True,
        stage=stage,
        error_kind=failure_kind,
        command_debug=command_debug,
    )


class OpenClawBrowserClient:
    """Thin wrapper around `openclaw browser` commands.

    The backend stays conservative: it only opens, inspects, uploads, fills,
    screenshots, and evaluates state. It never clicks submit-like buttons.
    """

    def __init__(self, *, command: str, timeout_ms: int, logger: logging.Logger) -> None:
        self._command = [part for part in shlex.split(command) if part.strip()]
        self._timeout_ms = timeout_ms
        self._logger = logger
        self._command_debug: list[dict[str, Any]] = []

    def command_debug(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._command_debug]

    def _run(self, *args: str, stage: str, timeout_ms: int | None = None) -> str:
        if not self._command:
            raise BrowserCommandError(
                failure_category="tool_unavailable",
                blocking_reason="OPENCLAW_BROWSER_BASE_COMMAND is empty.",
                errors=["openclaw_browser_command_missing"],
                stage=stage,
                error_kind="tool_unavailable",
                command_debug={
                    "stage": stage,
                    "args": list(args),
                    "command": [],
                    "exit_code": None,
                    "stdout": "",
                    "stderr": "",
                    "timed_out": False,
                },
            )
        command = [*self._command, *args]
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=((timeout_ms or self._timeout_ms) / 1000.0),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = str(exc.stdout or "").strip()
            stderr = str(exc.stderr or "").strip()
            command_debug = {
                "stage": stage,
                "args": list(args),
                "command": _redact_command(command),
                "exit_code": None,
                "stdout": stdout,
                "stderr": stderr,
                "timed_out": True,
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
            self._command_debug.append(command_debug)
            raise _browser_command_failure(
                stage=stage,
                stdout=stdout,
                stderr=stderr,
                returncode=None,
                timed_out=True,
                command_debug=command_debug,
            ) from exc
        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()
        command_debug = {
            "stage": stage,
            "args": list(args),
            "command": _redact_command(command),
            "exit_code": completed.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": False,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        if completed.returncode != 0:
            failure = _browser_command_failure(
                stage=stage,
                stdout=stdout,
                stderr=stderr,
                returncode=completed.returncode,
                timed_out=False,
                command_debug=command_debug,
            )
            command_debug["failure_kind"] = failure.error_kind
            command_debug["failure_category"] = failure.failure_category
            self._command_debug.append(command_debug)
            raise failure
        self._command_debug.append(command_debug)
        return stdout

    def start(self) -> None:
        self._run("start", stage="browser_start")

    def status(self) -> str:
        return self._run("status", stage="probe_status")

    def tabs(self) -> str:
        return self._run("tabs", stage="probe_tabs")

    def open(self, url: str) -> None:
        self._run("open", url, stage="navigate_open")

    def click(self, ref: str) -> None:
        self._run("click", ref, stage="click_ref")

    def wait_for_load(self, load_state: str) -> None:
        self._run("wait", "--load", load_state, stage=f"wait_{load_state}")

    def snapshot(self) -> str:
        with tempfile.NamedTemporaryFile(prefix="openclaw-snapshot-", suffix=".txt", delete=False) as handle:
            snapshot_path = Path(handle.name)
        try:
            self._run("snapshot", "--format", "ai", "--limit", "600", "--out", str(snapshot_path), stage="snapshot")
            return snapshot_path.read_text(encoding="utf-8", errors="replace")
        finally:
            snapshot_path.unlink(missing_ok=True)

    def screenshot(self, destination: Path) -> Path:
        output = self._run("screenshot", "--full-page", stage=f"screenshot_{destination.stem}")
        match = re.search(r"MEDIA:(?P<path>\S+)", output)
        if not match:
            raise BrowserCommandError(
                failure_category="navigation_failed",
                blocking_reason="OpenClaw browser screenshot did not return a MEDIA path.",
                errors=["openclaw_browser_screenshot_missing_media_path"],
                stage=f"screenshot_{destination.stem}",
                error_kind="screenshot_failure",
            )
        source = Path(match.group("path")).expanduser()
        if not source.exists():
            raise BrowserCommandError(
                failure_category="navigation_failed",
                blocking_reason="OpenClaw browser screenshot path does not exist.",
                errors=["openclaw_browser_screenshot_missing_file"],
                stage=f"screenshot_{destination.stem}",
                error_kind="screenshot_failure",
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return destination

    def evaluate_json(self, fn_source: str) -> Any:
        output = self._run("evaluate", "--fn", fn_source, stage="evaluate_json")
        cleaned = output.strip()
        if cleaned.startswith('"') and cleaned.endswith('"'):
            cleaned = json.loads(cleaned)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return cleaned

    def upload(self, staged_path: Path, *, input_ref: str | None = None) -> None:
        command = ["upload", str(staged_path)]
        if input_ref:
            command.extend(["--input-ref", input_ref])
        else:
            command.extend(["--element", "input[type=file]"])
        self._run(*command, stage="upload_resume", timeout_ms=max(self._timeout_ms, 120_000))

    def fill(self, fields: list[dict[str, Any]]) -> None:
        with tempfile.NamedTemporaryFile(prefix="openclaw-fill-", suffix=".json", mode="w", encoding="utf-8", delete=False) as handle:
            json.dump(fields, handle, ensure_ascii=True)
            handle.flush()
            fields_path = Path(handle.name)
        try:
            self._run("fill", "--fields-file", str(fields_path), stage="fill_fields")
        finally:
            fields_path.unlink(missing_ok=True)

    def select(self, ref: str, value: str) -> None:
        self._run("select", ref, value, stage="select_field")


def _configure_logger() -> logging.Logger:
    logger = logging.getLogger("openclaw_apply_browser_backend")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(getattr(logging, _text(os.getenv("OPENCLAW_APPLY_LOG_LEVEL")).upper() or "INFO", logging.INFO))
    logger.propagate = False
    return logger


def _classify_snapshot_field_type(label: str) -> str | None:
    lower = label.lower()
    if any(token in lower for token in ("fieldset", "legend", "group")):
        return "group"
    if "radio" in lower:
        return "radio"
    if "checkbox" in lower:
        return "checkbox"
    if any(token in lower for token in ("option", "menuitem", "listitem")):
        return "option"
    if "button" in lower and not any(token in lower for token in ("input", "field", "textbox", "textarea")):
        return "button"
    if "link" in lower and not any(token in lower for token in ("unlink",)):
        return "link"
    if "file" in lower or "upload" in lower or "attach" in lower:
        return "file"
    if any(token in lower for token in ("combobox", "select", "dropdown")):
        return "select"
    if any(token in lower for token in ("textarea", "textbox", "input", "field")):
        return "text"
    return None


def _extract_visible_label(label: str) -> str:
    quoted = re.search(r'"(?P<quoted>[^"]+)"', label)
    if quoted:
        return _text(quoted.group("quoted"))
    cleaned = re.sub(
        r"^(textbox|textarea|input|field|combobox|select|dropdown|radio|checkbox|group|button|link)\s+",
        "",
        label,
        flags=re.IGNORECASE,
    )
    return _text(cleaned)


def _normalize_label_text(label: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", _extract_visible_label(label).lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _label_contains_phrase(label: str, phrase: str) -> bool:
    normalized_label = _normalize_label_text(label)
    normalized_phrase = _normalize_label_text(phrase)
    if not normalized_label or not normalized_phrase:
        return False
    return bool(re.search(rf"(?:^| ){re.escape(normalized_phrase)}(?: |$)", normalized_label))


def _ref_search_text(ref: SnapshotRef) -> str:
    return " ".join(part for part in (ref.context_text, ref.label, ref.raw_line) if _text(part))


def _snapshot_ref_prefilled(ref: SnapshotRef) -> bool:
    lower = ref.raw_line.lower()
    if any(token in lower for token in (" selected", " filled", " checked")):
        return True
    if ":" not in ref.raw_line:
        return False
    _, _, remainder = ref.raw_line.partition(":")
    return bool(_text(remainder))


def _parse_snapshot_refs(snapshot_text: str) -> list[SnapshotRef]:
    refs: list[SnapshotRef] = []
    seen_refs: set[str] = set()
    context_stack: list[tuple[int, str]] = []
    patterns = [
        re.compile(
            r"^\s*-\s*(?P<label>.+?)\s*(?:\[[^\]]+\]\s*)*\[ref=(?P<ref>[A-Za-z0-9._:-]+)\](?:\s*\[[^\]]+\]\s*)*:?(?P<tail>.*)$"
        ),
        re.compile(r"^\s*\[(?P<ref>[A-Za-z0-9._:-]+)\]\s*(?P<label>.+)$"),
        re.compile(r"\bref(?:erence)?[:=#]?\s*(?P<ref>[A-Za-z0-9._:-]+)\b.*?(?P<label>[A-Za-z].+)"),
        re.compile(r"^\s*(?P<ref>[A-Za-z0-9._:-]+)[\].:\-)\s]+\s*(?P<label>.+)$"),
    ]
    for raw_line in snapshot_text.splitlines():
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if not line:
            continue
        while context_stack and context_stack[-1][0] >= indent:
            context_stack.pop()
        if line.startswith("- "):
            context_label = _text(re.sub(r"\s*(?:\[[^\]]+\]\s*)+$", "", line[2:].rstrip(":")))
            if context_label and not context_label.lower().startswith(
                ("generic", "img", "listitem", "list", "note", "paragraph", "text:")
            ):
                context_stack.append((indent, context_label))
        for pattern in patterns:
            match = pattern.search(line)
            if not match:
                continue
            ref = match.group("ref")
            if not re.search(r"[A-Za-z0-9]", ref):
                break
            label = _text(match.group("label"))
            tail = _text(match.groupdict().get("tail"))
            if tail:
                label = f"{label} {tail}".strip()
            if ref in seen_refs:
                break
            field_type = _classify_snapshot_field_type(label)
            if field_type is None and not any(token in label.lower() for token in SNAPSHOT_CONTROL_HINTS):
                break
            context_text = " ".join(context for _, context in context_stack[:-1] if context)
            refs.append(SnapshotRef(ref=ref, label=label, field_type=field_type, raw_line=line))
            refs[-1] = SnapshotRef(ref=ref, label=label, field_type=field_type, raw_line=line, context_text=context_text)
            seen_refs.add(ref)
            break
    return refs


def _keyword_score(text: str, keywords: list[str]) -> int:
    haystack = text.lower()
    return sum(1 for keyword in keywords if keyword and keyword.lower() in haystack)


def _field_is_required(ref: SnapshotRef, canonical_key: str | None = None) -> bool:
    combined = _ref_search_text(ref).lower()
    if "*" in ref.raw_line or "*" in ref.label or " required" in combined:
        return True
    return bool(canonical_key and canonical_key in INFERRED_REQUIRED_CANONICAL_KEYS)


def _option_matches_desired_value(option_label: str, desired_value: str) -> bool:
    option_normalized = _normalize_label_text(option_label)
    desired_normalized = _normalize_label_text(desired_value)
    if not option_normalized or not desired_normalized:
        raw_option_normalized = re.sub(r"[^a-z0-9]+", " ", _text(option_label).lower()).strip()
        raw_desired_normalized = re.sub(r"[^a-z0-9]+", " ", _text(desired_value).lower()).strip()
        return bool(
            raw_option_normalized
            and raw_desired_normalized
            and (
                raw_desired_normalized in raw_option_normalized
                or raw_option_normalized in raw_desired_normalized
            )
        )
    aliases = {
        "yes": {"yes", "y", "true"},
        "no": {"no", "n", "false"},
        "male": {"male", "man"},
        "mobile": {"mobile", "cell", "cell phone"},
        "full time": {"full time", "full-time", "permanent"},
        "not a veteran": {"not a veteran", "not a protected veteran", "i am not a protected veteran", "no"},
        "no disability": {"no disability", "no, i do not have a disability", "no"},
        "white (not hispanic)": {"white", "white not hispanic", "not hispanic"},
        "prefer not to say": {
            "prefer not to say",
            "prefer not to disclose",
            "decline to answer",
            "decline to self identify",
            "choose not to disclose",
            "choose not to self identify",
            "do not wish to answer",
            "do not wish to self identify",
        },
        "i have not worked with a recruiter": {
            "i have not worked with a recruiter",
            "have not worked with a recruiter",
            "not worked with a recruiter",
            "no",
            "none",
        },
        "none": {
            "none",
            "no",
            "n a",
            "not applicable",
            "no active clearance",
            "no active polygraph",
            "no current clearance",
            "do not currently hold an active clearance",
        },
        "i agree": {
            "i agree",
            "agree",
            "yes",
            "confirm",
            "i have read and understand the above statement",
            "i have read and understand above",
            "read and understand above",
        },
    }
    desired_aliases = aliases.get(desired_normalized, {desired_normalized})
    if option_normalized in desired_aliases or desired_normalized in option_normalized or option_normalized in desired_normalized:
        return True
    raw_option_normalized = re.sub(r"[^a-z0-9]+", " ", _text(option_label).lower()).strip()
    raw_desired_normalized = re.sub(r"[^a-z0-9]+", " ", _text(desired_value).lower()).strip()
    return bool(
        raw_option_normalized
        and raw_desired_normalized
        and (
            raw_desired_normalized in raw_option_normalized
            or raw_option_normalized in raw_desired_normalized
        )
    )


def _looks_like_self_id_ref(ref: SnapshotRef) -> bool:
    combined = _normalize_label_text(_ref_search_text(ref))
    return any(token in combined for token in SELF_ID_HINT_TOKENS)


def _looks_like_known_question_ref(ref: SnapshotRef) -> bool:
    combined = _normalize_label_text(_ref_search_text(ref))
    return any(token in combined for token in KNOWN_QUESTION_HINT_TOKENS)


def _ref_prompt_label(ref: SnapshotRef) -> str:
    return _text(ref.context_text or _extract_visible_label(ref.label) or ref.label)


def _ref_question_group_key(ref: SnapshotRef) -> str:
    return _normalize_label_text(_ref_prompt_label(ref)) or _normalize_label_text(ref.label) or ref.ref


def _find_upload_ref(refs: list[SnapshotRef]) -> SnapshotRef | None:
    file_refs = [ref for ref in refs if ref.field_type == "file"]
    if not file_refs:
        return None
    ranked = sorted(
        file_refs,
        key=lambda ref: (
            2 if ref.field_type == "file" else 0,
            _keyword_score(ref.label, ["resume", "cv", "upload", "attach"]),
        ),
        reverse=True,
    )
    top = ranked[0] if ranked else None
    if top:
        return top
    return None


def _find_clickable_ref(
    refs: list[SnapshotRef],
    *,
    keywords: list[str],
    allowed_types: tuple[str, ...] = ("button", "link"),
    disallowed_keywords: list[str] | None = None,
) -> SnapshotRef | None:
    disallowed_keywords = disallowed_keywords or []
    ranked = sorted(
        (
            ref
            for ref in refs
            if ref.field_type in set(allowed_types)
            and _keyword_score(f"{ref.context_text} {ref.label}", keywords) > 0
            and _keyword_score(f"{ref.context_text} {ref.label}", disallowed_keywords) == 0
        ),
        key=lambda ref: (
            _keyword_score(f"{ref.context_text} {ref.label}", keywords),
            1 if ref.field_type == "button" else 0,
        ),
        reverse=True,
    )
    return ranked[0] if ranked else None


def _resume_label_from_text(text: str) -> str | None:
    visible = _text(_extract_visible_label(text))
    if not visible:
        return None
    normalized = _normalize_label_text(visible)
    if "deselect resume" in normalized:
        stripped = re.sub(r"(?i)^deselect\s+resume\s*", "", visible).strip(" :-")
        return _text(stripped) or visible
    file_match = re.search(r"([^\"]+\.(?:pdf|docx?|rtf))", visible, re.IGNORECASE)
    if file_match:
        return _text(file_match.group(1))
    if (
        "resume" in normalized
        and len(_tokenize(visible)) > 1
        and not any(token in normalized for token in ("upload resume", "attach resume"))
    ):
        return visible
    return None


def _selected_resume_diagnostics(snapshot_text: str, refs: list[SnapshotRef]) -> dict[str, Any]:
    lines = [line.strip() for line in snapshot_text.splitlines() if line.strip()]
    detected = False
    verified = False
    selected_label: str | None = None

    for ref in refs:
        if ref.field_type != "radio":
            continue
        combined = _normalize_label_text(_ref_search_text(ref))
        if "deselect resume" not in combined:
            continue
        detected = True
        selected_label = _resume_label_from_text(ref.label) or selected_label
        if "checked" in ref.raw_line.lower() or " selected" in ref.raw_line.lower():
            verified = True
            break

    if not verified:
        selected_line_indexes = [
            index
            for index, line in enumerate(lines)
            if re.search(r"(?:^| )selected(?: |$)", _normalize_label_text(line))
        ]
        if selected_line_indexes:
            detected = True
        for index in selected_line_indexes:
            window = lines[max(0, index - 3) : min(len(lines), index + 4)]
            for line in window:
                candidate_label = _resume_label_from_text(line)
                if not candidate_label:
                    continue
                if selected_label is None:
                    selected_label = candidate_label
                if candidate_label.lower().endswith((".pdf", ".docx", ".doc", ".rtf")) or "resume" in _normalize_label_text(
                    candidate_label
                ):
                    verified = True
                    selected_label = candidate_label
                    break
            if verified:
                break

    return {
        "selected_resume_detected": detected,
        "selected_resume_label": selected_label,
        "selected_resume_verified": verified,
    }


def _find_dropdown_option_ref(
    refs: list[SnapshotRef],
    *,
    desired_value: str,
    opener_ref: str | None = None,
    opener_label: str | None = None,
) -> SnapshotRef | None:
    opener_keywords = _tokenize(_text(opener_label))
    ranked = sorted(
        (
            ref
            for ref in refs
            if ref.ref != opener_ref
            and ref.field_type in {"option", "button", "link", "text"}
            and _option_matches_desired_value(ref.label, desired_value)
        ),
        key=lambda ref: (
            2 if ref.field_type == "option" else (1 if ref.field_type in {"button", "link"} else 0),
            _keyword_score(f"{ref.context_text} {ref.label}", opener_keywords),
        ),
        reverse=True,
    )
    return ranked[0] if ranked else None


def _safe_full_name(answer_profile: dict[str, Any]) -> str:
    first_name = _text(answer_profile.get("first_name"))
    last_name = _text(answer_profile.get("last_name"))
    full_name = " ".join(part for part in (first_name, last_name) if part).strip()
    return full_name or "Zachary Kralec"


def _is_personal_answer_key(canonical_key: str) -> bool:
    return canonical_key in {
        "veteran_status",
        "gender",
        "disability_status",
        "ethnicity",
        "race",
        "ethnicity_race",
        "sexual_orientation",
    }


def _linkedin_personal_fallback_value(canonical_key: str) -> str | None:
    fallbacks = {
        "race": "White",
        "ethnicity": "White",
        "ethnicity_race": "White",
        "gender": "Male",
        "sexual_orientation": "Straight",
        "disability_status": "No disability",
        "veteran_status": "Not a veteran",
    }
    return fallbacks.get(canonical_key)


def _visible_neutral_personal_option(grouped: list[tuple[SnapshotRef, dict[str, Any]]]) -> str | None:
    neutral_values = (
        "Prefer not to disclose",
        "Prefer not to say",
        "Choose not to self-identify",
        "Choose not to disclose",
        "Decline to answer",
        "Decline to self-identify",
    )
    for candidate in neutral_values:
        for ref, _ in grouped:
            if ref.field_type not in {"radio", "checkbox", "option"}:
                continue
            if _option_matches_desired_value(ref.label, candidate):
                return _extract_visible_label(ref.label) or candidate
    return None


def _linkedin_custom_mapping(ref: SnapshotRef, *, application_target: dict[str, Any]) -> dict[str, Any] | None:
    application_url = _text(application_target.get("application_url") or application_target.get("source_url"))
    if not _host(application_url).endswith("linkedin.com"):
        return None
    combined = _normalize_label_text(_ref_search_text(ref))
    if not combined:
        return None
    if "security clearance level" in combined or ("clearance" in combined and "level" in combined):
        return {
            "canonical_key": "security_clearance_level",
            "confidence": 0.96,
            "matched_phrase": "linkedin_security_clearance_level",
            "normalized_label": combined,
        }
    if "polygraph level" in combined or ("polygraph" in combined and "level" in combined):
        return {
            "canonical_key": "polygraph_level",
            "confidence": 0.96,
            "matched_phrase": "linkedin_polygraph_level",
            "normalized_label": combined,
        }
    if "recruiter" in combined and any(token in combined for token in ("working with", "worked with", "been working with")):
        return {
            "canonical_key": "worked_with_company_recruiter_before",
            "confidence": 0.96,
            "matched_phrase": "linkedin_recruiter_contact",
            "normalized_label": combined,
        }
    if "sexual orientation" in combined or "orientation" in combined:
        return {
            "canonical_key": "sexual_orientation",
            "confidence": 0.96,
            "matched_phrase": "linkedin_sexual_orientation",
            "normalized_label": combined,
        }
    if "learn about our company" in combined or "learn about the company" in combined:
        return {
            "canonical_key": "hear_about_us",
            "confidence": 0.96,
            "matched_phrase": "linkedin_hear_about_company",
            "normalized_label": combined,
        }
    if "full name" in combined:
        return {
            "canonical_key": "certification_full_name",
            "confidence": 0.96,
            "matched_phrase": "linkedin_certification_full_name",
            "normalized_label": combined,
        }
    if any(token in combined for token in ("today s date", "todays date")):
        return {
            "canonical_key": "certification_date",
            "confidence": 0.96,
            "matched_phrase": "linkedin_certification_date",
            "normalized_label": combined,
        }
    if any(token in combined for token in ("read and understand", "i agree", "agree", "acknowledge", "certify")):
        return {
            "canonical_key": "certification_confirmation",
            "confidence": 0.94,
            "matched_phrase": "linkedin_certification_confirmation",
            "normalized_label": combined,
        }
    return None


def _mapping_for_ref(ref: SnapshotRef, *, application_target: dict[str, Any]) -> dict[str, Any] | None:
    custom_mapping = _linkedin_custom_mapping(ref, application_target=application_target)
    if isinstance(custom_mapping, dict):
        return custom_mapping
    return normalize_canonical_key(ref.label, context_text=ref.context_text)


def _linkedin_top_choice_optional_step(snapshot_text: str) -> bool:
    combined = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", snapshot_text.lower())).strip()
    return "top choice" in combined and "optional" in combined


def _linkedin_follow_company_optional_refs(refs: list[SnapshotRef]) -> list[SnapshotRef]:
    matched: list[SnapshotRef] = []
    for ref in refs:
        if ref.field_type != "checkbox" or _field_is_required(ref):
            continue
        combined = _normalize_label_text(_ref_search_text(ref))
        if "follow" in combined and "company" in combined:
            matched.append(ref)
    return matched


def _snapshot_contains_option_text(snapshot_text: str, desired_value: str) -> bool:
    for line in snapshot_text.splitlines():
        if "option" not in line.lower():
            continue
        if _option_matches_desired_value(line, desired_value):
            return True
    return False


def _snapshot_contains_selected_value(snapshot_text: str, desired_value: str) -> bool:
    for line in snapshot_text.splitlines():
        normalized_line = _normalize_label_text(line)
        if not normalized_line or "selected" not in normalized_line:
            continue
        if _option_matches_desired_value(line, desired_value):
            return True
    return False


def _snapshot_refs_show_field_value(
    refs: list[SnapshotRef],
    *,
    field_name: str,
    desired_value: str,
) -> bool:
    for ref in refs:
        if _contact_field_name(ref) != field_name:
            continue
        if _option_matches_desired_value(_ref_search_text(ref), desired_value):
            return True
    return False


def _combobox_result_value_matches(result: Any, desired_value: str) -> bool:
    if not isinstance(result, dict):
        return False
    for key in ("activeValue", "value", "selectedValue", "matchedValue"):
        if _option_matches_desired_value(_text(result.get(key)), desired_value):
            return True
    return False


def _combobox_selection_diagnostics(
    *,
    evaluate_result: Any,
    snapshot_text: str,
    refs: list[SnapshotRef],
    actions: list[ContactFieldAction],
    field_name: str,
    desired_value: str,
) -> dict[str, Any]:
    active_value = _text(evaluate_result.get("activeValue")) if isinstance(evaluate_result, dict) else ""
    if _combobox_result_value_matches(evaluate_result, desired_value):
        return {
            "success": True,
            "evaluate_result_active_value": active_value,
            "success_evidence_used": "evaluate_result_active_value",
            "false_positive_prevented": False,
        }
    if _snapshot_contains_selected_value(snapshot_text, desired_value):
        return {
            "success": True,
            "evaluate_result_active_value": active_value,
            "success_evidence_used": "snapshot_selected_value",
            "false_positive_prevented": False,
        }
    if _snapshot_refs_show_field_value(refs, field_name=field_name, desired_value=desired_value):
        return {
            "success": True,
            "evaluate_result_active_value": active_value,
            "success_evidence_used": "refreshed_field_value",
            "false_positive_prevented": False,
        }
    if _contact_field_value_satisfied(actions, field_name=field_name, desired_value=desired_value):
        return {
            "success": True,
            "evaluate_result_active_value": active_value,
            "success_evidence_used": "planned_action_value",
            "false_positive_prevented": False,
        }
    return {
        "success": False,
        "evaluate_result_active_value": active_value,
        "success_evidence_used": None,
        "false_positive_prevented": bool(
            isinstance(evaluate_result, dict)
            and _as_bool(evaluate_result.get("ok"), default=False)
            and not _combobox_result_value_matches(evaluate_result, desired_value)
        ),
    }


def _contact_field_value_satisfied(
    actions: list[ContactFieldAction],
    *,
    field_name: str,
    desired_value: str,
) -> bool:
    for action in actions:
        if action.candidate.field_name != field_name:
            continue
        if action.action not in {"fill", "prefilled_verified"}:
            continue
        if action.candidate.field_type == "select":
            if _option_matches_desired_value(_ref_search_text(action.candidate.ref), desired_value):
                return True
            continue
        return True
    return False


def _combobox_keyboard_typeahead_script(desired_value: str) -> str:
    desired_json = json.dumps(desired_value, ensure_ascii=True)
    return (
        "() => JSON.stringify((() => {"
        f"const desired = {desired_json};"
        "const active = document.activeElement;"
        "if (!active || typeof active !== 'object') return {ok:false, reason:'no_active_element', strategy:'keyboard_typeahead'};"
        "try {"
        "  if (typeof active.focus === 'function') active.focus();"
        "  if ('value' in active) {"
        "    active.value = '';"
        "    active.dispatchEvent(new Event('input', {bubbles:true}));"
        "    for (const ch of desired) {"
        "      active.dispatchEvent(new KeyboardEvent('keydown', {key: ch, bubbles:true}));"
        "      active.value = String(active.value || '') + ch;"
        "      active.dispatchEvent(new Event('input', {bubbles:true}));"
        "      active.dispatchEvent(new KeyboardEvent('keyup', {key: ch, bubbles:true}));"
        "    }"
        "    active.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles:true}));"
        "    active.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', bubbles:true}));"
        "    active.dispatchEvent(new Event('change', {bubbles:true}));"
        "    return {ok:true, strategy:'keyboard_typeahead', activeValue:String(active.value || '')};"
        "  }"
        "  return {ok:false, reason:'active_element_not_value_capable', strategy:'keyboard_typeahead'};"
        "} catch (error) {"
        "  return {ok:false, reason:String(error), strategy:'keyboard_typeahead'};"
        "}"
        "})())"
    )


def _combobox_evaluate_selection_script(desired_value: str) -> str:
    desired_json = json.dumps(desired_value, ensure_ascii=True)
    return (
        "() => JSON.stringify((() => {"
        f"const desired = {desired_json};"
        "const norm = (value) => String(value || '').trim().toLowerCase();"
        "const active = document.activeElement;"
        "const target = active && typeof active === 'object' ? active : null;"
        "try {"
        "  const options = Array.from(document.querySelectorAll('option,[role=\"option\"]'));"
        "  const match = options.find((option) => norm(option.textContent) === norm(desired));"
        "  if (match) {"
        "    if ('selected' in match) match.selected = true;"
        "    match.dispatchEvent(new MouseEvent('click', {bubbles:true}));"
        "  }"
        "  if (target && 'value' in target) {"
        "    target.value = desired;"
        "    target.dispatchEvent(new Event('input', {bubbles:true}));"
        "    target.dispatchEvent(new Event('change', {bubbles:true}));"
        "    return {ok:true, strategy:'evaluate_selection', matchedOption: !!match, activeValue:String(target.value || '')};"
        "  }"
        "  return {ok:false, reason:'no_target', strategy:'evaluate_selection', matchedOption: !!match};"
        "} catch (error) {"
        "  return {ok:false, reason:String(error), strategy:'evaluate_selection'};"
        "}"
        "})())"
    )


def _linkedin_radio_groups_probe_script() -> str:
    return (
        "() => JSON.stringify((() => {"
        "const probeKind = '__openclaw_linkedin_radio_groups_probe__';"
        "const norm = (value) => String(value || '').trim().replace(/\\s+/g, ' ').toLowerCase();"
        "const visible = (node) => !!node && typeof node.getClientRects === 'function' && node.getClientRects().length > 0;"
        "const optionLabel = (input) => {"
        "  if (!input) return '';"
        "  const push = [];"
        "  const add = (value) => { const text = String(value || '').trim(); if (text) push.push(text); };"
        "  if (input.id) {"
        "    const explicit = document.querySelector(`label[for=\"${CSS.escape(input.id)}\"]`);"
        "    if (explicit) {"
        "      add(explicit.getAttribute('data-test-text-selectable-option__label'));"
        "      add(explicit.textContent);"
        "    }"
        "  }"
        "  const closestLabel = input.closest('label');"
        "  if (closestLabel) {"
        "    add(closestLabel.getAttribute('data-test-text-selectable-option__label'));"
        "    add(closestLabel.textContent);"
        "  }"
        "  add(input.getAttribute('aria-label'));"
        "  add(input.getAttribute('data-test-text-selectable-option__input'));"
        "  return push.find(Boolean) || '';"
        "};"
        "const selectedMarker = (input) => {"
        "  if (!input) return false;"
        "  if (input.checked) return true;"
        "  const optionRoot = input.closest('[data-test-text-selectable-option]') || input.parentElement;"
        "  if (!optionRoot) return false;"
        "  const attrs = ["
        "    optionRoot.getAttribute('aria-checked'),"
        "    optionRoot.getAttribute('data-test-selected'),"
        "    optionRoot.getAttribute('data-selected'),"
        "    optionRoot.getAttribute('aria-selected')"
        "  ].map((value) => norm(value));"
        "  if (attrs.some((value) => value === 'true' || value === 'checked' || value === 'selected')) return true;"
        "  const classes = norm(optionRoot.className);"
        "  return classes.includes('selected') || classes.includes('checked');"
        "};"
        "const inferFieldName = (groupLabel, options, inputs) => {"
        "  const labelNorm = norm(groupLabel);"
        "  const optionNorms = options.map((option) => norm(option.label));"
        "  const phoneTypeTokens = ['mobile', 'home', 'work', 'other', 'cell'];"
        "  const dashlane = inputs.map((input) => norm(input.getAttribute('data-dashlane-classification'))).join(' ');"
        "  if ((labelNorm.includes('type') || dashlane.includes('phone')) && optionNorms.some((value) => phoneTypeTokens.includes(value))) {"
        "    return 'phone_type';"
        "  }"
        "  return '';"
        "};"
        "try {"
        "  const groups = Array.from(document.querySelectorAll('fieldset')).map((fieldset) => {"
        "    const inputs = Array.from(fieldset.querySelectorAll('input[type=\"radio\"]')).filter(visible);"
        "    if (!inputs.length) return null;"
        "    const legend = fieldset.querySelector('legend');"
        "    const labelText = String((legend && legend.textContent) || fieldset.getAttribute('aria-label') || '').trim();"
        "    const options = inputs.map((input) => ({"
        "      label: optionLabel(input),"
        "      inputId: String(input.id || ''),"
        "      inputName: String(input.name || ''),"
        "      checked: !!input.checked,"
        "      selectedMarker: selectedMarker(input)"
        "    }));"
        "    const fieldName = inferFieldName(labelText, options, inputs);"
        "    const required = norm(labelText).includes('required') || labelText.includes('*') || inputs.some((input) => !!input.required || norm(input.getAttribute('aria-required')) === 'true');"
        "    const selected = options.find((option) => option.checked || option.selectedMarker) || null;"
        "    return {"
        "      field_name: fieldName,"
        "      group_label: labelText,"
        "      required,"
        "      options: options.map((option) => option.label).filter(Boolean),"
        "      selected_option: selected ? selected.label : null,"
        "      selection_verified: !!selected && !!selected.checked,"
        "      chosen_option: selected ? selected.label : null,"
        "      refs_involved: options.map((option) => option.inputId || option.inputName).filter(Boolean),"
        "      option_details: options"
        "    };"
        "  }).filter(Boolean);"
        "  return { probeKind, groups };"
        "} catch (error) {"
        "  return { probeKind, error: String(error), groups: [] };"
        "}"
        "})())"
    )


def _linkedin_radio_group_select_script(field_name: str, option_label: str) -> str:
    field_name_json = json.dumps(field_name, ensure_ascii=True)
    option_json = json.dumps(option_label, ensure_ascii=True)
    return (
        "() => JSON.stringify((() => {"
        "const probeKind = '__openclaw_linkedin_radio_group_select__';"
        f"const targetField = {field_name_json};"
        f"const targetOption = {option_json};"
        "const norm = (value) => String(value || '').trim().replace(/\\s+/g, ' ').toLowerCase();"
        "const visible = (node) => !!node && typeof node.getClientRects === 'function' && node.getClientRects().length > 0;"
        "const optionLabel = (input) => {"
        "  if (!input) return '';"
        "  const values = [];"
        "  const add = (value) => { const text = String(value || '').trim(); if (text) values.push(text); };"
        "  if (input.id) {"
        "    const explicit = document.querySelector(`label[for=\"${CSS.escape(input.id)}\"]`);"
        "    if (explicit) {"
        "      add(explicit.getAttribute('data-test-text-selectable-option__label'));"
        "      add(explicit.textContent);"
        "    }"
        "  }"
        "  const closestLabel = input.closest('label');"
        "  if (closestLabel) {"
        "    add(closestLabel.getAttribute('data-test-text-selectable-option__label'));"
        "    add(closestLabel.textContent);"
        "  }"
        "  add(input.getAttribute('aria-label'));"
        "  add(input.getAttribute('data-test-text-selectable-option__input'));"
        "  return values.find(Boolean) || '';"
        "};"
        "const selectedMarker = (input) => {"
        "  if (!input) return false;"
        "  if (input.checked) return true;"
        "  const optionRoot = input.closest('[data-test-text-selectable-option]') || input.parentElement;"
        "  if (!optionRoot) return false;"
        "  const attrs = ["
        "    optionRoot.getAttribute('aria-checked'),"
        "    optionRoot.getAttribute('data-test-selected'),"
        "    optionRoot.getAttribute('data-selected'),"
        "    optionRoot.getAttribute('aria-selected')"
        "  ].map((value) => norm(value));"
        "  if (attrs.some((value) => value === 'true' || value === 'checked' || value === 'selected')) return true;"
        "  const classes = norm(optionRoot.className);"
        "  return classes.includes('selected') || classes.includes('checked');"
        "};"
        "const fieldsets = Array.from(document.querySelectorAll('fieldset')).filter((fieldset) => Array.from(fieldset.querySelectorAll('input[type=\"radio\"]')).some(visible));"
        "const matchFieldset = fieldsets.find((fieldset) => {"
        "  const legend = fieldset.querySelector('legend');"
        "  const label = String((legend && legend.textContent) || fieldset.getAttribute('aria-label') || '').trim();"
        "  const inputs = Array.from(fieldset.querySelectorAll('input[type=\"radio\"]')).filter(visible);"
        "  const optionNorms = inputs.map((input) => norm(optionLabel(input)));"
        "  const labelNorm = norm(label);"
        "  if (targetField === 'phone_type') {"
        "    return labelNorm.includes('type') && optionNorms.some((value) => ['mobile', 'home', 'work', 'other', 'cell'].includes(value));"
        "  }"
        "  return labelNorm.includes(norm(targetField));"
        "}) || null;"
        "if (!matchFieldset) return { probeKind, found: false, selection_attempted: false, selection_verified: false };"
        "const radios = Array.from(matchFieldset.querySelectorAll('input[type=\"radio\"]')).filter(visible);"
        "const chosen = radios.find((input) => norm(optionLabel(input)) === norm(targetOption)) || null;"
        "if (!chosen) return { probeKind, found: true, selection_attempted: false, selection_verified: false };"
        "const chosenLabel = chosen.id ? document.querySelector(`label[for=\"${CSS.escape(chosen.id)}\"]`) : chosen.closest('label');"
        "if (!chosen.checked) {"
        "  chosen.click();"
        "  chosen.dispatchEvent(new Event('input', { bubbles: true }));"
        "  chosen.dispatchEvent(new Event('change', { bubbles: true }));"
        "}"
        "if (!chosen.checked && chosenLabel) {"
        "  chosenLabel.click();"
        "}"
        "const verified = !!chosen.checked || selectedMarker(chosen);"
        "const legend = matchFieldset.querySelector('legend');"
        "return {"
        "  probeKind,"
        "  found: true,"
        "  field_name: targetField,"
        "  group_label: String((legend && legend.textContent) || matchFieldset.getAttribute('aria-label') || '').trim(),"
        "  selection_attempted: true,"
        "  selection_verified: verified,"
        "  chosen_option: optionLabel(chosen),"
        "  selected_option: verified ? optionLabel(chosen) : null,"
        "  refs_involved: [String(chosen.id || ''), String(chosen.name || '')].filter(Boolean)"
        "};"
        "})())"
    )


def _native_select_probe_script(field_label: str, desired_value: str) -> str:
    label_json = json.dumps(field_label, ensure_ascii=True)
    desired_json = json.dumps(desired_value, ensure_ascii=True)
    return (
        "() => JSON.stringify((() => {"
        "const probeKind = '__openclaw_native_select_probe__';"
        f"const desired = {desired_json};"
        f"const fieldLabel = {label_json};"
        "const norm = (value) => String(value || '').trim().replace(/\\s+/g, ' ').toLowerCase();"
        "const visible = (node) => !!node && typeof node.getClientRects === 'function' && node.getClientRects().length > 0;"
        "const labelText = (select) => {"
        "  const texts = [];"
        "  const push = (value) => { const text = String(value || '').trim(); if (text) texts.push(text); };"
        "  push(select.getAttribute('aria-label'));"
        "  push(select.name);"
        "  push(select.id);"
        "  if (select.labels) { for (const label of Array.from(select.labels)) push(label.textContent); }"
        "  if (select.id) {"
        "    const byFor = document.querySelector(`label[for=\"${CSS.escape(select.id)}\"]`);"
        "    if (byFor) push(byFor.textContent);"
        "  }"
        "  return texts.join(' ').trim();"
        "};"
        "try {"
        "  const desiredNorm = norm(desired);"
        "  const fieldNorm = norm(fieldLabel);"
        "  const candidates = Array.from(document.querySelectorAll('select')).filter(visible).map((select) => {"
        "    const text = labelText(select);"
        "    const textNorm = norm(text);"
        "    const options = Array.from(select.options || []).map((option) => ({"
        "      value: String(option.value || ''),"
        "      label: String(option.textContent || '').trim(),"
        "      valueNorm: norm(option.value),"
        "      labelNorm: norm(option.textContent),"
        "    }));"
        "    const matchedOption = options.find((option) => option.valueNorm === desiredNorm || option.labelNorm === desiredNorm)"
        "      || options.find((option) => desiredNorm && (option.valueNorm.includes(desiredNorm) || option.labelNorm.includes(desiredNorm)));"
        "    let score = 0;"
        "    if (fieldNorm && (textNorm.includes(fieldNorm) || fieldNorm.includes(textNorm))) score += 10;"
        "    if (desiredNorm && matchedOption) score += 5;"
        "    if (textNorm.includes('country')) score += 3;"
        "    return {"
        "      labelText: text,"
        "      score,"
        "      optionCount: options.length,"
        "      matchedOptionValue: matchedOption ? matchedOption.value : '',"
        "      matchedOptionLabel: matchedOption ? matchedOption.label : '',"
        "    };"
        "  }).sort((a, b) => b.score - a.score || b.optionCount - a.optionCount);"
        "  const best = candidates[0] || null;"
        "  if (!best || best.score <= 0) return {probeKind, isNativeSelect:false, detectedFieldType:'combobox'};"
        "  return {"
        "    probeKind,"
        "    isNativeSelect:true,"
        "    detectedFieldType:'select',"
        "    optionCount: best.optionCount,"
        "    matchedOptionValue: best.matchedOptionValue,"
        "    matchedOptionLabel: best.matchedOptionLabel,"
        "    fieldLabelText: best.labelText,"
        "  };"
        "} catch (error) {"
        "  return {probeKind, isNativeSelect:false, detectedFieldType:'combobox', error:String(error)};"
        "}"
        "})())"
    )


def _normalize_select_attempt_value(desired_value: str, probe_result: Any) -> str:
    normalized_desired = _text(desired_value)
    if not isinstance(probe_result, dict):
        return normalized_desired
    matched_value = _text(probe_result.get("matchedOptionValue"))
    matched_label = _text(probe_result.get("matchedOptionLabel"))
    if matched_value:
        return matched_value
    if matched_label:
        return matched_label
    upper_value = normalized_desired.upper()
    return upper_value if upper_value else normalized_desired


def _detected_select_field_type(probe_result: Any) -> str:
    if isinstance(probe_result, dict) and _text(probe_result.get("detectedFieldType")) == "select":
        return "select"
    if isinstance(probe_result, dict) and _as_bool(probe_result.get("isNativeSelect"), default=False):
        return "select"
    return "combobox"


def _tokenize(text: str) -> list[str]:
    return [token for token in re.split(r"[^a-z0-9]+", text.lower()) if len(token) >= 3]


def _find_text_ref(refs: list[SnapshotRef], *, keywords: list[str], used_refs: set[str]) -> SnapshotRef | None:
    ranked = sorted(
        (ref for ref in refs if ref.ref not in used_refs and ref.field_type == "text"),
        key=lambda ref: (
            _keyword_score(f"{ref.context_text} {ref.label}", keywords),
            1 if "textarea" in ref.label.lower() or "textbox" in ref.label.lower() else 0,
        ),
        reverse=True,
    )
    top = ranked[0] if ranked else None
    if top and _keyword_score(f"{top.context_text} {top.label}", keywords) > 0:
        return top
    return None


def _find_generic_text_ref(refs: list[SnapshotRef], used_refs: set[str]) -> SnapshotRef | None:
    for ref in refs:
        lower = ref.label.lower()
        if ref.ref in used_refs or ref.field_type != "text":
            continue
        if any(token in lower for token in ("button", "submit", "apply", "search", "filter")):
            continue
        if any(token in lower for token in ("textarea", "textbox", "input", "field")):
            return ref
    return None


def _extract_contact_values(payload: dict[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}

    def assign(name: str, raw_value: Any) -> None:
        text = _text(raw_value)
        if text and not values.get(name):
            values[name] = text

    def assign_many(source: Any) -> None:
        if not isinstance(source, dict):
            return
        alias_map = {
            "first_name": ("first_name", "given_name"),
            "last_name": ("last_name", "family_name", "surname"),
            "email_address": ("email_address", "email"),
            "city": ("city",),
            "state_or_province": ("state_or_province", "state", "province", "region"),
            "postal_code": ("postal_code", "zip", "zip_code", "postcode"),
            "country": ("country", "country_code"),
            "primary_phone_number": ("primary_phone_number", "phone_number", "phone"),
            "phone_type": ("phone_type", "type"),
        }
        for field_name, aliases in alias_map.items():
            for alias in aliases:
                if alias in source:
                    assign(field_name, source.get(alias))
                    break

    assign_many(payload.get("contact_profile"))
    assign_many(payload.get("candidate_profile"))

    resume_variant = payload.get("resume_variant") if isinstance(payload.get("resume_variant"), dict) else {}
    resume_text = _text(resume_variant.get("resume_variant_text"))
    cover_letter_text = _text(payload.get("cover_letter_text"))
    combined_text = "\n".join(part for part in (resume_text, cover_letter_text) if part)

    if resume_text:
        for line in resume_text.splitlines():
            stripped = _text(line)
            if not stripped:
                continue
            if len(stripped.split()) >= 2 and not EMAIL_PATTERN.search(stripped) and not PHONE_PATTERN.search(stripped):
                name_parts = [part for part in re.split(r"\s+", stripped) if part]
                if len(name_parts) >= 2:
                    assign("first_name", name_parts[0])
                    assign("last_name", name_parts[-1])
                break
    email_match = EMAIL_PATTERN.search(combined_text)
    if email_match:
        assign("email_address", email_match.group(1))
    phone_match = PHONE_PATTERN.search(combined_text)
    if phone_match:
        assign("primary_phone_number", _text(phone_match.group(1)))
    phone_type_value = _normalize_label_text(values.get("phone_type", ""))
    if phone_type_value:
        for normalized, aliases in PHONE_TYPE_OPTIONS.items():
            if phone_type_value in aliases or any(alias in phone_type_value for alias in aliases):
                values["phone_type"] = normalized
                break
    return values


def _contact_field_name(ref: SnapshotRef) -> str | None:
    label_text = _normalize_label_text(ref.label)
    if not label_text:
        return None
    if "secondary phone" in label_text:
        return None
    if ref.field_type == "radio":
        for aliases in PHONE_TYPE_OPTIONS.values():
            if label_text in aliases:
                return "phone_type"
    for field_name, keywords in CONTACT_TEXT_FIELD_KEYWORDS.items():
        if any(_label_contains_phrase(label_text, keyword) for keyword in keywords):
            return field_name
    return None


def _contact_candidate_required(*, ref: SnapshotRef, field_name: str) -> bool:
    raw = ref.raw_line.lower()
    label = ref.label.lower()
    if "secondary" in raw or "secondary" in label:
        return False
    if field_name == "phone_type":
        return True
    return "*" in ref.raw_line or "*" in ref.label or "required" in raw


def _contact_candidates(refs: list[SnapshotRef]) -> list[ContactFieldCandidate]:
    candidates: list[ContactFieldCandidate] = []
    seen_keys: set[tuple[str, ...]] = set()
    for ref in refs:
        if ref.field_type not in {"text", "select", "radio"}:
            continue
        field_name = _contact_field_name(ref)
        if not field_name:
            continue
        dedupe_key = (
            field_name,
            _normalize_label_text(ref.label),
        ) if ref.field_type == "radio" else (field_name,)
        if dedupe_key in seen_keys:
            continue
        candidates.append(
            ContactFieldCandidate(
                ref=ref,
                field_name=field_name,
                field_type=ref.field_type,
                label=_extract_visible_label(ref.label),
                prefilled=_snapshot_ref_prefilled(ref),
                required=_contact_candidate_required(ref=ref, field_name=field_name),
            )
        )
        seen_keys.add(dedupe_key)
    return candidates


def _radio_matches_phone_type(candidate: ContactFieldCandidate, desired_value: str) -> bool:
    normalized_desired = _normalize_label_text(desired_value)
    if not normalized_desired:
        return False
    normalized_option = _normalize_label_text(candidate.label)
    aliases = PHONE_TYPE_OPTIONS.get(normalized_desired, {normalized_desired})
    return normalized_option in aliases


def _radio_group_field_name(group_label: str, option_labels: list[str], *, snapshot_text: str = "") -> str | None:
    normalized_group = _normalize_label_text(group_label)
    normalized_options = {_normalize_label_text(option) for option in option_labels if _normalize_label_text(option)}
    phone_type_aliases = set().union(*PHONE_TYPE_OPTIONS.values())
    if (
        (
            "type" in normalized_group
            or ("primary phone number" in snapshot_text.lower() and bool(normalized_options & phone_type_aliases))
        )
        and (
            bool(normalized_options & phone_type_aliases)
            or "primary phone number" in snapshot_text.lower()
        )
    ):
        return "phone_type"
    return None


def _fallback_radio_group_label(field_name: str) -> str:
    if field_name == "phone_type":
        return "Type * Required"
    return field_name.replace("_", " ").strip().title()


def _find_radio_group_label(snapshot_text: str, candidate: ContactFieldCandidate) -> str:
    lines = [line.strip() for line in snapshot_text.splitlines() if _text(line)]
    target_line = _text(candidate.ref.raw_line)
    target_index = -1
    for index, line in enumerate(lines):
        if line == target_line:
            target_index = index
            break
    if target_index >= 0:
        for line in reversed(lines[:target_index]):
            lowered = line.lower()
            if ' group "' in lowered or lowered.startswith("group "):
                label = _extract_visible_label(line)
                if label:
                    return label
            if any(
                token in lowered
                for token in (' heading "', "textbox ", "combobox ", "select ", "button ", "dialog ")
            ):
                break
    return _fallback_radio_group_label(candidate.field_name)


def _snapshot_radio_group_diagnostics(
    *,
    snapshot_text: str,
    refs: list[SnapshotRef],
    selection_attempts: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    lines = [line.strip() for line in snapshot_text.splitlines() if _text(line)]
    ref_by_line = {_text(ref.raw_line): ref for ref in refs}
    groups: list[dict[str, Any]] = []
    current_group_label: str | None = None
    current_options: list[dict[str, Any]] = []

    def finalize_group() -> None:
        nonlocal current_group_label, current_options
        if not current_options:
            current_group_label = None
            current_options = []
            return
        option_labels = [_text(row.get("label")) for row in current_options if _text(row.get("label"))]
        field_name = _radio_group_field_name(_text(current_group_label), option_labels, snapshot_text=snapshot_text)
        if not field_name:
            current_group_label = None
            current_options = []
            return
        attempt = dict((selection_attempts or {}).get(field_name) or {})
        selected = next((row for row in current_options if bool(row.get("checked"))), None)
        refs_involved = [
            _text(row.get("ref"))
            for row in current_options
            if _text(row.get("ref"))
        ]
        groups.append(
            {
                "field_name": field_name,
                "group_label": _text(current_group_label) or _fallback_radio_group_label(field_name),
                "required": bool(
                    field_name == "phone_type"
                    or "*" in _text(current_group_label)
                    or "required" in _text(current_group_label).lower()
                ),
                "options": option_labels,
                "selected_option": _text(selected.get("label")) if selected else None,
                "selection_attempted": bool(attempt.get("selection_attempted")),
                "selection_verified": bool(selected),
                "chosen_option": _text(attempt.get("chosen_option") or attempt.get("attempted_option")) or None,
                "refs_involved": refs_involved,
            }
        )
        current_group_label = None
        current_options = []

    for line in lines:
        lowered = line.lower()
        if ' group "' in lowered or lowered.startswith("group ") or "fieldset" in lowered or "legend" in lowered:
            finalize_group()
            current_group_label = _extract_visible_label(line)
            continue
        if "radio " in lowered:
            option_label = _extract_visible_label(line)
            ref = ref_by_line.get(line)
            current_options.append(
                {
                    "label": option_label,
                    "checked": (" checked" in lowered or " selected" in lowered),
                    "active_only": "[active]" in lowered,
                    "ref": ref.ref if ref else None,
                }
            )
            continue
        if current_options and any(
            token in lowered
            for token in ("textbox ", "combobox ", "select ", "button ", "heading ", "dialog ", "input ")
        ):
            finalize_group()
    finalize_group()
    return groups


def _linkedin_radio_groups_from_dom(client: OpenClawBrowserClient | Any, snapshot_text: str) -> list[dict[str, Any]]:
    snapshot_lower = snapshot_text.lower()
    if "contact info" not in snapshot_lower and "primary phone number" not in snapshot_lower:
        return []
    try:
        result = client.evaluate_json(_linkedin_radio_groups_probe_script())
    except Exception:
        return []
    if not isinstance(result, dict) or _text(result.get("probeKind")) != "__openclaw_linkedin_radio_groups_probe__":
        return []
    groups: list[dict[str, Any]] = []
    for row in result.get("groups") or []:
        if not isinstance(row, dict):
            continue
        field_name = _text(row.get("field_name"))
        group_label = _text(row.get("group_label"))
        options = [_text(option) for option in list(row.get("options") or []) if _text(option)]
        inferred_field_name = field_name or _radio_group_field_name(group_label, options, snapshot_text=snapshot_text) or ""
        if not inferred_field_name:
            continue
        groups.append(
            {
                "field_name": inferred_field_name,
                "group_label": group_label or _fallback_radio_group_label(inferred_field_name),
                "required": _as_bool(row.get("required"), default=(inferred_field_name == "phone_type")),
                "options": options,
                "selected_option": _text(row.get("selected_option")) or None,
                "selection_attempted": False,
                "selection_verified": _as_bool(row.get("selection_verified"), default=False),
                "chosen_option": _text(row.get("chosen_option")) or None,
                "refs_involved": [_text(ref) for ref in list(row.get("refs_involved") or []) if _text(ref)],
            }
        )
    return groups


def _merge_radio_group_diagnostics(
    *,
    snapshot_groups: list[dict[str, Any]],
    dom_groups: list[dict[str, Any]],
    selection_attempts: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in snapshot_groups + dom_groups:
        field_name = _text(row.get("field_name"))
        if not field_name:
            continue
        existing = merged.get(field_name, {})
        options = list(dict.fromkeys([*list(existing.get("options") or []), *list(row.get("options") or [])]))
        refs_involved = list(dict.fromkeys([*list(existing.get("refs_involved") or []), *list(row.get("refs_involved") or [])]))
        combined = {
            "field_name": field_name,
            "group_label": _text(row.get("group_label")) or _text(existing.get("group_label")) or _fallback_radio_group_label(field_name),
            "required": bool(row.get("required") or existing.get("required")),
            "options": options,
            "selected_option": _text(row.get("selected_option")) or _text(existing.get("selected_option")) or None,
            "selection_attempted": bool(row.get("selection_attempted") or existing.get("selection_attempted")),
            "selection_verified": bool(row.get("selection_verified") or existing.get("selection_verified")),
            "chosen_option": _text(row.get("chosen_option")) or _text(existing.get("chosen_option")) or None,
            "refs_involved": refs_involved,
        }
        merged[field_name] = combined
    for field_name, attempt in (selection_attempts or {}).items():
        current = merged.setdefault(
            field_name,
            {
                "field_name": field_name,
                "group_label": _fallback_radio_group_label(field_name),
                "required": field_name == "phone_type",
                "options": [],
                "selected_option": None,
                "selection_attempted": False,
                "selection_verified": False,
                "chosen_option": None,
                "refs_involved": [],
            },
        )
        current["selection_attempted"] = bool(attempt.get("selection_attempted")) or bool(current.get("selection_attempted"))
        current["selection_verified"] = bool(attempt.get("selection_verified")) or bool(current.get("selection_verified"))
        current["chosen_option"] = _text(attempt.get("chosen_option") or attempt.get("attempted_option")) or current.get("chosen_option")
        attempted_ref = _text(attempt.get("attempted_ref"))
        if attempted_ref and attempted_ref not in current["refs_involved"]:
            current["refs_involved"].append(attempted_ref)
        verified_option = _text(attempt.get("verified_option"))
        if verified_option:
            current["selected_option"] = verified_option
    return list(merged.values())


def _contact_radio_group_diagnostics(
    *,
    snapshot_text: str,
    refs: list[SnapshotRef],
    selection_attempts: dict[str, dict[str, Any]] | None = None,
    dom_radio_groups: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    snapshot_groups = _snapshot_radio_group_diagnostics(
        snapshot_text=snapshot_text,
        refs=refs,
        selection_attempts=selection_attempts,
    )
    merged = _merge_radio_group_diagnostics(
        snapshot_groups=snapshot_groups,
        dom_groups=list(dom_radio_groups or []),
        selection_attempts=selection_attempts,
    )
    diagnostics: list[dict[str, Any]] = []
    for row in merged:
        diagnostics.append(
            {
                "field_name": _text(row.get("field_name")) or None,
                "group_label": _text(row.get("group_label")) or None,
                "required": bool(row.get("required")),
                "options": list(row.get("options") or []),
                "option_labels": list(row.get("options") or []),
                "selected_option": _text(row.get("selected_option")) or None,
                "selection_attempted": bool(row.get("selection_attempted")),
                "selection_verified": bool(row.get("selection_verified")),
                "chosen_option": _text(row.get("chosen_option")) or None,
                "refs_involved": list(row.get("refs_involved") or []),
            }
        )
    return diagnostics


def _plan_contact_field_actions(
    *,
    refs: list[SnapshotRef],
    contact_values: dict[str, str],
    used_refs: set[str] | None = None,
) -> list[ContactFieldAction]:
    reserved = set(used_refs or set())
    planned: list[ContactFieldAction] = []
    for candidate in _contact_candidates(refs):
        if candidate.ref.ref in reserved:
            planned.append(ContactFieldAction(candidate=candidate, action="skipped", reason="ref_already_allocated"))
            continue
        if candidate.prefilled:
            planned.append(ContactFieldAction(candidate=candidate, action="prefilled_verified"))
            reserved.add(candidate.ref.ref)
            continue
        desired_value = _text(contact_values.get(candidate.field_name))
        if candidate.field_name == "phone_type" and candidate.field_type == "radio":
            if desired_value and _radio_matches_phone_type(candidate, desired_value):
                planned.append(ContactFieldAction(candidate=candidate, action="fill", value=True))
                reserved.add(candidate.ref.ref)
            else:
                planned.append(
                    ContactFieldAction(
                        candidate=candidate,
                        action="skipped",
                        reason="no_matching_phone_type_value",
                    )
                )
            continue
        if not desired_value:
            planned.append(ContactFieldAction(candidate=candidate, action="skipped", reason="no_safe_value_available"))
            continue
        planned.append(ContactFieldAction(candidate=candidate, action="fill", value=desired_value))
        reserved.add(candidate.ref.ref)
    return planned


def _detect_keywords(text: str, hints: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(hint in lowered for hint in hints)


def _matched_keywords(text: str, hints: tuple[str, ...]) -> list[str]:
    lowered = text.lower()
    return [hint for hint in hints if hint in lowered]


def _host(url: str) -> str:
    return urlparse(url).netloc.lower()


def _is_linkedin_easy_apply_target(payload: dict[str, Any]) -> bool:
    target = payload.get("application_target") if isinstance(payload.get("application_target"), dict) else {}
    application_url = _text(target.get("application_url") or target.get("source_url"))
    return _host(application_url).endswith("linkedin.com") if application_url else False


def _validate_resume_upload_target(
    *,
    payload: dict[str, Any],
    resume_upload_path: str,
    upload_ref: SnapshotRef | None,
    screenshots: list[dict[str, Any]],
    checkpoint_urls: list[str],
    page_title: str | None,
    warnings: list[str],
    errors: list[str],
    page_diagnostics: dict[str, Any],
    form_diagnostics: dict[str, Any],
    build_debug_json: Any,
) -> dict[str, Any] | None:
    candidate = Path(resume_upload_path).expanduser()
    if not candidate.exists() or not candidate.is_file():
        return _result(
            draft_status="not_started",
            source_status="upload_failed",
            awaiting_review=False,
            review_status="blocked",
            failure_category="upload_failed",
            blocking_reason="The tailored resume could not be uploaded successfully.",
            screenshot_metadata_references=screenshots,
            checkpoint_urls=checkpoint_urls,
            page_title=page_title,
            warnings=warnings,
            errors=[*errors, "resume_upload_path_missing_or_not_file"],
            page_diagnostics=page_diagnostics,
            form_diagnostics=form_diagnostics,
            debug_json=build_debug_json(),
        )

    extension = candidate.suffix.lower()
    if _is_linkedin_easy_apply_target(payload) and extension not in set(LINKEDIN_ALLOWED_RESUME_EXTENSIONS):
        return _result(
            draft_status="not_started",
            source_status="unsupported_resume_upload_format",
            awaiting_review=False,
            review_status="blocked",
            failure_category="unsupported_resume_upload_format",
            blocking_reason="LinkedIn Easy Apply only accepts PDF, DOCX, or DOC resume uploads.",
            screenshot_metadata_references=screenshots,
            checkpoint_urls=checkpoint_urls,
            page_title=page_title,
            warnings=warnings,
            errors=[
                *errors,
                f"unsupported_resume_upload_format:{extension or 'none'}",
                "resume_upload_site:linkedin_easy_apply",
                "resume_upload_allowed_extensions:.pdf,.docx,.doc",
            ],
            page_diagnostics=page_diagnostics,
            form_diagnostics=form_diagnostics,
            debug_json=build_debug_json(),
        )

    if upload_ref is not None and upload_ref.field_type != "file":
        return _result(
            draft_status="not_started",
            source_status="unsupported_form",
            awaiting_review=False,
            review_status="blocked",
            failure_category="unsupported_form",
            blocking_reason="The form structure could not be safely automated in draft-only mode.",
            screenshot_metadata_references=screenshots,
            checkpoint_urls=checkpoint_urls,
            page_title=page_title,
            warnings=warnings,
            errors=[*errors, "resume_upload_ref_not_file_input"],
            page_diagnostics=page_diagnostics,
            form_diagnostics=form_diagnostics,
            debug_json=build_debug_json(),
        )
    return None


def _safe_stage_upload(source_path: str, *, run_key: str) -> Path:
    candidate = Path(source_path).expanduser().resolve()
    UPLOAD_STAGING_DIR.mkdir(parents=True, exist_ok=True)
    staged_path = UPLOAD_STAGING_DIR / f"{run_key}{candidate.suffix.lower() or '.txt'}"
    shutil.copy2(candidate, staged_path)
    return staged_path


def _screenshot_reference(path: Path, *, label: str, page_url: str | None) -> dict[str, Any]:
    size_bytes = path.stat().st_size if path.exists() else None
    return {
        "label": label,
        "path": str(path.resolve()),
        "kind": "checkpoint",
        "captured_at": _utc_iso(),
        "page_url": page_url,
        "mime_type": "image/png",
        "size_bytes": size_bytes,
    }


def _capture_screenshot(
    client: OpenClawBrowserClient,
    *,
    screenshot_dir: Path,
    checkpoint_name: str,
    page_url: str | None,
    screenshots: list[dict[str, Any]],
    warnings: list[str],
    screenshot_failures: list[dict[str, Any]],
    max_screenshots: int,
) -> None:
    if len(screenshots) >= max_screenshots:
        return
    destination = screenshot_dir / f"{len(screenshots) + 1:02d}-{checkpoint_name}.png"
    try:
        captured = client.screenshot(destination)
    except BrowserCommandError as exc:
        warnings.append(f"screenshot_failed:{checkpoint_name}")
        screenshot_failures.append(
            {
                "checkpoint_name": checkpoint_name,
                "failure_category": exc.failure_category,
                "blocking_reason": exc.blocking_reason,
                "error_kind": exc.error_kind or "screenshot_failure",
                "stage": exc.stage,
            }
        )
        return
    screenshots.append(_screenshot_reference(captured, label=checkpoint_name, page_url=page_url))


def _page_diagnostics(
    *,
    application_url: str,
    current_url: str,
    page_title: str,
    snapshot_text: str,
    refs: list[SnapshotRef],
    upload_ref: SnapshotRef | None,
) -> dict[str, Any]:
    excerpt = snapshot_text[:DEFAULT_MAX_SNAPSHOT_CHARS]
    combined_text = _combine_text(current_url, page_title, excerpt)
    application_host = _host(application_url)
    current_host = _host(current_url)
    application_path = urlparse(application_url).path.lower()
    current_path = urlparse(current_url).path.lower()
    is_linkedin_target = application_host.endswith("linkedin.com")
    login_indicator_matches = _matched_keywords(combined_text, LOGIN_HINTS)
    checkpoint_marker_matches = _matched_keywords(combined_text, LINKEDIN_CHECKPOINT_HINTS)
    linkedin_login_page_matches = _matched_keywords(combined_text, LINKEDIN_LOGIN_PAGE_HINTS) if is_linkedin_target else []
    explicit_login_url_detected = any(
        token in current_path for token in ("/login", "/uas/login", "/checkpoint", "/authwall")
    ) or current_host.startswith("auth.")
    linkedin_nav_matches = _matched_keywords(excerpt, LINKEDIN_NAV_HINTS) if is_linkedin_target else []
    linkedin_nav_visible = len(linkedin_nav_matches) >= 2
    easy_apply_dialog_matches = _matched_keywords(excerpt, LINKEDIN_DIALOG_HINTS) if is_linkedin_target else []
    easy_apply_dialog_exists = is_linkedin_target and (
        bool(easy_apply_dialog_matches)
        or ('dialog "' in excerpt.lower() and "apply to" in excerpt.lower())
    )
    linkedin_job_page_matches = _matched_keywords(excerpt, LINKEDIN_JOB_PAGE_HINTS) if is_linkedin_target else []
    linkedin_job_page_visible = is_linkedin_target and (
        "/jobs/view/" in application_path
        or "/jobs/view/" in current_path
        or bool(linkedin_job_page_matches)
    )
    apply_modal_expected = is_linkedin_target and (
        "opensduiapplyflow=true" in application_url.lower()
        or "/apply/" in application_path
    )
    login_or_checkpoint_markers_present = explicit_login_url_detected or bool(checkpoint_marker_matches) or (
        bool(linkedin_login_page_matches) and not easy_apply_dialog_exists and not linkedin_nav_visible
    )
    apply_modal_not_mounted = bool(
        apply_modal_expected
        and linkedin_job_page_visible
        and not easy_apply_dialog_exists
        and not login_or_checkpoint_markers_present
        and current_host == application_host
    )
    linkedin_state = None
    if is_linkedin_target:
        if easy_apply_dialog_exists:
            linkedin_state = "easy_apply_dialog_open"
        elif login_or_checkpoint_markers_present:
            linkedin_state = "login_or_checkpoint"
        elif apply_modal_not_mounted:
            linkedin_state = "job_page_modal_not_mounted"
        elif linkedin_job_page_visible:
            linkedin_state = "job_page_visible"
        else:
            linkedin_state = "unknown"
    return {
        "application_url": application_url,
        "current_url": current_url,
        "final_url": current_url,
        "target_host": application_host,
        "current_host": current_host,
        "page_title": page_title,
        "login_indicators_detected": bool(login_indicator_matches),
        "login_indicator_matches": login_indicator_matches,
        "captcha_indicators_detected": _detect_keywords(excerpt, CAPTCHA_HINTS),
        "anti_bot_indicators_detected": _detect_keywords(excerpt, ANTI_BOT_HINTS),
        "submit_indicators_detected": _detect_keywords(excerpt, SUBMIT_HINTS),
        "easy_apply_dialog_exists": easy_apply_dialog_exists,
        "upload_input_exists": bool(upload_ref and upload_ref.field_type == "file"),
        "upload_input_ref": upload_ref.ref if upload_ref and upload_ref.field_type == "file" else None,
        "detected_ref_count": len(refs),
        "linkedin_nav_visible": linkedin_nav_visible,
        "linkedin_nav_matches": linkedin_nav_matches,
        "login_or_checkpoint_markers_present": login_or_checkpoint_markers_present,
        "checkpoint_marker_matches": checkpoint_marker_matches,
        "linkedin_login_page_matches": linkedin_login_page_matches,
        "explicit_login_url_detected": explicit_login_url_detected,
        "linkedin_job_page_visible": linkedin_job_page_visible,
        "linkedin_job_page_matches": linkedin_job_page_matches,
        "apply_modal_expected": apply_modal_expected,
        "apply_modal_not_mounted": apply_modal_not_mounted,
        "linkedin_state": linkedin_state,
        "snapshot_excerpt": excerpt,
    }


def _linkedin_step_context(
    *,
    snapshot_text: str,
    refs: list[SnapshotRef],
    upload_ref: SnapshotRef | None,
    contact_field_actions: list[ContactFieldAction],
    page_diagnostics: dict[str, Any],
) -> dict[str, Any]:
    excerpt = snapshot_text[:DEFAULT_MAX_SNAPSHOT_CHARS].lower()
    easy_apply_ref = _find_clickable_ref(refs, keywords=["easy apply"])
    next_ref = _find_clickable_ref(
        refs,
        keywords=["next", "continue"],
        disallowed_keywords=["submit", "review", "dismiss", "close", "cancel", "save"],
    )
    upload_button_ref = _find_clickable_ref(
        refs,
        keywords=["upload", "resume", "attach"],
        disallowed_keywords=["submit", "review"],
    )
    contact_heading_present = any(
        token in excerpt for token in ('heading "contact info"', "contact info", "email address", "primary phone number")
    )
    resume_heading_present = any(
        token in excerpt for token in ('heading "resume"', "upload resume", "be sure to include an updated resume")
    )
    review_heading_present = any(
        token in excerpt
        for token in (
            'heading "review your application"',
            "review your application",
            "before submitting",
        )
    )
    later_step_heading_present = any(
        token in excerpt
        for token in (
            'heading "additional questions"',
            'heading "work experience"',
            'heading "education"',
            'heading "screening questions"',
        )
    )
    selected_resume = _selected_resume_diagnostics(snapshot_text, refs)
    state = page_diagnostics.get("linkedin_state")
    if page_diagnostics.get("easy_apply_dialog_exists"):
        if contact_heading_present or any(action.candidate.field_name in CONTACT_TEXT_FIELD_KEYWORDS for action in contact_field_actions):
            state = "easy_apply_contact_info_step"
        elif (
            upload_ref is not None
            or resume_heading_present
            or upload_button_ref is not None
            or selected_resume["selected_resume_detected"]
        ):
            state = "easy_apply_resume_upload_step"
        elif review_heading_present:
            state = "easy_apply_review_step"
        elif later_step_heading_present or page_diagnostics.get("submit_indicators_detected"):
            state = "easy_apply_later_step"
        else:
            state = "easy_apply_later_step"
    elif easy_apply_ref is not None and page_diagnostics.get("linkedin_job_page_visible"):
        state = "job_page_easy_apply_visible"

    return {
        "state": state,
        "modal_open": bool(page_diagnostics.get("easy_apply_dialog_exists")),
        "easy_apply_ref": easy_apply_ref.ref if easy_apply_ref else None,
        "easy_apply_ref_label": easy_apply_ref.label if easy_apply_ref else None,
        "easy_apply_ref_reason": "easy_apply_trigger_visible_on_job_page" if easy_apply_ref else None,
        "next_ref": next_ref.ref if next_ref else None,
        "next_ref_label": next_ref.label if next_ref else None,
        "next_ref_reason": "next_or_continue_button_visible_on_current_step" if next_ref else None,
        "next_button_ref": next_ref.ref if next_ref else None,
        "next_button_label": next_ref.label if next_ref else None,
        "upload_ref": upload_ref.ref if upload_ref else None,
        "upload_ref_label": upload_ref.label if upload_ref else None,
        "upload_ref_reason": "current_step_file_input_detected" if upload_ref else None,
        "upload_button_ref": upload_button_ref.ref if upload_button_ref else None,
        "upload_button_ref_label": upload_button_ref.label if upload_button_ref else None,
        "upload_button_ref_reason": "resume_upload_button_visible_without_file_input" if upload_button_ref and upload_ref is None else None,
        "selected_resume_detected": selected_resume["selected_resume_detected"],
        "selected_resume_label": selected_resume["selected_resume_label"],
        "selected_resume_verified": selected_resume["selected_resume_verified"],
        "upload_required": state == "easy_apply_resume_upload_step" and not selected_resume["selected_resume_verified"],
        "continue_button_ref": next_ref.ref if state == "easy_apply_resume_upload_step" and next_ref else None,
        "continue_button_label": next_ref.label if state == "easy_apply_resume_upload_step" and next_ref else None,
        "continue_clicked": False,
        "continue_verified": False,
}


def _extract_progress_percent(snapshot_text: str) -> int | None:
    match = re.search(r"\b([1-9]\d?|100)\s*%", snapshot_text)
    if match:
        return int(match.group(1))
    return None


def _snapshot_heading_text(snapshot_text: str) -> str | None:
    for line in snapshot_text.splitlines():
        stripped = line.strip()
        if "heading" not in stripped.lower():
            continue
        heading = _extract_visible_label(stripped)
        if heading:
            return heading
    return None


def _linkedin_visible_step_labels(refs: list[SnapshotRef], *, limit: int = 8) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        if ref.field_type not in {"text", "select", "radio", "checkbox"}:
            continue
        label = _ref_prompt_label(ref)
        if not label:
            continue
        normalized = _normalize_label_text(label)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        labels.append(label)
        if len(labels) >= limit:
            break
    return labels


def _linkedin_review_like_step(snapshot_text: str, refs: list[SnapshotRef], page_diagnostics: dict[str, Any]) -> bool:
    excerpt = snapshot_text[:DEFAULT_MAX_SNAPSHOT_CHARS].lower()
    if str(page_diagnostics.get("linkedin_state") or "") == "easy_apply_review_step":
        return True
    if any(
        token in excerpt
        for token in (
            'heading "review your application"',
            "review your application",
            "before submitting",
            "submit application",
            "submit your application",
            "final review",
        )
    ):
        return True
    submit_ref = _find_clickable_ref(
        refs,
        keywords=["submit", "finish application", "apply now", "send application"],
        disallowed_keywords=["save", "dismiss", "cancel", "close"],
    )
    return bool(submit_ref)


def _linkedin_step_signature(snapshot_text: str, refs: list[SnapshotRef], page_diagnostics: dict[str, Any]) -> dict[str, Any]:
    heading = _snapshot_heading_text(snapshot_text) or _text(page_diagnostics.get("linkedin_state")) or "unknown"
    progress_percent = _extract_progress_percent(snapshot_text)
    visible_labels = _linkedin_visible_step_labels(refs)
    next_label = _extract_visible_label(
        _text(page_diagnostics.get("next_ref_label") or page_diagnostics.get("next_button_label"))
    )
    signature_parts = [
        _normalize_label_text(heading),
        str(progress_percent) if progress_percent is not None else "",
        _normalize_label_text(next_label),
        *[_normalize_label_text(label) for label in visible_labels[:6]],
    ]
    signature = "|".join(part for part in signature_parts if part)
    return {
        "signature": signature or _normalize_label_text(snapshot_text[:240]) or "unknown-step",
        "heading": heading,
        "progress_percent": progress_percent,
        "visible_labels": visible_labels[:8],
        "next_button_label": next_label or None,
        "review_like": _linkedin_review_like_step(snapshot_text, refs, page_diagnostics),
    }


def _contact_step_progression_diagnostics(actions: list[ContactFieldAction]) -> dict[str, Any]:
    return _contact_step_progression_diagnostics_with_radios(actions, radio_group_diagnostics=[])


def _contact_step_progression_diagnostics_with_radios(
    actions: list[ContactFieldAction],
    *,
    radio_group_diagnostics: list[dict[str, Any]],
) -> dict[str, Any]:
    grouped_actions: dict[str, list[ContactFieldAction]] = {}
    for action in actions:
        grouped_actions.setdefault(action.candidate.field_name, []).append(action)

    blocking_skipped_fields: list[dict[str, Any]] = []
    nonblocking_skipped_fields: list[dict[str, Any]] = []
    required_field_statuses: list[dict[str, Any]] = []
    blocking_reasons = {"no_safe_value_available", "no_matching_phone_type_value"}
    radio_by_field = {
        _text(row.get("field_name")): row
        for row in radio_group_diagnostics
        if _text(row.get("field_name"))
    }

    for field_name, grouped in grouped_actions.items():
        required = any(action.candidate.required for action in grouped)
        is_radio_group = all(action.candidate.field_type == "radio" for action in grouped)
        radio_diag = radio_by_field.get(field_name, {})
        satisfied = (
            any(action.candidate.prefilled for action in grouped)
            if is_radio_group
            else any(action.action in {"fill", "prefilled_verified"} for action in grouped)
        )
        skipped_rows = [action for action in grouped if action.action == "skipped"]
        required_field_statuses.append(
            {
                "field_name": field_name,
                "required": required,
                "satisfied": satisfied,
                "skipped_reasons": [str(action.reason or "") for action in skipped_rows],
                "group_label": _text(radio_diag.get("group_label")) or None,
                "selected_option": _text(radio_diag.get("selected_option")) or None,
                "selection_attempted": bool(radio_diag.get("selection_attempted")),
                "selection_verified": bool(radio_diag.get("selection_verified")),
            }
        )
        for action in skipped_rows:
            row = {
                "ref": action.candidate.ref.ref,
                "field_name": field_name,
                "label": action.candidate.label,
                "field_type": action.candidate.field_type,
                "reason": action.reason,
                "required": required,
                "field_satisfied_elsewhere": satisfied,
                "group_label": _text(radio_diag.get("group_label")) or None,
            }
            if is_radio_group:
                nonblocking_skipped_fields.append(row)
                continue
            if required and not satisfied and str(action.reason or "") in blocking_reasons:
                blocking_skipped_fields.append(row)
            else:
                nonblocking_skipped_fields.append(row)
        if is_radio_group and required and not satisfied:
            blocking_skipped_fields.append(
                {
                    "ref": grouped[0].candidate.ref.ref,
                    "field_name": field_name,
                    "label": grouped[0].candidate.label,
                    "field_type": grouped[0].candidate.field_type,
                    "reason": (
                        "radio_selection_attempted_but_not_verified"
                        if bool(radio_diag.get("selection_attempted"))
                        else "required_radio_group_unselected"
                    ),
                    "required": required,
                    "field_satisfied_elsewhere": False,
                    "group_label": _text(radio_diag.get("group_label")) or None,
                    "selected_option": _text(radio_diag.get("selected_option")) or None,
                    "selection_attempted": bool(radio_diag.get("selection_attempted")),
                    "selection_verified": bool(radio_diag.get("selection_verified")),
                }
            )

    for field_name, radio_diag in radio_by_field.items():
        if field_name in grouped_actions:
            continue
        required = bool(radio_diag.get("required"))
        selection_verified = bool(radio_diag.get("selection_verified"))
        required_field_statuses.append(
            {
                "field_name": field_name,
                "required": required,
                "satisfied": selection_verified,
                "skipped_reasons": [],
                "group_label": _text(radio_diag.get("group_label")) or None,
                "selected_option": _text(radio_diag.get("selected_option")) or None,
                "selection_attempted": bool(radio_diag.get("selection_attempted")),
                "selection_verified": selection_verified,
            }
        )
        if required and not selection_verified:
            blocking_skipped_fields.append(
                {
                    "ref": None,
                    "field_name": field_name,
                    "label": _text(radio_diag.get("group_label")) or _fallback_radio_group_label(field_name),
                    "field_type": "radio",
                    "reason": (
                        "radio_selection_attempted_but_not_verified"
                        if bool(radio_diag.get("selection_attempted"))
                        else "required_radio_group_unselected"
                    ),
                    "required": True,
                    "field_satisfied_elsewhere": False,
                    "group_label": _text(radio_diag.get("group_label")) or None,
                    "selected_option": _text(radio_diag.get("selected_option")) or None,
                    "selection_attempted": bool(radio_diag.get("selection_attempted")),
                    "selection_verified": selection_verified,
                }
            )

    can_advance = len(blocking_skipped_fields) == 0
    return {
        "blocking_skipped_fields": blocking_skipped_fields[:20],
        "nonblocking_skipped_fields": nonblocking_skipped_fields[:20],
        "required_field_statuses": required_field_statuses[:20],
        "radio_group_diagnostics": radio_group_diagnostics[:20],
        "can_advance": can_advance,
        "next_click_gate_reason": (
            "all_required_contact_fields_satisfied" if can_advance else "blocking_required_contact_fields"
        ),
    }


def _contact_step_can_advance(
    actions: list[ContactFieldAction],
    *,
    radio_group_diagnostics: list[dict[str, Any]],
) -> bool:
    return bool(
        _contact_step_progression_diagnostics_with_radios(
            actions,
            radio_group_diagnostics=radio_group_diagnostics,
        )["can_advance"]
    )


def _contact_fill_work(
    actions: list[ContactFieldAction],
) -> tuple[list[dict[str, Any]], list[ContactFieldAction], list[dict[str, Any]]]:
    fill_payloads: list[dict[str, Any]] = []
    select_actions: list[ContactFieldAction] = []
    manifest_rows: list[dict[str, Any]] = []
    for action in actions:
        candidate = action.candidate
        if action.action == "prefilled_verified":
            manifest_rows.append(
                {
                    "field_name": candidate.field_name,
                    "label": candidate.label,
                    "field_type": candidate.field_type,
                    "status": "prefilled_verified",
                    "value_preview": None,
                    "value_redacted": True,
                }
            )
            continue
        if action.action != "fill":
            continue
        if candidate.field_type == "select":
            select_actions.append(action)
            manifest_rows.append(
                {
                    "field_name": candidate.field_name,
                    "label": candidate.label,
                    "field_type": candidate.field_type,
                    "status": "selected",
                    "value_preview": None,
                    "value_redacted": True,
                }
            )
            continue
        fill_payloads.append({"ref": candidate.ref.ref, "value": action.value, "type": candidate.field_type})
        manifest_rows.append(
            {
                "field_name": candidate.field_name,
                "label": candidate.label,
                "field_type": candidate.field_type,
                "status": "checked" if candidate.field_type == "radio" else "filled",
                "value_preview": None,
                "value_redacted": True,
            }
        )
    return fill_payloads, select_actions, manifest_rows


def _form_diagnostics(
    *,
    snapshot_text: str,
    refs: list[SnapshotRef],
    upload_ref: SnapshotRef | None,
    field_actions: list[ContactFieldAction],
    radio_selection_attempts: dict[str, dict[str, Any]] | None = None,
    dom_radio_groups: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    radio_group_diagnostics = _contact_radio_group_diagnostics(
        snapshot_text=snapshot_text,
        refs=refs,
        selection_attempts=radio_selection_attempts,
        dom_radio_groups=dom_radio_groups,
    )
    progression = _contact_step_progression_diagnostics_with_radios(
        field_actions,
        radio_group_diagnostics=radio_group_diagnostics,
    )
    candidate_details: list[dict[str, Any]] = []
    for action in field_actions[:20]:
        candidate_details.append(
            {
                "ref": action.candidate.ref.ref,
                "field_name": action.candidate.field_name,
                "field_type": action.candidate.field_type,
                "label": action.candidate.label,
                "action": action.action,
                "reason": action.reason,
                "prefilled": action.candidate.prefilled,
                "required": action.candidate.required,
            }
        )
    return {
        "detected_ref_count": len(refs),
        "upload_ref": upload_ref.ref if upload_ref else None,
        "fill_candidate_count": len(field_actions),
        "fillable_candidate_count": sum(1 for action in field_actions if action.action == "fill"),
        "prefilled_candidate_count": sum(1 for action in field_actions if action.action == "prefilled_verified"),
        "detected_labels": [action.candidate.label for action in field_actions[:20]],
        "candidate_details": candidate_details,
        "skipped_fields": [
            {
                "ref": action.candidate.ref.ref,
                "field_name": action.candidate.field_name,
                "label": action.candidate.label,
                "field_type": action.candidate.field_type,
                "reason": action.reason,
                "required": action.candidate.required,
            }
            for action in field_actions
            if action.action == "skipped"
        ][:20],
        "blocking_skipped_fields": progression["blocking_skipped_fields"],
        "nonblocking_skipped_fields": progression["nonblocking_skipped_fields"],
        "required_field_statuses": progression["required_field_statuses"],
        "radio_group_diagnostics": progression["radio_group_diagnostics"],
        "next_click_gate_reason": progression["next_click_gate_reason"],
    }


def _explicit_answer_entries(answers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for row in answers:
        if not isinstance(row, dict):
            continue
        answer_text = _text(row.get("answer"))
        question_text = _text(row.get("question"))
        answer_type = _text(row.get("answer_type")).lower() or "custom"
        if not answer_text and answer_type != "motivation":
            continue
        mapping = normalize_canonical_key(question_text)
        entries.append(
            {
                "question": question_text,
                "answer": answer_text,
                "answer_type": answer_type,
                "canonical_key": mapping.get("canonical_key") if isinstance(mapping, dict) else None,
            }
        )
    return entries


def _resolve_question_answer(
    *,
    canonical_key: str,
    required: bool,
    ref: SnapshotRef,
    answer_profile: dict[str, Any],
    application_target: dict[str, Any],
    explicit_answers: list[dict[str, Any]],
) -> dict[str, Any]:
    application_url = _text(application_target.get("application_url") or application_target.get("source_url"))
    linkedin_target = _host(application_url).endswith("linkedin.com") if application_url else False
    field_text = _normalize_label_text(_combine_text(ref.context_text, ref.label, ref.raw_line))

    for row in explicit_answers:
        if row.get("canonical_key") == canonical_key and _text(row.get("answer")):
            return {
                "action": "answer",
                "canonical_key": canonical_key,
                "value": _text(row.get("answer")),
                "source": "explicit_payload",
                "confidence": 0.97,
            }
    if canonical_key == "reason_for_interest":
        for row in explicit_answers:
            if row.get("answer_type") == "motivation" and _text(row.get("answer")):
                return {
                    "action": "answer",
                    "canonical_key": canonical_key,
                    "value": _text(row.get("answer")),
                    "source": "explicit_payload",
                    "confidence": 0.97,
                }
        motivation = motivation_answer(
            profile=answer_profile,
            application_target=application_target,
            question_text=_text(ref.context_text or ref.label),
        )
        return {
            "action": "answer",
            "canonical_key": canonical_key,
            "value": _text(motivation.get("answer")),
            "source": _text(motivation.get("source")) or "deterministic_fallback",
            "confidence": float(motivation.get("confidence") or 0.0),
            "reason": _text(motivation.get("reason")),
        }
    if canonical_key == "reason_seeking_new_role":
        value = _text(answer_profile.get("reason_seeking_new_role"))
        if value:
            return {
                "action": "answer",
                "canonical_key": canonical_key,
                "value": value,
                "source": "default_profile",
                "confidence": 0.95,
            }
    if linkedin_target:
        linkedin_defaults: dict[str, dict[str, str]] = {
            "work_authorized_us": {"value": "Yes", "source": "linkedin_policy_work_authorized"},
            "sponsorship_required": {"value": "No", "source": "linkedin_policy_sponsorship"},
            "worked_with_company_recruiter_before": {
                "value": "I have not worked with a recruiter",
                "source": "linkedin_policy_recruiter_contact",
            },
            "hear_about_us": {"value": "LinkedIn", "source": "linkedin_policy_referral_source"},
            "desired_salary": {"value": "100000", "source": "linkedin_policy_salary_default"},
            "available_start_date": {"value": _current_form_date(), "source": "linkedin_policy_start_date_current"},
            "certification_full_name": {"value": _safe_full_name(answer_profile), "source": "linkedin_policy_certification_full_name"},
            "certification_date": {"value": _current_form_date(), "source": "linkedin_policy_certification_date"},
            "certification_confirmation": {"value": "I Agree", "source": "linkedin_policy_certification_confirmation"},
        }
        if canonical_key in linkedin_defaults:
            default = linkedin_defaults[canonical_key]
            return {
                "action": "answer",
                "canonical_key": canonical_key,
                "value": _text(default.get("value")),
                "source": _text(default.get("source")),
                "confidence": 0.95,
                "required": required,
                "self_id_handling_mode": "standard",
            }
        if canonical_key == "security_clearance":
            return {
                "action": "answer",
                "canonical_key": canonical_key,
                "value": "None" if "level" in field_text else "No",
                "source": "linkedin_policy_security_clearance",
                "confidence": 0.95,
                "required": required,
                "self_id_handling_mode": "standard",
            }
        if canonical_key == "security_clearance_level":
            return {
                "action": "answer",
                "canonical_key": canonical_key,
                "value": "None",
                "source": "linkedin_policy_security_clearance_level",
                "confidence": 0.95,
                "required": required,
                "self_id_handling_mode": "standard",
            }
        if canonical_key == "polygraph":
            return {
                "action": "answer",
                "canonical_key": canonical_key,
                "value": "None" if "level" in field_text else "No",
                "source": "linkedin_policy_polygraph",
                "confidence": 0.95,
                "required": required,
                "self_id_handling_mode": "standard",
            }
        if canonical_key == "polygraph_level":
            return {
                "action": "answer",
                "canonical_key": canonical_key,
                "value": "None",
                "source": "linkedin_policy_polygraph_level",
                "confidence": 0.95,
                "required": required,
                "self_id_handling_mode": "standard",
            }
        if is_self_id_key(canonical_key):
            return {
                "action": "answer",
                "canonical_key": canonical_key,
                "value": "Prefer not to say",
                "source": "linkedin_safe_self_id_default",
                "confidence": 0.93,
                "required": required,
                "self_id_handling_mode": "safe_neutral_default",
            }
    return resolve_default_answer(
        profile=answer_profile,
        canonical_key=canonical_key,
        required=required,
        field_label=_text(ref.context_text or ref.label),
        field_type=ref.field_type,
    )


def _build_generic_answer_actions(
    *,
    refs: list[SnapshotRef],
    used_refs: set[str],
    answer_profile: dict[str, Any],
    application_target: dict[str, Any],
    answers: list[dict[str, Any]],
) -> dict[str, Any]:
    explicit_answers = _explicit_answer_entries(answers)
    grouped_refs: dict[str, list[tuple[SnapshotRef, dict[str, Any]]]] = {}
    mapped_ref_ids: set[str] = set()
    policy_matches: list[dict[str, Any]] = []
    answers_applied: list[dict[str, Any]] = []
    safe_skips: list[dict[str, Any]] = []
    personal_answer_fallbacks_used: list[dict[str, Any]] = []
    linkedin_target = _host(_text(application_target.get("application_url") or application_target.get("source_url"))).endswith("linkedin.com")
    for ref in refs:
        if ref.ref in used_refs or ref.field_type not in {"text", "select", "radio", "checkbox"}:
            continue
        mapping = _mapping_for_ref(ref, application_target=application_target)
        if not isinstance(mapping, dict):
            continue
        grouped_refs.setdefault(str(mapping["canonical_key"]), []).append((ref, mapping))
        mapped_ref_ids.add(ref.ref)

    fill_payloads: list[dict[str, Any]] = []
    select_actions: list[dict[str, Any]] = []
    execution_actions: list[dict[str, Any]] = []
    manifest_rows: list[dict[str, Any]] = []
    answer_mappings: list[dict[str, Any]] = []
    missing_required_fields: list[dict[str, Any]] = []
    required_fields_filled: list[str] = []
    answer_confidences: list[float] = []
    self_id_handling_modes: list[str] = []
    covered_explicit_canonical_keys: set[str] = set()

    for canonical_key, grouped in grouped_refs.items():
        covered_explicit_canonical_keys.add(canonical_key)
        required = any(_field_is_required(ref, canonical_key) for ref, _ in grouped)
        representative_ref = grouped[0][0]
        representative_mapping = grouped[0][1]
        resolution = _resolve_question_answer(
            canonical_key=canonical_key,
            required=required,
            ref=representative_ref,
            answer_profile=answer_profile,
            application_target=application_target,
            explicit_answers=explicit_answers,
        )
        if linkedin_target and _is_personal_answer_key(canonical_key):
            visible_neutral_option = _visible_neutral_personal_option(grouped)
            if visible_neutral_option:
                resolution = {
                    "action": "answer",
                    "canonical_key": canonical_key,
                    "value": visible_neutral_option,
                    "source": "linkedin_personal_answer_visible_neutral_option",
                    "confidence": 0.96,
                    "required": required,
                    "self_id_handling_mode": "safe_neutral_default",
                }
            elif required:
                fallback_value = _linkedin_personal_fallback_value(canonical_key)
                if fallback_value:
                    resolution = {
                        "action": "answer",
                        "canonical_key": canonical_key,
                        "value": fallback_value,
                        "source": "linkedin_personal_answer_fallback",
                        "confidence": 0.95,
                        "required": required,
                        "self_id_handling_mode": "truthful_personal_fallback",
                        "reason": "required_personal_answer_fallback",
                    }
                else:
                    resolution = {
                        "action": "review",
                        "canonical_key": canonical_key,
                        "value": None,
                        "source": "linkedin_personal_answer_uncertain",
                        "confidence": 0.0,
                        "required": required,
                        "self_id_handling_mode": "review",
                        "reason": f"no_safe_fallback_for:{canonical_key}",
                    }
            else:
                resolution = {
                    "action": "skip",
                    "canonical_key": canonical_key,
                    "value": None,
                    "source": "linkedin_optional_personal_answer_skip",
                    "confidence": 0.7,
                    "required": required,
                    "self_id_handling_mode": "skip_optional",
                }
        confidence = float(resolution.get("confidence") or 0.0)
        chosen_ref: SnapshotRef | None = None
        desired_value = _text(resolution.get("value"))
        if resolution.get("action") == "answer" and confidence >= DEFAULT_FILL_MIN_CONFIDENCE:
            for ref, _ in grouped:
                if ref.field_type == "select":
                    chosen_ref = ref
                    select_actions.append(
                        {
                            "ref": ref.ref,
                            "value": desired_value,
                            "canonical_key": canonical_key,
                            "confidence": confidence,
                            "field_type": ref.field_type,
                            "label": _ref_prompt_label(ref),
                            "normalized_label": _normalize_label_text(_ref_search_text(ref)),
                            "matched_phrase": representative_mapping.get("matched_phrase"),
                            "source": resolution.get("source"),
                        }
                    )
                    break
                if ref.field_type in {"radio", "checkbox"} and _option_matches_desired_value(ref.label, desired_value):
                    chosen_ref = ref
                    fill_payloads.append(
                        {
                            "ref": ref.ref,
                            "value": True,
                            "type": ref.field_type,
                            "canonical_key": canonical_key,
                            "label": _ref_prompt_label(ref),
                            "normalized_label": _normalize_label_text(_ref_search_text(ref)),
                            "matched_phrase": representative_mapping.get("matched_phrase"),
                            "source": resolution.get("source"),
                        }
                    )
                    break
                if ref.field_type == "text":
                    chosen_ref = ref
                    fill_payloads.append(
                        {
                            "ref": ref.ref,
                            "value": desired_value,
                            "type": "text",
                            "canonical_key": canonical_key,
                            "label": _ref_prompt_label(ref),
                            "normalized_label": _normalize_label_text(_ref_search_text(ref)),
                            "matched_phrase": representative_mapping.get("matched_phrase"),
                            "source": resolution.get("source"),
                        }
                    )
                    break
        if chosen_ref is not None:
            used_refs.add(chosen_ref.ref)
            manifest_row = (
                {
                    "field_name": canonical_key,
                    "label": _ref_prompt_label(chosen_ref),
                    "field_type": chosen_ref.field_type,
                    "status": "selected" if chosen_ref.field_type == "select" else ("checked" if chosen_ref.field_type in {"radio", "checkbox"} else "answered"),
                    "value_preview": None,
                    "value_redacted": True,
                    "canonical_key": canonical_key,
                    "answer_source": resolution.get("source"),
                    "confidence": round(confidence, 4),
                }
            )
            manifest_rows.append(manifest_row)
            execution_actions.append(
                {
                    "action_type": "select" if chosen_ref.field_type == "select" else "fill",
                    "field_type": chosen_ref.field_type,
                    "original_ref": chosen_ref.ref,
                    "value": desired_value if chosen_ref.field_type in {"select", "text"} else True,
                    "canonical_key": canonical_key,
                    "label": _ref_prompt_label(chosen_ref),
                    "normalized_label": _normalize_label_text(_ref_search_text(chosen_ref)),
                    "matched_phrase": representative_mapping.get("matched_phrase"),
                    "source": resolution.get("source"),
                    "manifest_row": manifest_row,
                }
            )
            if _text(resolution.get("source")) == "linkedin_personal_answer_fallback":
                personal_answer_fallbacks_used.append(
                    {
                        "canonical_key": canonical_key,
                        "label": _ref_prompt_label(chosen_ref),
                        "value": desired_value,
                        "reason": "required_no_visible_neutral_option",
                    }
                )
            answers_applied.append(
                {
                    "canonical_key": canonical_key,
                    "label": _ref_prompt_label(chosen_ref),
                    "field_type": chosen_ref.field_type,
                    "source": resolution.get("source"),
                    "confidence": round(confidence, 4),
                }
            )
            answer_confidences.append(confidence)
            if required:
                required_fields_filled.append(canonical_key)
        elif required:
            missing_reason = _text(resolution.get("reason") or resolution.get("source") or "unmapped_required_field")
            if _text(resolution.get("source")) == "linkedin_safe_self_id_default":
                missing_reason = "no_safe_neutral_option_available"
            missing_required_fields.append(
                {
                    "canonical_key": canonical_key,
                    "label": _ref_prompt_label(representative_ref),
                    "reason": (
                        "required_personal_answer_fallback_unmatched"
                        if _text(resolution.get("source")) == "linkedin_personal_answer_fallback"
                        else missing_reason
                    ),
                    "confidence": round(confidence, 4),
                }
            )
        elif resolution.get("action") == "skip":
            safe_skips.append(
                {
                    "canonical_key": canonical_key,
                    "label": _ref_prompt_label(representative_ref),
                    "reason": _text(resolution.get("source") or "safe_skip"),
                }
            )
        self_id_handling_modes.append(_text(resolution.get("self_id_handling_mode")) or ("review" if is_self_id_key(canonical_key) else "standard"))
        if _text(resolution.get("source")).startswith("linkedin_"):
            policy_matches.append(
                {
                    "canonical_key": canonical_key,
                    "label": _ref_prompt_label(representative_ref),
                    "source": resolution.get("source"),
                }
            )
        answer_mappings.append(
            {
                "canonical_key": canonical_key,
                "label": _ref_prompt_label(representative_ref),
                "required": required,
                "source": resolution.get("source"),
                "action": resolution.get("action"),
                "confidence": round(confidence, 4),
                "matched_phrase": representative_mapping.get("matched_phrase"),
                "normalized_label": representative_mapping.get("normalized_label"),
                "value_preview": None if resolution.get("action") == "answer" else None,
            }
        )

    seen_unmapped_groups: set[str] = set()
    for ref in refs:
        if ref.ref in used_refs or ref.ref in mapped_ref_ids or ref.field_type not in {"text", "select", "radio", "checkbox"}:
            continue
        group_key = _ref_question_group_key(ref)
        if group_key in seen_unmapped_groups:
            continue
        seen_unmapped_groups.add(group_key)
        required = _field_is_required(ref)
        if _looks_like_self_id_ref(ref):
            if required:
                missing_required_fields.append(
                    {
                        "canonical_key": "ambiguous_self_id",
                        "label": _ref_prompt_label(ref),
                        "reason": "ambiguous_required_self_id_field",
                        "confidence": 0.0,
                    }
                )
                self_id_handling_modes.append("review")
                answer_mappings.append(
                    {
                        "canonical_key": None,
                        "label": _ref_prompt_label(ref),
                        "required": True,
                        "source": "ambiguous_self_id",
                        "action": "review",
                        "confidence": 0.0,
                        "matched_phrase": None,
                        "normalized_label": _normalize_label_text(_ref_search_text(ref)),
                        "value_preview": None,
                    }
                )
            else:
                self_id_handling_modes.append("skip_optional")
                answer_mappings.append(
                    {
                        "canonical_key": None,
                        "label": _ref_prompt_label(ref),
                        "required": False,
                        "source": "unmapped_optional_self_id",
                        "action": "skip",
                        "confidence": 0.5,
                        "matched_phrase": None,
                        "normalized_label": _normalize_label_text(_ref_search_text(ref)),
                        "value_preview": None,
                    }
                )
            continue
        if required and _looks_like_known_question_ref(ref):
            missing_required_fields.append(
                {
                    "canonical_key": "ambiguous_required_field",
                    "label": _ref_prompt_label(ref),
                    "reason": "ambiguous_required_field",
                    "confidence": 0.0,
                }
            )
            answer_mappings.append(
                {
                    "canonical_key": None,
                    "label": _ref_prompt_label(ref),
                    "required": True,
                    "source": "ambiguous_required_field",
                    "action": "review",
                    "confidence": 0.0,
                    "matched_phrase": None,
                    "normalized_label": _normalize_label_text(_ref_search_text(ref)),
                    "value_preview": None,
                }
            )

    self_id_mode = "standard"
    if any(mode == "review" for mode in self_id_handling_modes):
        self_id_mode = "review"
    elif any(mode == "truthful_personal_fallback" for mode in self_id_handling_modes):
        self_id_mode = "truthful_personal_fallback"
    elif any(mode == "safe_neutral_default" for mode in self_id_handling_modes):
        self_id_mode = "safe_neutral_default"
    elif any(mode == "direct_default" for mode in self_id_handling_modes):
        self_id_mode = "direct_default"
    elif any(mode == "skip_optional" for mode in self_id_handling_modes):
        self_id_mode = "skip_optional"

    return {
        "fill_payloads": fill_payloads,
        "select_actions": select_actions,
        "execution_actions": execution_actions,
        "manifest_rows": manifest_rows,
        "answer_mappings": answer_mappings,
        "policy_matches": policy_matches,
        "answers_applied": answers_applied,
        "safe_skips": safe_skips,
        "personal_answer_fallbacks_used": personal_answer_fallbacks_used,
        "missing_required_fields": missing_required_fields,
        "required_fields_filled": required_fields_filled,
        "self_id_handling_mode": self_id_mode,
        "answer_confidences": answer_confidences,
        "covered_explicit_canonical_keys": sorted(covered_explicit_canonical_keys),
    }


def _contact_execution_actions(actions: list[ContactFieldAction]) -> list[dict[str, Any]]:
    execution_actions: list[dict[str, Any]] = []
    for action in actions:
        candidate = action.candidate
        if action.action != "fill":
            continue
        manifest_row = {
            "field_name": candidate.field_name,
            "label": candidate.label,
            "field_type": candidate.field_type,
            "status": "selected" if candidate.field_type == "select" else ("checked" if candidate.field_type == "radio" else "filled"),
            "value_preview": None,
            "value_redacted": True,
        }
        execution_actions.append(
            {
                "action_type": "select" if candidate.field_type == "select" else "fill",
                "field_type": candidate.field_type,
                "original_ref": candidate.ref.ref,
                "value": action.value,
                "field_name": candidate.field_name,
                "label": candidate.label,
                "normalized_label": _normalize_label_text(_ref_search_text(candidate.ref)),
                "source": "contact_profile",
                "manifest_row": manifest_row,
            }
        )
    return execution_actions


def _stale_ref_error(exc: BrowserCommandError) -> bool:
    command_debug = exc.command_debug if isinstance(exc.command_debug, dict) else {}
    combined = _combine_text(
        exc.blocking_reason,
        exc.stage,
        exc.error_kind,
        _text(command_debug.get("stderr")),
        _text(command_debug.get("stdout")),
    )
    return "not found or not visible" in combined or ("element" in combined and "not found" in combined)


def _action_ref_match_score(ref: SnapshotRef, descriptor: dict[str, Any], *, application_target: dict[str, Any]) -> int:
    candidate_text = _normalize_label_text(_ref_search_text(ref))
    descriptor_text = _normalize_label_text(_text(descriptor.get("normalized_label") or descriptor.get("label")))
    score = 0
    if descriptor_text and candidate_text == descriptor_text:
        score += 12
    elif descriptor_text and (descriptor_text in candidate_text or candidate_text in descriptor_text):
        score += 8
    if descriptor_text:
        score += _keyword_score(candidate_text, _tokenize(descriptor_text)[:8])
    raw_label = _text(descriptor.get("label"))
    if raw_label:
        score += _keyword_score(_ref_search_text(ref), _tokenize(raw_label)[:8])
    canonical_key = _text(descriptor.get("canonical_key"))
    if canonical_key:
        mapping = _mapping_for_ref(ref, application_target=application_target)
        if isinstance(mapping, dict) and _text(mapping.get("canonical_key")) == canonical_key:
            score += 16
            matched_phrase = _text(descriptor.get("matched_phrase"))
            if matched_phrase and _text(mapping.get("matched_phrase")) == matched_phrase:
                score += 4
    if _text(descriptor.get("original_ref")) == ref.ref:
        score += 2
    return score


def _resolve_live_action_ref(
    *,
    descriptor: dict[str, Any],
    refs: list[SnapshotRef],
    application_target: dict[str, Any],
    contact_values: dict[str, Any],
) -> SnapshotRef | None:
    if not refs:
        return None
    current_ref_lookup = {ref.ref: ref for ref in refs}
    action_type = _text(descriptor.get("action_type"))
    field_type = _text(descriptor.get("field_type"))
    original_ref = _text(descriptor.get("original_ref"))
    desired_value = descriptor.get("value")

    if action_type == "click":
        raw_label = _text(descriptor.get("label"))
        keywords = _tokenize(raw_label)[:6] or ["next", "continue"]
        disallowed = ["submit", "review", "dismiss", "close", "cancel", "save"]
        candidate = _find_clickable_ref(refs, keywords=keywords, disallowed_keywords=disallowed)
        if candidate is not None:
            return candidate
        return current_ref_lookup.get(original_ref)

    field_name = _text(descriptor.get("field_name"))
    if field_name:
        contact_candidates = [
            action.candidate.ref
            for action in _plan_contact_field_actions(refs=refs, contact_values=contact_values, used_refs=set())
            if action.candidate.field_name == field_name
        ]
        if field_type in {"radio", "checkbox"}:
            desired_value_text = _text(desired_value)
            matches = [
                ref for ref in contact_candidates if ref.field_type == field_type and _option_matches_desired_value(ref.label, desired_value_text)
            ]
            if matches:
                return max(matches, key=lambda ref: _action_ref_match_score(ref, descriptor, application_target=application_target))
        typed_candidates = [ref for ref in contact_candidates if not field_type or ref.field_type == field_type]
        if typed_candidates:
            return max(typed_candidates, key=lambda ref: _action_ref_match_score(ref, descriptor, application_target=application_target))

    canonical_key = _text(descriptor.get("canonical_key"))
    if canonical_key:
        mapped_candidates = []
        for ref in refs:
            if field_type and ref.field_type != field_type:
                continue
            mapping = _mapping_for_ref(ref, application_target=application_target)
            if isinstance(mapping, dict) and _text(mapping.get("canonical_key")) == canonical_key:
                mapped_candidates.append(ref)
        if field_type in {"radio", "checkbox"}:
            desired_value_text = _text(desired_value)
            option_matches = [ref for ref in mapped_candidates if _option_matches_desired_value(ref.label, desired_value_text)]
            if option_matches:
                return max(option_matches, key=lambda ref: _action_ref_match_score(ref, descriptor, application_target=application_target))
        elif mapped_candidates:
            return max(mapped_candidates, key=lambda ref: _action_ref_match_score(ref, descriptor, application_target=application_target))

    if original_ref and original_ref in current_ref_lookup:
        current_ref = current_ref_lookup[original_ref]
        if not field_type or current_ref.field_type == field_type:
            return current_ref

    fallback_candidates = [ref for ref in refs if not field_type or ref.field_type == field_type]
    if field_type in {"radio", "checkbox"}:
        desired_value_text = _text(desired_value)
        fallback_candidates = [
            ref for ref in fallback_candidates if _option_matches_desired_value(ref.label, desired_value_text)
        ]
    if fallback_candidates:
        return max(fallback_candidates, key=lambda ref: _action_ref_match_score(ref, descriptor, application_target=application_target))
    return None


def _submit_decision(
    *,
    answer_profile: dict[str, Any],
    missing_required_fields: list[dict[str, Any]],
    answer_confidences: list[float],
    review_step_visible: bool,
) -> dict[str, Any]:
    min_confidence = float(answer_profile.get("auto_submit_min_confidence") or DEFAULT_AUTO_SUBMIT_MIN_CONFIDENCE)
    score = min(answer_confidences) if answer_confidences else 0.0
    if not bool(answer_profile.get("auto_submit_allowed", True)):
        return {
            "should_auto_submit": False,
            "confidence_score": round(score, 4),
            "min_confidence": round(min_confidence, 4),
            "reason": "auto_submit_disabled",
        }
    if missing_required_fields:
        return {
            "should_auto_submit": False,
            "confidence_score": round(score, 4),
            "min_confidence": round(min_confidence, 4),
            "reason": "missing_required_fields",
        }
    if not review_step_visible:
        return {
            "should_auto_submit": False,
            "confidence_score": round(score, 4),
            "min_confidence": round(min_confidence, 4),
            "reason": "not_at_review_step",
        }
    if score < min_confidence:
        return {
            "should_auto_submit": False,
            "confidence_score": round(score, 4),
            "min_confidence": round(min_confidence, 4),
            "reason": "confidence_below_threshold",
        }
    return {
        "should_auto_submit": True,
        "confidence_score": round(score, 4),
        "min_confidence": round(min_confidence, 4),
        "reason": "confidence_threshold_met",
    }


def _browser_metadata(client: OpenClawBrowserClient) -> tuple[str, str]:
    page_title = _text(client.evaluate_json("() => JSON.stringify(document.title || '')"))
    current_url = _text(client.evaluate_json("() => JSON.stringify(window.location.href || '')"))
    return page_title, current_url


def _debug_payload(
    client: OpenClawBrowserClient | Any,
    runtime_config: BrowserRuntimeConfig,
    *,
    attach_probe_succeeded: bool,
    start_attempted: bool,
    start_used: bool,
    last_error: BrowserCommandError | None,
    screenshot_failures: list[dict[str, Any]],
    linkedin_progression: list[dict[str, Any]],
) -> dict[str, Any]:
    command_debug = client.command_debug() if hasattr(client, "command_debug") and callable(client.command_debug) else []
    return {
        "browser_runtime": {
            "run_on_host": runtime_config.run_on_host,
            "attach_mode": runtime_config.attach_mode,
            "skip_browser_start": runtime_config.skip_browser_start,
            "allow_browser_start": runtime_config.allow_browser_start,
            "gateway_url": runtime_config.gateway_url,
            "cdp_url": runtime_config.cdp_url,
            "gateway_token_present": runtime_config.gateway_token_present,
            "host_gateway_alias": runtime_config.host_gateway_alias,
            "running_in_docker": runtime_config.running_in_docker,
            "base_command": _redact_command([part for part in shlex.split(runtime_config.command) if part.strip()]),
            "attach_probe_succeeded": attach_probe_succeeded,
            "start_attempted": start_attempted,
            "start_used": start_used,
            "last_error_stage": last_error.stage if last_error else None,
            "last_error_kind": last_error.error_kind if last_error else None,
            "screenshot_failures": screenshot_failures,
        },
        "openclaw_commands": command_debug,
        "linkedin_progression": linkedin_progression,
    }


def run_backend(
    payload: dict[str, Any],
    *,
    client: OpenClawBrowserClient | None = None,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    logger = logger or _configure_logger()
    constraints = payload.get("constraints") if isinstance(payload.get("constraints"), dict) else {}
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}
    target = payload.get("application_target") if isinstance(payload.get("application_target"), dict) else {}
    resume_variant = payload.get("resume_variant") if isinstance(payload.get("resume_variant"), dict) else {}
    answers = payload.get("application_answers") if isinstance(payload.get("application_answers"), list) else []
    auth = payload.get("auth") if isinstance(payload.get("auth"), dict) else {}

    application_url = _text(target.get("application_url") or target.get("source_url"))
    screenshot_dir = Path(_text(artifacts.get("screenshot_dir")) or ".").resolve()
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    run_key = _text(artifacts.get("run_key")) or "application-draft"
    capture_screenshots = _as_bool(payload.get("capture_screenshots"), default=True)
    max_screenshots = max(0, int(payload.get("max_screenshots") or 8))
    inspect_only = _as_bool(payload.get("inspect_only"), default=_as_bool(constraints.get("inspect_only")))

    if not application_url:
        return invalid_input_result(["missing_application_url"])
    if _as_bool(payload.get("submit")) or not _as_bool(payload.get("stop_before_submit"), default=True):
        return _result(
            draft_status="not_started",
            source_status="unsafe_submit_attempted",
            awaiting_review=False,
            review_status="blocked",
            failure_category="unsafe_submit_attempted",
            blocking_reason="Backend-level no-submit guard rejected a submit-capable request.",
            errors=["backend_no_submit_guard_rejected_request"],
        )

    timeout_seconds = max(5, int(constraints.get("timeout_seconds") or 240))
    runtime_config = _resolve_runtime_config(payload)
    client = client or OpenClawBrowserClient(command=runtime_config.command, timeout_ms=timeout_seconds * 1000, logger=logger)

    warnings: list[str] = []
    errors: list[str] = []
    fields_filled_manifest: list[dict[str, Any]] = []
    screenshots: list[dict[str, Any]] = []
    checkpoint_urls: list[str] = [application_url]
    staged_upload_path: Path | None = None
    screenshot_failures: list[dict[str, Any]] = []
    linkedin_progression: list[dict[str, Any]] = []
    linkedin_contact_step_diagnostics: dict[str, Any] = {}
    linkedin_resume_step_diagnostics: dict[str, Any] = {}
    linkedin_later_step_diagnostics: dict[str, Any] = {}
    linkedin_last_step_signature: str | None = None
    linkedin_repeated_signature_count = 0
    linkedin_action_budget_by_signature: dict[str, int] = {}
    linkedin_later_step_iteration_count = 0
    linkedin_last_action_attempted: str | None = None
    linkedin_last_field_targeted: str | None = None
    linkedin_last_policy_match: dict[str, Any] | None = None
    linkedin_last_visible_labels: list[str] = []
    linkedin_last_progress_percent: int | None = None
    linkedin_repeated_state_detected = False
    linkedin_repeated_state_reason: str | None = None
    radio_selection_attempts: dict[str, dict[str, Any]] = {}
    recorded_contact_manifest_keys: set[tuple[str, str, str]] = set()
    answer_profile = build_default_answer_profile(payload)
    generic_answer_diagnostics: dict[str, Any] = {
        "answer_mappings": [],
        "missing_required_fields": [],
        "required_fields_filled": [],
        "self_id_handling_mode": "standard",
        "submit_decision": {
            "should_auto_submit": False,
            "confidence_score": 0.0,
            "min_confidence": round(float(answer_profile.get("auto_submit_min_confidence") or DEFAULT_AUTO_SUBMIT_MIN_CONFIDENCE), 4),
            "reason": "not_evaluated",
        },
    }
    page_diagnostics: dict[str, Any] = {}
    form_diagnostics: dict[str, Any] = {}
    attach_probe_succeeded = False
    start_attempted = False
    start_used = False
    last_error: BrowserCommandError | None = None
    contact_values = _extract_contact_values(payload)
    for contact_key, profile_key in (
        ("first_name", "first_name"),
        ("last_name", "last_name"),
        ("email_address", "email"),
        ("city", "city"),
        ("state_or_province", "state_or_province"),
        ("postal_code", "postal_code"),
        ("country", "country"),
        ("primary_phone_number", "primary_phone_number"),
        ("phone_type", "phone_type"),
    ):
        contact_values[contact_key] = contact_values.get(contact_key) or _text(answer_profile.get(profile_key))

    def build_debug_json() -> dict[str, Any]:
        return _debug_payload(
            client,
            runtime_config,
            attach_probe_succeeded=attach_probe_succeeded,
            start_attempted=start_attempted,
            start_used=start_used,
            last_error=last_error,
            screenshot_failures=screenshot_failures,
            linkedin_progression=linkedin_progression,
        )

    def merge_contact_step_diagnostics(current_page_diagnostics: dict[str, Any]) -> None:
        if linkedin_contact_step_diagnostics:
            current_page_diagnostics.update(linkedin_contact_step_diagnostics)

    def merge_linkedin_step_diagnostics(current_page_diagnostics: dict[str, Any]) -> None:
        merge_contact_step_diagnostics(current_page_diagnostics)
        if linkedin_resume_step_diagnostics:
            current_page_diagnostics.update(linkedin_resume_step_diagnostics)
        if linkedin_later_step_diagnostics:
            current_page_diagnostics.update(linkedin_later_step_diagnostics)

    def update_contact_step_diagnostics(
        current_page_diagnostics: dict[str, Any],
        current_form_diagnostics: dict[str, Any],
        *,
        next_clicked: bool | None = None,
        next_not_clicked_reason: str | None = None,
    ) -> None:
        next_button_ref = _text(current_page_diagnostics.get("next_button_ref") or current_page_diagnostics.get("next_ref")) or None
        next_button_label = _text(current_page_diagnostics.get("next_button_label") or current_page_diagnostics.get("next_ref_label")) or None
        diagnostics = {
            "next_button_ref": next_button_ref,
            "next_button_label": next_button_label,
            "next_button_clicked": bool(next_clicked) if next_clicked is not None else bool(linkedin_contact_step_diagnostics.get("next_button_clicked", False)),
            "next_button_not_clicked_reason": next_not_clicked_reason,
            "blocking_skipped_fields": list(current_form_diagnostics.get("blocking_skipped_fields") or []),
            "nonblocking_skipped_fields": list(current_form_diagnostics.get("nonblocking_skipped_fields") or []),
            "radio_group_diagnostics": list(current_form_diagnostics.get("radio_group_diagnostics") or []),
        }
        if next_clicked:
            diagnostics["next_button_not_clicked_reason"] = None
        elif diagnostics["next_button_not_clicked_reason"] is None:
            if not next_button_ref:
                diagnostics["next_button_not_clicked_reason"] = "next_button_not_detected"
            elif diagnostics["blocking_skipped_fields"]:
                diagnostics["next_button_not_clicked_reason"] = "blocking_required_contact_fields"
            else:
                diagnostics["next_button_not_clicked_reason"] = "next_button_not_clicked"
        linkedin_contact_step_diagnostics.update(diagnostics)
        current_page_diagnostics.update(linkedin_contact_step_diagnostics)

    def update_resume_step_diagnostics(
        current_page_diagnostics: dict[str, Any],
        *,
        continue_button_ref: str | None = None,
        continue_button_label: str | None = None,
        continue_clicked: bool | None = None,
        continue_verified: bool | None = None,
    ) -> None:
        selected_resume_detected = bool(
            current_page_diagnostics.get("selected_resume_detected")
            or linkedin_resume_step_diagnostics.get("selected_resume_detected", False)
        )
        selected_resume_label = (
            _text(current_page_diagnostics.get("selected_resume_label"))
            or _text(linkedin_resume_step_diagnostics.get("selected_resume_label"))
            or None
        )
        selected_resume_verified = bool(
            current_page_diagnostics.get("selected_resume_verified")
            or linkedin_resume_step_diagnostics.get("selected_resume_verified", False)
        )
        resolved_continue_ref = (
            _text(
                continue_button_ref
                or current_page_diagnostics.get("continue_button_ref")
                or current_page_diagnostics.get("next_button_ref")
                or current_page_diagnostics.get("next_ref")
                or linkedin_resume_step_diagnostics.get("continue_button_ref")
            )
            or None
        )
        resolved_continue_label = (
            _text(
                continue_button_label
                or current_page_diagnostics.get("continue_button_label")
                or current_page_diagnostics.get("next_button_label")
                or current_page_diagnostics.get("next_ref_label")
                or linkedin_resume_step_diagnostics.get("continue_button_label")
            )
            or None
        )
        upload_required_value = current_page_diagnostics.get("upload_required")
        if upload_required_value is None and "upload_required" in linkedin_resume_step_diagnostics:
            upload_required = bool(linkedin_resume_step_diagnostics.get("upload_required"))
        else:
            upload_required = bool(upload_required_value) if upload_required_value is not None else not selected_resume_verified
        diagnostics = {
            "selected_resume_detected": selected_resume_detected,
            "selected_resume_label": selected_resume_label,
            "selected_resume_verified": selected_resume_verified,
            "upload_required": upload_required,
            "continue_button_ref": resolved_continue_ref,
            "continue_button_label": resolved_continue_label,
            "continue_clicked": (
                bool(continue_clicked)
                if continue_clicked is not None
                else bool(linkedin_resume_step_diagnostics.get("continue_clicked", False))
            ),
            "continue_verified": (
                bool(continue_verified)
                if continue_verified is not None
                else bool(linkedin_resume_step_diagnostics.get("continue_verified", False))
            ),
        }
        linkedin_resume_step_diagnostics.update(diagnostics)
        current_page_diagnostics.update(linkedin_resume_step_diagnostics)

    def _extend_unique_rows(target_key: str, rows: list[dict[str, Any]]) -> None:
        existing = list(linkedin_later_step_diagnostics.get(target_key) or [])
        seen = {json.dumps(row, sort_keys=True, default=str) for row in existing if isinstance(row, dict)}
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_key = json.dumps(row, sort_keys=True, default=str)
            if row_key in seen:
                continue
            existing.append(row)
            seen.add(row_key)
        linkedin_later_step_diagnostics[target_key] = existing[-20:]

    def update_later_step_diagnostics(
        current_page_diagnostics: dict[str, Any],
        refs: list[SnapshotRef],
        *,
        policy_matches: list[dict[str, Any]] | None = None,
        answers_applied: list[dict[str, Any]] | None = None,
        safe_skips: list[dict[str, Any]] | None = None,
        optional_steps_skipped: list[dict[str, Any]] | None = None,
        personal_answer_fallbacks: list[dict[str, Any]] | None = None,
        action_diagnostics: list[dict[str, Any]] | None = None,
        iteration_count: int | None = None,
        repeated_signature_count: int | None = None,
        last_step_signature: str | None = None,
        last_action_attempted: str | None = None,
        last_field_targeted: str | None = None,
        last_policy_match: dict[str, Any] | None = None,
        last_visible_labels: list[str] | None = None,
        last_progress_percent: int | None = None,
        repeated_state_detected: bool | None = None,
        repeated_state_reason: str | None = None,
        review_like_step_detected: bool | None = None,
    ) -> None:
        if policy_matches:
            _extend_unique_rows("later_step_policy_matches", policy_matches)
        if answers_applied:
            _extend_unique_rows("later_step_answers_applied", answers_applied)
        if safe_skips:
            _extend_unique_rows("later_step_safe_skips", safe_skips)
        if optional_steps_skipped:
            _extend_unique_rows("later_step_optional_steps_skipped", optional_steps_skipped)
        if personal_answer_fallbacks:
            _extend_unique_rows("later_step_personal_answer_fallbacks_used", personal_answer_fallbacks)
        if action_diagnostics:
            _extend_unique_rows("later_step_action_diagnostics", action_diagnostics)
        submit_ref = _find_clickable_ref(
            refs,
            keywords=["submit", "finish application", "apply now", "send application"],
            disallowed_keywords=["save", "dismiss", "cancel", "close"],
        )
        review_step_detected = bool(current_page_diagnostics.get("linkedin_state") == "easy_apply_review_step")
        submit_visible = bool(submit_ref or current_page_diagnostics.get("submit_indicators_detected"))
        linkedin_later_step_diagnostics.update(
            {
                "later_step_policy_matches": list(linkedin_later_step_diagnostics.get("later_step_policy_matches") or []),
                "later_step_answers_applied": list(linkedin_later_step_diagnostics.get("later_step_answers_applied") or []),
                "later_step_safe_skips": list(linkedin_later_step_diagnostics.get("later_step_safe_skips") or []),
                "later_step_optional_steps_skipped": list(
                    linkedin_later_step_diagnostics.get("later_step_optional_steps_skipped") or []
                ),
                "later_step_personal_answer_fallbacks_used": list(
                    linkedin_later_step_diagnostics.get("later_step_personal_answer_fallbacks_used") or []
                ),
                "later_step_action_diagnostics": list(linkedin_later_step_diagnostics.get("later_step_action_diagnostics") or []),
                "later_step_iteration_count": (
                    int(iteration_count)
                    if iteration_count is not None
                    else int(linkedin_later_step_diagnostics.get("later_step_iteration_count") or 0)
                ),
                "repeated_signature_count": (
                    int(repeated_signature_count)
                    if repeated_signature_count is not None
                    else int(linkedin_later_step_diagnostics.get("repeated_signature_count") or 0)
                ),
                "last_step_signature": (
                    _text(last_step_signature)
                    or _text(linkedin_later_step_diagnostics.get("last_step_signature"))
                    or None
                ),
                "last_action_attempted": (
                    _text(last_action_attempted)
                    or _text(linkedin_later_step_diagnostics.get("last_action_attempted"))
                    or None
                ),
                "last_field_targeted": (
                    _text(last_field_targeted)
                    or _text(linkedin_later_step_diagnostics.get("last_field_targeted"))
                    or None
                ),
                "last_policy_match": (
                    dict(last_policy_match)
                    if isinstance(last_policy_match, dict)
                    else linkedin_later_step_diagnostics.get("last_policy_match")
                ),
                "last_visible_labels": list(last_visible_labels)
                if last_visible_labels is not None
                else list(linkedin_later_step_diagnostics.get("last_visible_labels") or []),
                "last_progress_percent": (
                    int(last_progress_percent)
                    if last_progress_percent is not None
                    else linkedin_later_step_diagnostics.get("last_progress_percent")
                ),
                "repeated_state_detected": (
                    bool(repeated_state_detected)
                    if repeated_state_detected is not None
                    else bool(linkedin_later_step_diagnostics.get("repeated_state_detected", False))
                ),
                "repeated_state_reason": (
                    _text(repeated_state_reason)
                    or _text(linkedin_later_step_diagnostics.get("repeated_state_reason"))
                    or None
                ),
                "review_step_detected": review_step_detected,
                "submit_visible": submit_visible,
                "review_like_step_detected": (
                    bool(review_like_step_detected)
                    if review_like_step_detected is not None
                    else bool(linkedin_later_step_diagnostics.get("review_like_step_detected", False))
                ),
                "submit_ready_without_autosubmit": bool((review_step_detected or review_like_step_detected) and submit_visible),
            }
        )
        current_page_diagnostics.update(linkedin_later_step_diagnostics)

    def sync_later_step_runtime_diagnostics(
        current_page_diagnostics: dict[str, Any],
        refs: list[SnapshotRef],
        *,
        signature_info: dict[str, Any] | None = None,
        snapshot_text_value: str | None = None,
    ) -> dict[str, Any]:
        nonlocal linkedin_last_visible_labels, linkedin_last_progress_percent
        signature_info = signature_info or _linkedin_step_signature(
            snapshot_text_value if snapshot_text_value is not None else snapshot_text,
            refs,
            current_page_diagnostics,
        )
        linkedin_last_visible_labels = list(signature_info.get("visible_labels") or [])
        linkedin_last_progress_percent = signature_info.get("progress_percent")
        update_later_step_diagnostics(
            current_page_diagnostics,
            refs,
            iteration_count=linkedin_later_step_iteration_count,
            repeated_signature_count=linkedin_repeated_signature_count,
            last_step_signature=_text(signature_info.get("signature")) or linkedin_last_step_signature,
            last_action_attempted=linkedin_last_action_attempted,
            last_field_targeted=linkedin_last_field_targeted,
            last_policy_match=linkedin_last_policy_match,
            last_visible_labels=linkedin_last_visible_labels,
            last_progress_percent=linkedin_last_progress_percent,
            repeated_state_detected=linkedin_repeated_state_detected,
            repeated_state_reason=linkedin_repeated_state_reason,
            review_like_step_detected=bool(signature_info.get("review_like")),
        )
        return signature_info

    def capture_live_state() -> dict[str, Any]:
        live_page_title, live_current_url = _browser_metadata(client)
        live_checkpoint_urls = _dedupe([application_url, live_current_url])
        live_snapshot_text = client.snapshot()
        live_refs, live_upload_ref, live_contact_actions, live_form_diagnostics = analyze_form(live_snapshot_text)
        live_page_diagnostics = _page_diagnostics(
            application_url=application_url,
            current_url=live_current_url,
            page_title=live_page_title,
            snapshot_text=live_snapshot_text,
            refs=live_refs,
            upload_ref=live_upload_ref,
        )
        live_linkedin_context = _linkedin_step_context(
            snapshot_text=live_snapshot_text,
            refs=live_refs,
            upload_ref=live_upload_ref,
            contact_field_actions=live_contact_actions,
            page_diagnostics=live_page_diagnostics,
        )
        live_page_diagnostics.update(live_linkedin_context)
        live_page_diagnostics["linkedin_state"] = live_linkedin_context["state"]
        merge_linkedin_step_diagnostics(live_page_diagnostics)
        if _is_linkedin_easy_apply_target(payload):
            sync_later_step_runtime_diagnostics(
                live_page_diagnostics,
                live_refs,
                snapshot_text_value=live_snapshot_text,
            )
        return {
            "page_title": live_page_title,
            "current_url": live_current_url,
            "checkpoint_urls": live_checkpoint_urls,
            "snapshot_text": live_snapshot_text,
            "refs": live_refs,
            "upload_ref": live_upload_ref,
            "contact_field_actions": live_contact_actions,
            "form_diagnostics": live_form_diagnostics,
            "page_diagnostics": live_page_diagnostics,
        }

    def _stale_action_resolution_error(descriptor: dict[str, Any], *, retry_attempted: bool) -> BrowserCommandError:
        return BrowserCommandError(
            failure_category="manual_review_required",
            blocking_reason="LinkedIn later-step automation could not safely re-resolve a field after the form re-rendered.",
            errors=[
                "linkedin_later_step_action_ref_unresolved_after_retry"
                if retry_attempted
                else "linkedin_later_step_action_ref_unresolved"
            ],
            safe_to_retry=False,
            stage="linkedin_later_step_action_recovery",
            error_kind="stale_ref_unresolved",
            command_debug={
                "original_ref": _text(descriptor.get("original_ref")),
                "action_attempted": _text(descriptor.get("action_type")),
                "label": _text(descriptor.get("label")),
            },
        )

    def execute_linkedin_live_action(descriptor: dict[str, Any], *, current_state: dict[str, Any] | None = None) -> dict[str, Any]:
        nonlocal page_title, current_url, checkpoint_urls, snapshot_text, refs, upload_ref, contact_field_actions, form_diagnostics, page_diagnostics
        nonlocal linkedin_last_action_attempted, linkedin_last_field_targeted
        action_diag = {
            "action_attempted": _text(descriptor.get("action_type")) or "unknown",
            "stale_ref_detected": False,
            "ref_re_resolved": False,
            "retry_attempted": False,
            "retry_succeeded": False,
            "original_ref": _text(descriptor.get("original_ref")) or None,
            "replacement_ref": None,
            "label": _text(descriptor.get("label")) or None,
            "canonical_key": _text(descriptor.get("canonical_key")) or _text(descriptor.get("field_name")) or None,
        }
        linkedin_last_action_attempted = action_diag["action_attempted"]
        linkedin_last_field_targeted = (
            _text(descriptor.get("canonical_key"))
            or _text(descriptor.get("field_name"))
            or _text(descriptor.get("label"))
            or None
        )

        def remember_state(state: dict[str, Any]) -> None:
            nonlocal page_title, current_url, checkpoint_urls, snapshot_text, refs, upload_ref, contact_field_actions, form_diagnostics, page_diagnostics
            page_title = state["page_title"]
            current_url = state["current_url"]
            checkpoint_urls = state["checkpoint_urls"]
            snapshot_text = state["snapshot_text"]
            refs = state["refs"]
            upload_ref = state["upload_ref"]
            contact_field_actions = state["contact_field_actions"]
            form_diagnostics = state["form_diagnostics"]
            page_diagnostics = state["page_diagnostics"]

        def perform_action(target_ref: SnapshotRef) -> None:
            action_type = _text(descriptor.get("action_type"))
            field_type = _text(descriptor.get("field_type")) or _text(target_ref.field_type)
            if action_type == "select":
                client.select(target_ref.ref, str(descriptor.get("value")))
                return
            if action_type == "click":
                client.click(target_ref.ref)
                return
            client.fill([{"ref": target_ref.ref, "value": descriptor.get("value"), "type": field_type}])

        live_state = current_state or capture_live_state()
        remember_state(live_state)
        sync_later_step_runtime_diagnostics(
            live_state["page_diagnostics"],
            live_state["refs"],
            snapshot_text_value=live_state["snapshot_text"],
        )
        resolved_ref = _resolve_live_action_ref(
            descriptor=descriptor,
            refs=live_state["refs"],
            application_target=target,
            contact_values=contact_values,
        )
        if resolved_ref is None:
            update_later_step_diagnostics(live_state["page_diagnostics"], live_state["refs"], action_diagnostics=[action_diag])
            raise _stale_action_resolution_error(descriptor, retry_attempted=False)
        action_diag["replacement_ref"] = resolved_ref.ref
        action_diag["ref_re_resolved"] = resolved_ref.ref != action_diag["original_ref"]
        try:
            perform_action(resolved_ref)
        except BrowserCommandError as exc:
            if not _stale_ref_error(exc):
                update_later_step_diagnostics(live_state["page_diagnostics"], live_state["refs"], action_diagnostics=[action_diag])
                raise
            action_diag["stale_ref_detected"] = True
            action_diag["retry_attempted"] = True
            retry_state = capture_live_state()
            remember_state(retry_state)
            retry_ref = _resolve_live_action_ref(
                descriptor=descriptor,
                refs=retry_state["refs"],
                application_target=target,
                contact_values=contact_values,
            )
            if retry_ref is None:
                update_later_step_diagnostics(retry_state["page_diagnostics"], retry_state["refs"], action_diagnostics=[action_diag])
                raise _stale_action_resolution_error(descriptor, retry_attempted=True) from exc
            action_diag["replacement_ref"] = retry_ref.ref
            action_diag["ref_re_resolved"] = retry_ref.ref != action_diag["original_ref"]
            try:
                perform_action(retry_ref)
            except BrowserCommandError:
                update_later_step_diagnostics(retry_state["page_diagnostics"], retry_state["refs"], action_diagnostics=[action_diag])
                raise
            action_diag["retry_succeeded"] = True
            final_state = capture_live_state()
            remember_state(final_state)
            update_later_step_diagnostics(final_state["page_diagnostics"], final_state["refs"], action_diagnostics=[action_diag])
            return final_state
        final_state = capture_live_state()
        remember_state(final_state)
        update_later_step_diagnostics(final_state["page_diagnostics"], final_state["refs"], action_diagnostics=[action_diag])
        return final_state

    def append_contact_manifest_rows(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            field_name = _text(row.get("field_name"))
            label = _text(row.get("label"))
            status = _text(row.get("status"))
            key = (field_name, label, status)
            if not field_name or key in recorded_contact_manifest_keys:
                continue
            recorded_contact_manifest_keys.add(key)
            fields_filled_manifest.append(row)

    def record_contact_refresh(
        *,
        trigger_operation: str,
        executed_ref: str,
        contact_actions: list[ContactFieldAction],
    ) -> None:
        refreshes = list(linkedin_contact_step_diagnostics.get("contact_snapshot_refreshes") or [])
        re_resolved_refs = [action.candidate.ref.ref for action in contact_actions if action.action in {"fill", "prefilled_verified"}]
        refreshes.append(
            {
                "trigger_operation": trigger_operation,
                "executed_ref": executed_ref,
                "re_resolved_refs": re_resolved_refs[:20],
            }
        )
        linkedin_contact_step_diagnostics.update(
            {
                "contact_snapshot_refreshed": True,
                "contact_snapshot_refresh_count": len(refreshes),
                "contact_snapshot_refreshes": refreshes[-10:],
                "contact_last_refresh_trigger": trigger_operation,
                "contact_last_re_resolved_refs": re_resolved_refs[:20],
            }
        )

    def record_contact_interaction(
        *,
        action: ContactFieldAction,
        interaction_type: str,
        option_ref: str | None = None,
        dropdown_snapshot_used: bool = False,
        combobox_opened: bool = False,
        option_text_detected: bool | None = None,
        option_ref_present: bool | None = None,
        keyboard_typeahead_attempted: bool = False,
        evaluate_selection_attempted: bool = False,
        interaction_strategy_used: str | None = None,
        combobox_selection_success: bool | None = None,
        completion_source: str | None = None,
        skipped_followup_actions: bool | None = None,
        evaluate_result_active_value: str | None = None,
        success_evidence_used: str | None = None,
        false_positive_prevented: bool | None = None,
        detected_field_type: str | None = None,
        select_value_attempted: str | None = None,
        select_value_normalized: str | None = None,
        select_success: bool | None = None,
    ) -> None:
        interactions = list(linkedin_contact_step_diagnostics.get("contact_field_interactions") or [])
        interactions.append(
            {
                "field_name": action.candidate.field_name,
                "field_ref": action.candidate.ref.ref,
                "field_label": action.candidate.label,
                "interaction_type": interaction_type,
                "option_ref": option_ref,
                "dropdown_snapshot_used": dropdown_snapshot_used,
                "combobox_opened": combobox_opened,
                "option_text_detected": option_text_detected,
                "option_ref_present": option_ref_present,
                "keyboard_typeahead_attempted": keyboard_typeahead_attempted,
                "evaluate_selection_attempted": evaluate_selection_attempted,
                "interaction_strategy_used": interaction_strategy_used or interaction_type,
                "combobox_selection_success": combobox_selection_success,
                "completion_source": completion_source,
                "skipped_followup_actions": skipped_followup_actions,
                "evaluate_result_active_value": evaluate_result_active_value,
                "success_evidence_used": success_evidence_used,
                "false_positive_prevented": false_positive_prevented,
                "detected_field_type": detected_field_type,
                "select_value_attempted": select_value_attempted,
                "select_value_normalized": select_value_normalized,
                "select_success": select_success,
            }
        )
        linkedin_contact_step_diagnostics.update(
            {
                "contact_field_interactions": interactions[-20:],
                "contact_last_interaction_type": interaction_type,
                "contact_last_option_ref": option_ref,
                "contact_last_dropdown_snapshot_used": dropdown_snapshot_used,
                "contact_last_combobox_opened": combobox_opened,
                "contact_last_option_text_detected": option_text_detected,
                "contact_last_option_ref_present": option_ref_present,
                "contact_last_keyboard_typeahead_attempted": keyboard_typeahead_attempted,
                "contact_last_evaluate_selection_attempted": evaluate_selection_attempted,
                "contact_last_interaction_strategy_used": interaction_strategy_used or interaction_type,
                "contact_last_combobox_selection_success": combobox_selection_success,
                "contact_last_completion_source": completion_source,
                "contact_last_skipped_followup_actions": skipped_followup_actions,
                "contact_last_evaluate_result_active_value": evaluate_result_active_value,
                "contact_last_success_evidence_used": success_evidence_used,
                "contact_last_false_positive_prevented": false_positive_prevented,
                "contact_last_detected_field_type": detected_field_type,
                "contact_last_select_value_attempted": select_value_attempted,
                "contact_last_select_value_normalized": select_value_normalized,
                "contact_last_select_success": select_success,
                "contact_any_false_positive_prevented": bool(
                    linkedin_contact_step_diagnostics.get("contact_any_false_positive_prevented")
                    or false_positive_prevented
                ),
            }
        )

    def analyze_form(
        current_snapshot_text: str,
        *,
        used_refs: set[str] | None = None,
    ) -> tuple[list[SnapshotRef], SnapshotRef | None, list[ContactFieldAction], dict[str, Any]]:
        current_refs = _parse_snapshot_refs(current_snapshot_text)
        current_upload_ref = _find_upload_ref(current_refs)
        current_actions = _plan_contact_field_actions(
            refs=current_refs,
            contact_values=contact_values,
            used_refs=used_refs,
        )
        current_dom_radio_groups = (
            _linkedin_radio_groups_from_dom(client, current_snapshot_text)
            if _is_linkedin_easy_apply_target(payload)
            else []
        )
        current_form_diagnostics = _form_diagnostics(
            snapshot_text=current_snapshot_text,
            refs=current_refs,
            upload_ref=current_upload_ref,
            field_actions=current_actions,
            radio_selection_attempts=radio_selection_attempts,
            dom_radio_groups=current_dom_radio_groups,
        )
        current_radio_group_diagnostics = list(current_form_diagnostics.get("radio_group_diagnostics") or [])
        radio_snapshot_context_present = any(
            token in current_snapshot_text.lower()
            for token in ("contact info", "primary phone number", "radio ", 'group "type', "fieldset", "legend")
        )
        if (
            (current_radio_group_diagnostics and radio_snapshot_context_present)
            or "radio_group_diagnostics" not in linkedin_contact_step_diagnostics
        ):
            linkedin_contact_step_diagnostics["radio_group_diagnostics"] = current_radio_group_diagnostics
        return current_refs, current_upload_ref, current_actions, current_form_diagnostics

    def preferred_radio_option(group_diagnostics: dict[str, Any]) -> str | None:
        field_name = _text(group_diagnostics.get("field_name"))
        options = [_text(option) for option in list(group_diagnostics.get("options") or []) if _text(option)]
        desired_value = _text(contact_values.get(field_name))
        if field_name == "phone_type":
            for option in options:
                if _normalize_label_text(option) == "mobile":
                    return option
        if desired_value:
            desired_normalized = _normalize_label_text(desired_value)
            for option in options:
                if _normalize_label_text(option) == desired_normalized:
                    return option
        return options[0] if options else None

    def attempt_linkedin_radio_group_selection(
        group_diagnostics: dict[str, Any],
        *,
        fallback_ref: str | None = None,
    ) -> tuple[bool, bool]:
        field_name = _text(group_diagnostics.get("field_name"))
        chosen_option = preferred_radio_option(group_diagnostics)
        if not field_name or not chosen_option:
            return False, False
        selection_attempt = {
            "selection_attempted": True,
            "attempted_option": chosen_option,
            "chosen_option": chosen_option,
            "attempted_ref": fallback_ref,
            "selection_verified": False,
        }
        dom_attempted = False
        dom_verified = False
        if _is_linkedin_easy_apply_target(payload):
            result = client.evaluate_json(_linkedin_radio_group_select_script(field_name, chosen_option))
            if isinstance(result, dict) and _text(result.get("probeKind")) == "__openclaw_linkedin_radio_group_select__":
                dom_attempted = _as_bool(result.get("selection_attempted"), default=False)
                dom_verified = _as_bool(result.get("selection_verified"), default=False)
                attempted_ref = ",".join([_text(ref) for ref in list(result.get("refs_involved") or []) if _text(ref)])
                selection_attempt.update(
                    {
                        "attempted_ref": attempted_ref or fallback_ref,
                        "chosen_option": _text(result.get("chosen_option")) or chosen_option,
                        "selection_verified": dom_verified,
                        "verified_option": _text(result.get("selected_option")) or None,
                    }
                )
        if not dom_attempted and fallback_ref:
            client.click(fallback_ref)
        radio_selection_attempts[field_name] = selection_attempt
        return (dom_attempted or bool(fallback_ref)), dom_verified

    def record_linkedin_action(
        *,
        state: str | None,
        action: str,
        reason: str,
        chosen_ref: str | None,
        chosen_label: str | None,
        upload_ref_value: str | None,
        advanced: bool,
    ) -> None:
        linkedin_progression.append(
            {
                "state": state,
                "action": action,
                "reason": reason,
                "chosen_ref": chosen_ref,
                "chosen_label": chosen_label,
                "chosen_upload_ref": upload_ref_value,
                "advanced_to_new_step": advanced,
            }
        )

    try:
        if runtime_config.attach_mode or runtime_config.skip_browser_start:
            try:
                client.status()
                client.tabs()
                attach_probe_succeeded = True
            except BrowserCommandError as exc:
                last_error = exc
                if runtime_config.allow_browser_start and not runtime_config.skip_browser_start:
                    warnings.append("attach_probe_failed_starting_browser")
                else:
                    raise
        if not attach_probe_succeeded and runtime_config.allow_browser_start and not runtime_config.skip_browser_start:
            start_attempted = True
            client.start()
            start_used = True
            client.status()
            client.tabs()
            attach_probe_succeeded = True
            last_error = None
        client.open(application_url)
        client.wait_for_load("domcontentloaded")
        try:
            client.wait_for_load("networkidle")
        except BrowserCommandError:
            warnings.append("networkidle_wait_skipped")

        page_title, current_url = _browser_metadata(client)
        checkpoint_urls = _dedupe([application_url, current_url])
        snapshot_text = client.snapshot()
        refs, upload_ref, contact_field_actions, form_diagnostics = analyze_form(snapshot_text)
        if capture_screenshots:
            _capture_screenshot(
                client,
                screenshot_dir=screenshot_dir,
                checkpoint_name="landing",
                page_url=current_url,
                screenshots=screenshots,
                warnings=warnings,
                screenshot_failures=screenshot_failures,
                max_screenshots=max_screenshots,
            )

        page_diagnostics = _page_diagnostics(
            application_url=application_url,
            current_url=current_url,
            page_title=page_title,
            snapshot_text=snapshot_text,
            refs=refs,
            upload_ref=upload_ref,
        )
        linkedin_context = _linkedin_step_context(
            snapshot_text=snapshot_text,
            refs=refs,
            upload_ref=upload_ref,
            contact_field_actions=contact_field_actions,
            page_diagnostics=page_diagnostics,
        )
        page_diagnostics.update(linkedin_context)
        page_diagnostics["linkedin_state"] = linkedin_context["state"]
        merge_linkedin_step_diagnostics(page_diagnostics)

        if page_diagnostics["captcha_indicators_detected"]:
            return _result(
                draft_status="not_started",
                source_status="captcha_or_bot_challenge",
                awaiting_review=False,
                review_status="blocked",
                failure_category="captcha_or_bot_challenge",
                blocking_reason="The page presented a captcha or bot challenge that should be handled manually.",
                screenshot_metadata_references=screenshots,
                checkpoint_urls=checkpoint_urls,
                page_title=page_title,
                warnings=warnings,
                errors=errors,
                page_diagnostics=page_diagnostics,
                form_diagnostics=form_diagnostics,
                debug_json=build_debug_json(),
            )
        if page_diagnostics["anti_bot_indicators_detected"] and not page_diagnostics["login_or_checkpoint_markers_present"]:
            return _result(
                draft_status="not_started",
                source_status="anti_bot_blocked",
                awaiting_review=False,
                review_status="blocked",
                failure_category="anti_bot_blocked",
                blocking_reason="The page blocked automation with anti-bot defenses and should be handled manually.",
                screenshot_metadata_references=screenshots,
                checkpoint_urls=checkpoint_urls,
                page_title=page_title,
                warnings=warnings,
                errors=errors,
                page_diagnostics=page_diagnostics,
                form_diagnostics=form_diagnostics,
                debug_json=build_debug_json(),
            )
        if _host(current_url) and _host(current_url) != _host(application_url):
            auth_failure_category = "session_expired" if _as_bool(auth.get("session_available")) else "login_required"
            if page_diagnostics["login_or_checkpoint_markers_present"]:
                return _result(
                    draft_status="not_started",
                    source_status=auth_failure_category,
                    awaiting_review=False,
                    review_status="blocked",
                    failure_category=auth_failure_category,
                    blocking_reason=(
                        "The existing browser session expired before the draft could be prepared."
                        if auth_failure_category == "session_expired"
                        else "The application flow requires a logged-in session that is not currently available."
                    ),
                    screenshot_metadata_references=screenshots,
                    checkpoint_urls=checkpoint_urls,
                    page_title=page_title,
                    warnings=warnings,
                    errors=errors,
                    page_diagnostics=page_diagnostics,
                    form_diagnostics=form_diagnostics,
                    debug_json=build_debug_json(),
                )
            return _result(
                draft_status="not_started",
                source_status="redirected_off_target",
                awaiting_review=False,
                review_status="blocked",
                failure_category="redirected_off_target",
                blocking_reason="The browser was redirected away from the intended application target.",
                screenshot_metadata_references=screenshots,
                checkpoint_urls=checkpoint_urls,
                page_title=page_title,
                warnings=warnings,
                errors=errors,
                page_diagnostics=page_diagnostics,
                form_diagnostics=form_diagnostics,
                debug_json=build_debug_json(),
            )
        if page_diagnostics["target_host"].endswith("linkedin.com"):
            for _ in range(4):
                linkedin_state = str(page_diagnostics.get("linkedin_state") or "").strip()
                if linkedin_state == "job_page_easy_apply_visible" and page_diagnostics.get("easy_apply_ref"):
                    chosen_ref = str(page_diagnostics.get("easy_apply_ref"))
                    chosen_label = _text(page_diagnostics.get("easy_apply_ref_label"))
                    previous_state = linkedin_state
                    previous_url = current_url
                    previous_excerpt = snapshot_text[:DEFAULT_MAX_SNAPSHOT_CHARS]
                    client.click(chosen_ref)
                    page_title, current_url = _browser_metadata(client)
                    checkpoint_urls = _dedupe([application_url, current_url])
                    snapshot_text = client.snapshot()
                    refs, upload_ref, contact_field_actions, form_diagnostics = analyze_form(snapshot_text)
                    page_diagnostics = _page_diagnostics(
                        application_url=application_url,
                        current_url=current_url,
                        page_title=page_title,
                        snapshot_text=snapshot_text,
                        refs=refs,
                        upload_ref=upload_ref,
                    )
                    linkedin_context = _linkedin_step_context(
                        snapshot_text=snapshot_text,
                        refs=refs,
                        upload_ref=upload_ref,
                        contact_field_actions=contact_field_actions,
                        page_diagnostics=page_diagnostics,
                    )
                    page_diagnostics.update(linkedin_context)
                    page_diagnostics["linkedin_state"] = linkedin_context["state"]
                    merge_linkedin_step_diagnostics(page_diagnostics)
                    advanced = bool(
                        current_url != previous_url
                        or page_diagnostics.get("linkedin_state") != previous_state
                        or snapshot_text[:DEFAULT_MAX_SNAPSHOT_CHARS] != previous_excerpt
                    )
                    record_linkedin_action(
                        state=previous_state,
                        action="click_easy_apply",
                        reason="easy_apply_trigger_visible_on_job_page",
                        chosen_ref=chosen_ref,
                        chosen_label=chosen_label,
                        upload_ref_value=_text(page_diagnostics.get("upload_ref")),
                        advanced=advanced,
                    )
                    if not advanced:
                        warnings.append("linkedin_easy_apply_click_did_not_advance")
                        break
                    continue

                if linkedin_state == "easy_apply_contact_info_step":
                    initial_fill_payloads, initial_select_actions, initial_manifest_rows = _contact_fill_work(contact_field_actions)
                    append_contact_manifest_rows(
                        [
                            row
                            for row in initial_manifest_rows
                            if _text(row.get("status")) == "prefilled_verified"
                        ]
                    )
                    contact_execution_attempted = bool(initial_fill_payloads or initial_select_actions)
                    completed_contact_field_names: set[str] = set()
                    attempted_contact_signatures: set[tuple[str, str, str, str]] = set()
                    while page_diagnostics.get("linkedin_state") == "easy_apply_contact_info_step":
                        next_contact_action = next(
                            (
                                action
                                for action in contact_field_actions
                                if action.action == "fill" and action.candidate.field_name not in completed_contact_field_names
                            ),
                            None,
                        )
                        if next_contact_action is None:
                            break
                        action_ref = next_contact_action.candidate.ref.ref
                        action_type = next_contact_action.candidate.field_type
                        action_signature = (
                            next_contact_action.candidate.field_name,
                            action_ref,
                            action_type,
                            _text(next_contact_action.value),
                        )
                        if action_signature in attempted_contact_signatures:
                            warnings.append("linkedin_contact_step_repeated_action_without_ref_change")
                            break
                        attempted_contact_signatures.add(action_signature)
                        executed_manifest_rows = _contact_fill_work([next_contact_action])[2]
                        action_completed = False
                        select_probe_result: Any = None
                        detected_field_type = "select" if action_type == "select" else action_type
                        if (
                            action_type == "select"
                            and _is_linkedin_easy_apply_target(payload)
                            and next_contact_action.candidate.field_name == "country"
                        ):
                            select_probe_result = client.evaluate_json(
                                _native_select_probe_script(
                                    next_contact_action.candidate.label,
                                    str(next_contact_action.value),
                                )
                            )
                            detected_field_type = _detected_select_field_type(select_probe_result)
                        if (
                            action_type == "select"
                            and _is_linkedin_easy_apply_target(payload)
                            and next_contact_action.candidate.field_name == "country"
                            and detected_field_type != "select"
                        ):
                            client.click(action_ref)
                            page_title, current_url = _browser_metadata(client)
                            checkpoint_urls = _dedupe([application_url, current_url])
                            snapshot_text = client.snapshot()
                            refs, upload_ref, contact_field_actions, form_diagnostics = analyze_form(snapshot_text)
                            record_contact_refresh(
                                trigger_operation="click_select_open",
                                executed_ref=action_ref,
                                contact_actions=contact_field_actions,
                            )
                            option_text_detected = _snapshot_contains_option_text(snapshot_text, str(next_contact_action.value))
                            option_ref = _find_dropdown_option_ref(
                                refs,
                                desired_value=str(next_contact_action.value),
                                opener_ref=action_ref,
                                opener_label=next_contact_action.candidate.label,
                            )
                            record_contact_interaction(
                                action=next_contact_action,
                                interaction_type="combobox_open",
                                option_ref=option_ref.ref if option_ref else None,
                                dropdown_snapshot_used=True,
                                combobox_opened=True,
                                option_text_detected=option_text_detected,
                                option_ref_present=option_ref is not None,
                                keyboard_typeahead_attempted=False,
                                evaluate_selection_attempted=False,
                                interaction_strategy_used="combobox_open",
                                detected_field_type=detected_field_type,
                                select_value_attempted=_text(next_contact_action.value),
                                select_value_normalized=None,
                                select_success=False,
                            )
                            keyboard_attempted = False
                            evaluate_attempted = False
                            combobox_selection_success = False

                            keyboard_attempted = True
                            keyboard_result = client.evaluate_json(
                                _combobox_keyboard_typeahead_script(str(next_contact_action.value))
                            )
                            page_title, current_url = _browser_metadata(client)
                            checkpoint_urls = _dedupe([application_url, current_url])
                            snapshot_text = client.snapshot()
                            refs, upload_ref, contact_field_actions, form_diagnostics = analyze_form(snapshot_text)
                            record_contact_refresh(
                                trigger_operation="keyboard_typeahead",
                                executed_ref=action_ref,
                                contact_actions=contact_field_actions,
                            )
                            keyboard_selection = _combobox_selection_diagnostics(
                                evaluate_result=keyboard_result,
                                snapshot_text=snapshot_text,
                                refs=refs,
                                actions=contact_field_actions,
                                field_name=next_contact_action.candidate.field_name,
                                desired_value=str(next_contact_action.value),
                            )
                            linkedin_contact_step_diagnostics.update(
                                {
                                    "contact_last_evaluate_result_active_value": keyboard_selection[
                                        "evaluate_result_active_value"
                                    ],
                                    "contact_last_success_evidence_used": keyboard_selection["success_evidence_used"],
                                    "contact_last_false_positive_prevented": keyboard_selection["false_positive_prevented"],
                                    "contact_any_false_positive_prevented": bool(
                                        linkedin_contact_step_diagnostics.get("contact_any_false_positive_prevented")
                                        or keyboard_selection["false_positive_prevented"]
                                    ),
                                    "contact_false_positive_event": (
                                        {
                                            "strategy": "keyboard_typeahead",
                                            "evaluate_result_active_value": keyboard_selection[
                                                "evaluate_result_active_value"
                                            ],
                                            "success_evidence_used": keyboard_selection["success_evidence_used"],
                                            "false_positive_prevented": keyboard_selection["false_positive_prevented"],
                                        }
                                        if keyboard_selection["false_positive_prevented"]
                                        else linkedin_contact_step_diagnostics.get("contact_false_positive_event")
                                    ),
                                }
                            )
                            if keyboard_selection["success"]:
                                combobox_selection_success = True
                                record_contact_interaction(
                                    action=next_contact_action,
                                    interaction_type="click_select",
                                    option_ref=None,
                                    dropdown_snapshot_used=True,
                                    combobox_opened=True,
                                    option_text_detected=option_text_detected,
                                    option_ref_present=option_ref is not None,
                                    keyboard_typeahead_attempted=keyboard_attempted,
                                    evaluate_selection_attempted=False,
                                    interaction_strategy_used="keyboard_typeahead",
                                    combobox_selection_success=True,
                                    completion_source="keyboard",
                                    skipped_followup_actions=True,
                                    evaluate_result_active_value=keyboard_selection["evaluate_result_active_value"],
                                    success_evidence_used=keyboard_selection["success_evidence_used"],
                                    false_positive_prevented=keyboard_selection["false_positive_prevented"],
                                    detected_field_type=detected_field_type,
                                    select_value_attempted=_text(next_contact_action.value),
                                    select_value_normalized=None,
                                    select_success=False,
                                )
                                refresh_trigger = "keyboard_typeahead"
                                action_ref_for_refresh = action_ref

                            if not combobox_selection_success:
                                current_country_action = next(
                                    (
                                        action
                                        for action in contact_field_actions
                                        if action.candidate.field_name == next_contact_action.candidate.field_name
                                    ),
                                    next_contact_action,
                                )
                                current_country_ref = current_country_action.candidate.ref.ref
                                client.fill([{"ref": current_country_ref, "value": str(next_contact_action.value), "type": "text"}])
                                page_title, current_url = _browser_metadata(client)
                                checkpoint_urls = _dedupe([application_url, current_url])
                                snapshot_text = client.snapshot()
                                refs, upload_ref, contact_field_actions, form_diagnostics = analyze_form(snapshot_text)
                                record_contact_refresh(
                                    trigger_operation="combobox_fill_type",
                                    executed_ref=current_country_ref,
                                    contact_actions=contact_field_actions,
                                )
                                fill_selection = _combobox_selection_diagnostics(
                                    evaluate_result=None,
                                    snapshot_text=snapshot_text,
                                    refs=refs,
                                    actions=contact_field_actions,
                                    field_name=next_contact_action.candidate.field_name,
                                    desired_value=str(next_contact_action.value),
                                )
                                linkedin_contact_step_diagnostics.update(
                                    {
                                        "contact_last_evaluate_result_active_value": fill_selection[
                                            "evaluate_result_active_value"
                                        ],
                                        "contact_last_success_evidence_used": fill_selection["success_evidence_used"],
                                        "contact_last_false_positive_prevented": fill_selection["false_positive_prevented"],
                                        "contact_any_false_positive_prevented": bool(
                                            linkedin_contact_step_diagnostics.get("contact_any_false_positive_prevented")
                                            or fill_selection["false_positive_prevented"]
                                        ),
                                        "contact_false_positive_event": (
                                            {
                                                "strategy": "combobox_fill_type",
                                                "evaluate_result_active_value": fill_selection[
                                                    "evaluate_result_active_value"
                                                ],
                                                "success_evidence_used": fill_selection["success_evidence_used"],
                                                "false_positive_prevented": fill_selection["false_positive_prevented"],
                                            }
                                            if fill_selection["false_positive_prevented"]
                                            else linkedin_contact_step_diagnostics.get("contact_false_positive_event")
                                        ),
                                    }
                                )
                                if fill_selection["success"]:
                                    combobox_selection_success = True
                                    record_contact_interaction(
                                        action=next_contact_action,
                                        interaction_type="click_select",
                                        option_ref=None,
                                        dropdown_snapshot_used=True,
                                        combobox_opened=True,
                                        option_text_detected=option_text_detected,
                                        option_ref_present=option_ref is not None,
                                        keyboard_typeahead_attempted=keyboard_attempted,
                                        evaluate_selection_attempted=False,
                                        interaction_strategy_used="combobox_fill_type",
                                        combobox_selection_success=True,
                                        completion_source="select",
                                        skipped_followup_actions=True,
                                        evaluate_result_active_value=fill_selection["evaluate_result_active_value"],
                                        success_evidence_used=fill_selection["success_evidence_used"],
                                        false_positive_prevented=fill_selection["false_positive_prevented"],
                                        detected_field_type=detected_field_type,
                                        select_value_attempted=_text(next_contact_action.value),
                                        select_value_normalized=None,
                                        select_success=False,
                                    )
                                    refresh_trigger = "combobox_fill_type"
                                    action_ref_for_refresh = current_country_ref

                            if not combobox_selection_success:
                                evaluate_attempted = True
                                current_country_action = next(
                                    (
                                        action
                                        for action in contact_field_actions
                                        if action.candidate.field_name == next_contact_action.candidate.field_name
                                    ),
                                    next_contact_action,
                                )
                                current_country_ref = current_country_action.candidate.ref.ref
                                evaluate_result = client.evaluate_json(
                                    _combobox_evaluate_selection_script(str(next_contact_action.value))
                                )
                                page_title, current_url = _browser_metadata(client)
                                checkpoint_urls = _dedupe([application_url, current_url])
                                snapshot_text = client.snapshot()
                                refs, upload_ref, contact_field_actions, form_diagnostics = analyze_form(snapshot_text)
                                record_contact_refresh(
                                    trigger_operation="evaluate_selection",
                                    executed_ref=current_country_ref,
                                    contact_actions=contact_field_actions,
                                )
                                evaluate_selection = _combobox_selection_diagnostics(
                                    evaluate_result=evaluate_result,
                                    snapshot_text=snapshot_text,
                                    refs=refs,
                                    actions=contact_field_actions,
                                    field_name=next_contact_action.candidate.field_name,
                                    desired_value=str(next_contact_action.value),
                                )
                                linkedin_contact_step_diagnostics.update(
                                    {
                                        "contact_last_evaluate_result_active_value": evaluate_selection[
                                            "evaluate_result_active_value"
                                        ],
                                        "contact_last_success_evidence_used": evaluate_selection["success_evidence_used"],
                                        "contact_last_false_positive_prevented": evaluate_selection["false_positive_prevented"],
                                        "contact_any_false_positive_prevented": bool(
                                            linkedin_contact_step_diagnostics.get("contact_any_false_positive_prevented")
                                            or evaluate_selection["false_positive_prevented"]
                                        ),
                                        "contact_false_positive_event": (
                                            {
                                                "strategy": "evaluate_selection",
                                                "evaluate_result_active_value": evaluate_selection[
                                                    "evaluate_result_active_value"
                                                ],
                                                "success_evidence_used": evaluate_selection["success_evidence_used"],
                                                "false_positive_prevented": evaluate_selection["false_positive_prevented"],
                                            }
                                            if evaluate_selection["false_positive_prevented"]
                                            else linkedin_contact_step_diagnostics.get("contact_false_positive_event")
                                        ),
                                    }
                                )
                                if evaluate_selection["success"]:
                                    combobox_selection_success = True
                                    record_contact_interaction(
                                        action=next_contact_action,
                                        interaction_type="click_select",
                                        option_ref=None,
                                        dropdown_snapshot_used=True,
                                        combobox_opened=True,
                                        option_text_detected=option_text_detected,
                                        option_ref_present=option_ref is not None,
                                        keyboard_typeahead_attempted=keyboard_attempted,
                                        evaluate_selection_attempted=evaluate_attempted,
                                        interaction_strategy_used="evaluate_selection",
                                        combobox_selection_success=True,
                                        completion_source="evaluate",
                                        skipped_followup_actions=True,
                                        evaluate_result_active_value=evaluate_selection["evaluate_result_active_value"],
                                        success_evidence_used=evaluate_selection["success_evidence_used"],
                                        false_positive_prevented=evaluate_selection["false_positive_prevented"],
                                        detected_field_type=detected_field_type,
                                        select_value_attempted=_text(next_contact_action.value),
                                        select_value_normalized=None,
                                        select_success=False,
                                    )
                                    refresh_trigger = "evaluate_selection"
                                    action_ref_for_refresh = current_country_ref

                            if not combobox_selection_success and option_ref is not None:
                                current_country_action = next(
                                    (
                                        action
                                        for action in contact_field_actions
                                        if action.candidate.field_name == next_contact_action.candidate.field_name
                                    ),
                                    next_contact_action,
                                )
                                current_country_ref = current_country_action.candidate.ref.ref
                                client.click(current_country_ref)
                                page_title, current_url = _browser_metadata(client)
                                checkpoint_urls = _dedupe([application_url, current_url])
                                snapshot_text = client.snapshot()
                                refs, upload_ref, contact_field_actions, form_diagnostics = analyze_form(snapshot_text)
                                record_contact_refresh(
                                    trigger_operation="click_select_open_option_ref",
                                    executed_ref=current_country_ref,
                                    contact_actions=contact_field_actions,
                                )
                                option_ref = _find_dropdown_option_ref(
                                    refs,
                                    desired_value=str(next_contact_action.value),
                                    opener_ref=current_country_ref,
                                    opener_label=current_country_action.candidate.label,
                                )
                                if option_ref is None:
                                    raise BrowserCommandError(
                                        failure_category="navigation_failed",
                                        blocking_reason=(
                                            f'LinkedIn dropdown option "{str(next_contact_action.value)}" text was detected, '
                                            "but no clickable option ref was available after reopening the combobox."
                                        ),
                                        errors=["linkedin_dropdown_option_ref_missing_after_reopen"],
                                        stage="linkedin_contact_click_select",
                                        error_kind="dropdown_option_missing",
                                    )
                                client.click(option_ref.ref)
                                combobox_selection_success = True
                                record_contact_interaction(
                                    action=next_contact_action,
                                    interaction_type="click_select",
                                    option_ref=option_ref.ref,
                                    dropdown_snapshot_used=True,
                                    combobox_opened=True,
                                    option_text_detected=option_text_detected,
                                    option_ref_present=True,
                                    keyboard_typeahead_attempted=keyboard_attempted,
                                    evaluate_selection_attempted=evaluate_attempted,
                                    interaction_strategy_used="option_ref_click",
                                    combobox_selection_success=True,
                                    completion_source="select",
                                    skipped_followup_actions=False,
                                    detected_field_type=detected_field_type,
                                    select_value_attempted=_text(next_contact_action.value),
                                    select_value_normalized=None,
                                    select_success=False,
                                )
                                refresh_trigger = "option_ref_click"
                                action_ref_for_refresh = option_ref.ref

                            if not combobox_selection_success:
                                raise BrowserCommandError(
                                    failure_category="navigation_failed",
                                    blocking_reason=(
                                        f'LinkedIn dropdown option "{str(next_contact_action.value)}" was visible in the snapshot, '
                                        "but it could not be selected via typeahead, fill, or evaluate fallback."
                                    ),
                                    errors=["linkedin_dropdown_selection_strategies_exhausted"],
                                    stage="linkedin_contact_click_select",
                                    error_kind="dropdown_selection_failed",
                                )
                            action_completed = True
                        elif action_type == "select":
                            normalized_select_value = (
                                _normalize_select_attempt_value(str(next_contact_action.value), select_probe_result)
                                if (
                                    _is_linkedin_easy_apply_target(payload)
                                    and next_contact_action.candidate.field_name == "country"
                                )
                                else str(next_contact_action.value)
                            )
                            try:
                                client.select(action_ref, normalized_select_value)
                                record_contact_interaction(
                                    action=next_contact_action,
                                    interaction_type="select",
                                    option_ref=None,
                                    dropdown_snapshot_used=False,
                                    detected_field_type=detected_field_type,
                                    select_value_attempted=_text(next_contact_action.value),
                                    select_value_normalized=normalized_select_value,
                                    select_success=True,
                                )
                                refresh_trigger = "select"
                                action_ref_for_refresh = action_ref
                                action_completed = True
                            except BrowserCommandError as exc:
                                record_contact_interaction(
                                    action=next_contact_action,
                                    interaction_type="select",
                                    option_ref=None,
                                    dropdown_snapshot_used=False,
                                    detected_field_type=detected_field_type,
                                    select_value_attempted=_text(next_contact_action.value),
                                    select_value_normalized=normalized_select_value,
                                    select_success=False,
                                )
                                raise exc
                        elif action_type == "radio":
                            current_radio_group = next(
                                (
                                    row
                                    for row in list(form_diagnostics.get("radio_group_diagnostics") or [])
                                    if _text(row.get("field_name")) == next_contact_action.candidate.field_name
                                ),
                                {
                                    "field_name": next_contact_action.candidate.field_name,
                                    "options": [next_contact_action.candidate.label],
                                    "group_label": _find_radio_group_label(snapshot_text, next_contact_action.candidate),
                                },
                            )
                            attempt_linkedin_radio_group_selection(current_radio_group, fallback_ref=action_ref)
                            record_contact_interaction(
                                action=next_contact_action,
                                interaction_type="click_radio",
                                option_ref=None,
                                dropdown_snapshot_used=False,
                                detected_field_type=detected_field_type,
                            )
                            refresh_trigger = "click_radio"
                            action_ref_for_refresh = action_ref
                        else:
                            client.fill([{"ref": action_ref, "value": next_contact_action.value, "type": action_type}])
                            record_contact_interaction(
                                action=next_contact_action,
                                interaction_type="fill",
                                option_ref=None,
                                dropdown_snapshot_used=False,
                            )
                            refresh_trigger = "fill"
                            action_ref_for_refresh = action_ref
                            action_completed = True
                        page_title, current_url = _browser_metadata(client)
                        checkpoint_urls = _dedupe([application_url, current_url])
                        snapshot_text = client.snapshot()
                        refs, upload_ref, contact_field_actions, form_diagnostics = analyze_form(snapshot_text)
                        record_contact_refresh(
                            trigger_operation=refresh_trigger,
                            executed_ref=action_ref_for_refresh,
                            contact_actions=contact_field_actions,
                        )
                        page_diagnostics = _page_diagnostics(
                            application_url=application_url,
                            current_url=current_url,
                            page_title=page_title,
                            snapshot_text=snapshot_text,
                            refs=refs,
                            upload_ref=upload_ref,
                        )
                        linkedin_context = _linkedin_step_context(
                            snapshot_text=snapshot_text,
                            refs=refs,
                            upload_ref=upload_ref,
                            contact_field_actions=contact_field_actions,
                            page_diagnostics=page_diagnostics,
                        )
                        page_diagnostics.update(linkedin_context)
                        page_diagnostics["linkedin_state"] = linkedin_context["state"]
                        merge_linkedin_step_diagnostics(page_diagnostics)
                        if action_type == "radio":
                            radio_group_diagnostics = list(form_diagnostics.get("radio_group_diagnostics") or [])
                            verified_radio_group = next(
                                (
                                    row
                                    for row in radio_group_diagnostics
                                    if _text(row.get("field_name")) == next_contact_action.candidate.field_name
                                ),
                                {},
                            )
                            radio_verified = bool(verified_radio_group.get("selection_verified"))
                            if radio_verified:
                                radio_selection_attempts[next_contact_action.candidate.field_name]["selection_verified"] = True
                                radio_selection_attempts[next_contact_action.candidate.field_name]["verified_option"] = _text(
                                    verified_radio_group.get("verified_option")
                                )
                                action_completed = True
                            else:
                                radio_selection_attempts[next_contact_action.candidate.field_name]["selection_verified"] = False
                                warnings.append("linkedin_contact_radio_selection_not_verified")
                        if action_completed:
                            completed_contact_field_names.add(next_contact_action.candidate.field_name)
                            append_contact_manifest_rows(executed_manifest_rows)
                    record_linkedin_action(
                        state=linkedin_state,
                        action="fill_contact_info",
                        reason="safe_contact_fields_detected_on_contact_step",
                        chosen_ref=None,
                        chosen_label=None,
                        upload_ref_value=_text(page_diagnostics.get("upload_ref")),
                        advanced=False,
                    )
                    if not contact_execution_attempted:
                        merge_linkedin_step_diagnostics(page_diagnostics)
                    if page_diagnostics.get("linkedin_state") == "easy_apply_contact_info_step":
                        pending_radio_group = next(
                            (
                                row
                                for row in list(form_diagnostics.get("radio_group_diagnostics") or [])
                                if bool(row.get("required"))
                                and not bool(row.get("selection_verified"))
                                and _text(row.get("field_name")) == "phone_type"
                            ),
                            None,
                        )
                        if pending_radio_group is not None:
                            attempted, verified = attempt_linkedin_radio_group_selection(pending_radio_group)
                            if attempted:
                                page_title, current_url = _browser_metadata(client)
                                checkpoint_urls = _dedupe([application_url, current_url])
                                snapshot_text = client.snapshot()
                                refs, upload_ref, contact_field_actions, form_diagnostics = analyze_form(snapshot_text)
                                record_contact_refresh(
                                    trigger_operation="select_radio_group_dom",
                                    executed_ref=_text(
                                        radio_selection_attempts.get("phone_type", {}).get("attempted_ref")
                                        or pending_radio_group.get("chosen_option")
                                    ),
                                    contact_actions=contact_field_actions,
                                )
                                page_diagnostics = _page_diagnostics(
                                    application_url=application_url,
                                    current_url=current_url,
                                    page_title=page_title,
                                    snapshot_text=snapshot_text,
                                    refs=refs,
                                    upload_ref=upload_ref,
                                )
                                linkedin_context = _linkedin_step_context(
                                    snapshot_text=snapshot_text,
                                    refs=refs,
                                    upload_ref=upload_ref,
                                    contact_field_actions=contact_field_actions,
                                    page_diagnostics=page_diagnostics,
                                )
                                page_diagnostics.update(linkedin_context)
                                page_diagnostics["linkedin_state"] = linkedin_context["state"]
                                merge_linkedin_step_diagnostics(page_diagnostics)
                                if verified:
                                    append_contact_manifest_rows(
                                        [
                                            {
                                                "field_name": "phone_type",
                                                "label": _text(
                                                    radio_selection_attempts.get("phone_type", {}).get("verified_option")
                                                    or pending_radio_group.get("chosen_option")
                                                ),
                                                "field_type": "radio",
                                                "status": "checked",
                                                "value_preview": None,
                                                "value_redacted": True,
                                            }
                                        ]
                                    )
                    if page_diagnostics.get("linkedin_state") == "easy_apply_contact_info_step":
                        progression = _contact_step_progression_diagnostics_with_radios(
                            contact_field_actions,
                            radio_group_diagnostics=list(form_diagnostics.get("radio_group_diagnostics") or []),
                        )
                        form_diagnostics = {
                            **form_diagnostics,
                            "blocking_skipped_fields": progression["blocking_skipped_fields"],
                            "nonblocking_skipped_fields": progression["nonblocking_skipped_fields"],
                            "required_field_statuses": progression["required_field_statuses"],
                            "radio_group_diagnostics": progression["radio_group_diagnostics"],
                            "next_click_gate_reason": progression["next_click_gate_reason"],
                        }
                        next_not_clicked_reason = None
                        if not page_diagnostics.get("next_button_ref"):
                            next_not_clicked_reason = "next_button_not_detected"
                        elif not progression["can_advance"]:
                            next_not_clicked_reason = progression["next_click_gate_reason"]
                        update_contact_step_diagnostics(
                            page_diagnostics,
                            form_diagnostics,
                            next_clicked=False,
                            next_not_clicked_reason=next_not_clicked_reason,
                        )
                    if (
                        page_diagnostics.get("linkedin_state") == "easy_apply_contact_info_step"
                        and page_diagnostics.get("next_button_ref")
                        and _contact_step_can_advance(
                            contact_field_actions,
                            radio_group_diagnostics=list(form_diagnostics.get("radio_group_diagnostics") or []),
                        )
                    ):
                        chosen_ref = str(page_diagnostics.get("next_button_ref"))
                        chosen_label = _text(page_diagnostics.get("next_button_label"))
                        previous_state = str(page_diagnostics.get("linkedin_state") or "")
                        previous_url = current_url
                        previous_excerpt = snapshot_text[:DEFAULT_MAX_SNAPSHOT_CHARS]
                        client.click(chosen_ref)
                        update_contact_step_diagnostics(
                            page_diagnostics,
                            form_diagnostics,
                            next_clicked=True,
                            next_not_clicked_reason=None,
                        )
                        page_title, current_url = _browser_metadata(client)
                        checkpoint_urls = _dedupe([application_url, current_url])
                        snapshot_text = client.snapshot()
                        refs, upload_ref, contact_field_actions, form_diagnostics = analyze_form(snapshot_text)
                        page_diagnostics = _page_diagnostics(
                            application_url=application_url,
                            current_url=current_url,
                            page_title=page_title,
                            snapshot_text=snapshot_text,
                            refs=refs,
                            upload_ref=upload_ref,
                        )
                        linkedin_context = _linkedin_step_context(
                            snapshot_text=snapshot_text,
                            refs=refs,
                            upload_ref=upload_ref,
                            contact_field_actions=contact_field_actions,
                            page_diagnostics=page_diagnostics,
                        )
                        page_diagnostics.update(linkedin_context)
                        page_diagnostics["linkedin_state"] = linkedin_context["state"]
                        merge_linkedin_step_diagnostics(page_diagnostics)
                        advanced = bool(
                            current_url != previous_url
                            or page_diagnostics.get("linkedin_state") != previous_state
                            or snapshot_text[:DEFAULT_MAX_SNAPSHOT_CHARS] != previous_excerpt
                        )
                        record_linkedin_action(
                            state=previous_state,
                            action="click_next",
                            reason="contact_step_complete_and_next_visible",
                            chosen_ref=chosen_ref,
                            chosen_label=chosen_label,
                            upload_ref_value=_text(page_diagnostics.get("upload_ref")),
                            advanced=advanced,
                        )
                        if not advanced:
                            warnings.append("linkedin_next_click_did_not_advance")
                            break
                        continue
                    break

                if linkedin_state == "easy_apply_resume_upload_step":
                    update_resume_step_diagnostics(page_diagnostics)
                    if page_diagnostics.get("selected_resume_verified") and page_diagnostics.get("continue_button_ref"):
                        chosen_ref = str(page_diagnostics.get("continue_button_ref"))
                        chosen_label = _text(page_diagnostics.get("continue_button_label"))
                        previous_state = linkedin_state
                        previous_url = current_url
                        previous_excerpt = snapshot_text[:DEFAULT_MAX_SNAPSHOT_CHARS]
                        client.click(chosen_ref)
                        page_title, current_url = _browser_metadata(client)
                        checkpoint_urls = _dedupe([application_url, current_url])
                        snapshot_text = client.snapshot()
                        refs, upload_ref, contact_field_actions, form_diagnostics = analyze_form(snapshot_text)
                        page_diagnostics = _page_diagnostics(
                            application_url=application_url,
                            current_url=current_url,
                            page_title=page_title,
                            snapshot_text=snapshot_text,
                            refs=refs,
                            upload_ref=upload_ref,
                        )
                        linkedin_context = _linkedin_step_context(
                            snapshot_text=snapshot_text,
                            refs=refs,
                            upload_ref=upload_ref,
                            contact_field_actions=contact_field_actions,
                            page_diagnostics=page_diagnostics,
                        )
                        page_diagnostics.update(linkedin_context)
                        page_diagnostics["linkedin_state"] = linkedin_context["state"]
                        merge_linkedin_step_diagnostics(page_diagnostics)
                        update_resume_step_diagnostics(
                            page_diagnostics,
                            continue_button_ref=chosen_ref,
                            continue_button_label=chosen_label,
                        )
                        advanced = bool(
                            current_url != previous_url
                            or page_diagnostics.get("linkedin_state") != previous_state
                            or snapshot_text[:DEFAULT_MAX_SNAPSHOT_CHARS] != previous_excerpt
                        )
                        update_resume_step_diagnostics(
                            page_diagnostics,
                            continue_button_ref=chosen_ref,
                            continue_button_label=chosen_label,
                            continue_clicked=True,
                            continue_verified=advanced,
                        )
                        record_linkedin_action(
                            state=previous_state,
                            action="click_next",
                            reason="resume_already_selected_and_continue_visible",
                            chosen_ref=chosen_ref,
                            chosen_label=chosen_label,
                            upload_ref_value=_text(page_diagnostics.get("upload_ref")),
                            advanced=advanced,
                        )
                        if not advanced:
                            warnings.append("linkedin_resume_continue_click_did_not_advance")
                            break
                        continue
                    break

                if linkedin_state in {"easy_apply_later_step", "easy_apply_review_step"}:
                    record_linkedin_action(
                        state=linkedin_state,
                        action="handoff_to_generic_step_handler",
                        reason="later_step_or_review_step_detected_for_generic_field_handling",
                        chosen_ref=None,
                        chosen_label=None,
                        upload_ref_value=_text(page_diagnostics.get("upload_ref")),
                        advanced=False,
                    )
                    break

                break
        if page_diagnostics["apply_modal_not_mounted"]:
            return _result(
                draft_status="not_started",
                source_status="manual_review_required",
                awaiting_review=False,
                review_status="blocked",
                failure_category="manual_review_required",
                blocking_reason="LinkedIn opened the job page, but the Easy Apply dialog did not mount.",
                screenshot_metadata_references=screenshots,
                checkpoint_urls=checkpoint_urls,
                page_title=page_title,
                warnings=warnings,
                errors=errors + ["easy_apply_modal_not_mounted"],
                page_diagnostics=page_diagnostics,
                form_diagnostics=form_diagnostics,
                debug_json=build_debug_json(),
            )
        if (
            page_diagnostics.get("linkedin_state") == "easy_apply_resume_upload_step"
            and not page_diagnostics.get("selected_resume_verified")
            and not page_diagnostics.get("upload_input_exists")
            and page_diagnostics.get("upload_button_ref")
        ):
            return _result(
                draft_status="not_started",
                source_status="unsupported_form",
                awaiting_review=False,
                review_status="blocked",
                failure_category="unsupported_form",
                blocking_reason="LinkedIn opened the resume step, but OpenClaw did not expose a safe file input ref for upload.",
                screenshot_metadata_references=screenshots,
                checkpoint_urls=checkpoint_urls,
                page_title=page_title,
                warnings=warnings,
                errors=errors + ["resume_upload_ref_not_file_input"],
                page_diagnostics=page_diagnostics,
                form_diagnostics=form_diagnostics,
                debug_json=build_debug_json(),
            )
        if page_diagnostics["login_or_checkpoint_markers_present"] and not _as_bool(auth.get("session_available")):
            return _result(
                draft_status="not_started",
                source_status="login_required",
                awaiting_review=False,
                review_status="blocked",
                failure_category="login_required",
                blocking_reason="The application flow requires a logged-in session that is not currently available.",
                screenshot_metadata_references=screenshots,
                checkpoint_urls=checkpoint_urls,
                page_title=page_title,
                warnings=warnings,
                errors=errors,
                page_diagnostics=page_diagnostics,
                form_diagnostics=form_diagnostics,
                debug_json=build_debug_json(),
            )

        if inspect_only:
            return _result(
                draft_status="inspect_only",
                source_status="inspect_only",
                awaiting_review=False,
                review_status="inspect_only",
                failure_category=None,
                blocking_reason=None,
                screenshot_metadata_references=screenshots,
                checkpoint_urls=checkpoint_urls,
                page_title=page_title,
                warnings=warnings,
                errors=errors,
                page_diagnostics=page_diagnostics,
                form_diagnostics=form_diagnostics,
                debug_json=build_debug_json(),
                inspect_only=True,
            )

        if page_diagnostics.get("linkedin_state") == "easy_apply_contact_info_step":
            return _result(
                draft_status="draft_ready" if fields_filled_manifest else "not_started",
                source_status="success" if fields_filled_manifest else "manual_review_required",
                awaiting_review=bool(fields_filled_manifest),
                review_status="awaiting_review" if fields_filled_manifest else "blocked",
                failure_category=None if fields_filled_manifest else "manual_review_required",
                blocking_reason=None if fields_filled_manifest else "LinkedIn contact info is open, but Mission Control could not safely advance to the next step.",
                fields_filled_manifest=fields_filled_manifest,
                screenshot_metadata_references=screenshots,
                checkpoint_urls=checkpoint_urls,
                page_title=page_title,
                warnings=warnings + ["linkedin_contact_step_not_advanced"],
                errors=errors if fields_filled_manifest else errors + ["linkedin_contact_step_requires_manual_review"],
                page_diagnostics=page_diagnostics,
                form_diagnostics=form_diagnostics,
                debug_json=build_debug_json(),
            )

        resume_upload_path = _text(artifacts.get("resume_upload_path") or resume_variant.get("resume_upload_path"))
        selected_resume_verified = bool(page_diagnostics.get("selected_resume_verified"))
        if not _as_bool(constraints.get("skip_resume_upload")) and not selected_resume_verified:
            if not resume_upload_path:
                return _result(
                    draft_status="not_started",
                    source_status="upload_failed",
                    awaiting_review=False,
                    review_status="blocked",
                    failure_category="upload_failed",
                    blocking_reason="The tailored resume could not be uploaded successfully.",
                    screenshot_metadata_references=screenshots,
                    checkpoint_urls=checkpoint_urls,
                    page_title=page_title,
                    warnings=warnings,
                    errors=["resume_upload_path_missing"],
                    page_diagnostics=page_diagnostics,
                    form_diagnostics=form_diagnostics,
                    debug_json=build_debug_json(),
                )
            upload_validation_error = _validate_resume_upload_target(
                payload=payload,
                resume_upload_path=resume_upload_path,
                upload_ref=upload_ref,
                screenshots=screenshots,
                checkpoint_urls=checkpoint_urls,
                page_title=page_title,
                warnings=warnings,
                errors=errors,
                page_diagnostics=page_diagnostics,
                form_diagnostics=form_diagnostics,
                build_debug_json=build_debug_json,
            )
            if upload_validation_error is not None:
                return upload_validation_error
            if not upload_ref:
                if contact_field_actions:
                    warnings.append("resume_upload_ref_not_detected_on_current_step")
                else:
                    return _result(
                        draft_status="not_started",
                        source_status="unsupported_form",
                        awaiting_review=False,
                        review_status="blocked",
                        failure_category="unsupported_form",
                        blocking_reason="The form structure could not be safely automated in draft-only mode.",
                        screenshot_metadata_references=screenshots,
                        checkpoint_urls=checkpoint_urls,
                        page_title=page_title,
                        warnings=warnings,
                        errors=["resume_upload_ref_not_detected"],
                        page_diagnostics=page_diagnostics,
                        form_diagnostics=form_diagnostics,
                        debug_json=build_debug_json(),
                    )
            else:
                staged_upload_path = _safe_stage_upload(resume_upload_path, run_key=run_key)
                client.upload(staged_upload_path, input_ref=upload_ref.ref)
                record_linkedin_action(
                    state=_text(page_diagnostics.get("linkedin_state")),
                    action="upload_resume",
                    reason="current_step_file_input_detected",
                    chosen_ref=upload_ref.ref,
                    chosen_label=upload_ref.label,
                    upload_ref_value=upload_ref.ref,
                    advanced=True,
                )
                fields_filled_manifest.append(
                    {
                        "field_name": "resume_upload",
                        "label": upload_ref.label,
                        "field_type": "file",
                        "status": "uploaded",
                        "value_preview": staged_upload_path.name,
                        "value_redacted": True,
                    }
                )
                page_title, current_url = _browser_metadata(client)
                checkpoint_urls = _dedupe([application_url, current_url])
                snapshot_text = client.snapshot()
                refs, upload_ref, contact_field_actions, form_diagnostics = analyze_form(snapshot_text)
                page_diagnostics = _page_diagnostics(
                    application_url=application_url,
                    current_url=current_url,
                    page_title=page_title,
                    snapshot_text=snapshot_text,
                    refs=refs,
                    upload_ref=upload_ref,
                )
                linkedin_context = _linkedin_step_context(
                    snapshot_text=snapshot_text,
                    refs=refs,
                    upload_ref=upload_ref,
                    contact_field_actions=contact_field_actions,
                    page_diagnostics=page_diagnostics,
                )
                page_diagnostics.update(linkedin_context)
                page_diagnostics["linkedin_state"] = linkedin_context["state"]
                merge_linkedin_step_diagnostics(page_diagnostics)
                if capture_screenshots:
                    _capture_screenshot(
                        client,
                        screenshot_dir=screenshot_dir,
                        checkpoint_name="post-upload",
                        page_url=current_url,
                        screenshots=screenshots,
                        warnings=warnings,
                        screenshot_failures=screenshot_failures,
                        max_screenshots=max_screenshots,
                    )

        generic_plan: dict[str, Any] = {
            "answer_mappings": [],
            "missing_required_fields": [],
            "required_fields_filled": [],
            "self_id_handling_mode": "standard",
            "answer_confidences": [],
            "policy_matches": [],
            "answers_applied": [],
            "safe_skips": [],
        }
        fill_work_attempted = False
        linkedin_generic_iterations = 6 if _is_linkedin_easy_apply_target(payload) else 1
        linkedin_later_step_guard_triggered = False
        linkedin_later_step_guard_reason: str | None = None
        linkedin_later_step_review_handoff = False
        final_refs = refs
        final_upload_ref = upload_ref
        final_contact_actions = contact_field_actions
        final_form_diagnostics = form_diagnostics
        final_page_diagnostics = page_diagnostics

        for _ in range(linkedin_generic_iterations):
            iteration_signature_info: dict[str, Any] | None = None
            iteration_signature: str | None = None
            if _is_linkedin_easy_apply_target(payload) and str(page_diagnostics.get("linkedin_state") or "") in {
                "easy_apply_later_step",
                "easy_apply_review_step",
            }:
                linkedin_later_step_iteration_count += 1
                iteration_signature_info = _linkedin_step_signature(snapshot_text, refs, page_diagnostics)
                iteration_signature = _text(iteration_signature_info.get("signature")) or None
                if iteration_signature and iteration_signature == linkedin_last_step_signature:
                    linkedin_repeated_signature_count += 1
                else:
                    linkedin_repeated_signature_count = 0
                linkedin_last_step_signature = iteration_signature
                sync_later_step_runtime_diagnostics(
                    page_diagnostics,
                    refs,
                    signature_info=iteration_signature_info,
                )
                if iteration_signature_info.get("review_like"):
                    linkedin_later_step_review_handoff = True
                    break
                if linkedin_repeated_signature_count > DEFAULT_LINKEDIN_LATER_STEP_MAX_REPEATED_SIGNATURES:
                    linkedin_repeated_state_detected = True
                    linkedin_repeated_state_reason = "repeated_later_step_signature_without_meaningful_progress"
                    linkedin_later_step_guard_triggered = True
                    linkedin_later_step_guard_reason = linkedin_repeated_state_reason
                    warnings.append("linkedin_repeated_later_step_signature_detected")
                    sync_later_step_runtime_diagnostics(
                        page_diagnostics,
                        refs,
                        signature_info=iteration_signature_info,
                    )
                    break

            fill_payloads: list[dict[str, Any]] = []
            fill_manifest_rows: list[dict[str, Any]] = []
            planned_live_actions: list[dict[str, Any]] = []
            allocated_refs: set[str] = set()

            contact_field_actions = _plan_contact_field_actions(
                refs=refs,
                contact_values=contact_values,
                used_refs=allocated_refs,
            )
            form_diagnostics = _form_diagnostics(
                snapshot_text=snapshot_text,
                refs=refs,
                upload_ref=upload_ref,
                field_actions=contact_field_actions,
                radio_selection_attempts=radio_selection_attempts,
                dom_radio_groups=(
                    _linkedin_radio_groups_from_dom(client, snapshot_text)
                    if _is_linkedin_easy_apply_target(payload)
                    else []
                ),
            )
            contact_fill_payloads, contact_select_actions, contact_manifest_rows = _contact_fill_work(contact_field_actions)
            planned_live_actions.extend(_contact_execution_actions(contact_field_actions))
            fill_payloads.extend(contact_fill_payloads)
            fill_manifest_rows.extend(contact_manifest_rows)
            for action in contact_field_actions:
                if action.action == "prefilled_verified":
                    allocated_refs.add(action.candidate.ref.ref)
            for action in contact_fill_payloads:
                allocated_refs.add(str(action.get("ref")))
            for action in contact_select_actions:
                allocated_refs.add(action.candidate.ref.ref)
            select_actions: list[ContactFieldAction | dict[str, Any]] = list(contact_select_actions)

            generic_plan = _build_generic_answer_actions(
                refs=refs,
                used_refs=allocated_refs,
                answer_profile=answer_profile,
                application_target=target,
                answers=answers,
            )
            fill_payloads.extend(generic_plan["fill_payloads"])
            fill_manifest_rows.extend(generic_plan["manifest_rows"])
            planned_live_actions.extend(list(generic_plan.get("execution_actions") or []))
            for action in generic_plan["fill_payloads"]:
                allocated_refs.add(str(action.get("ref")))
            for action in generic_plan["select_actions"]:
                allocated_refs.add(str(action.get("ref")))
                select_actions.append(action)

            cover_letter_text = _text(payload.get("cover_letter_text"))
            if cover_letter_text and not _as_bool(constraints.get("skip_field_fills")):
                cover_ref = _find_text_ref(
                    refs,
                    keywords=["cover", "letter", "message", "summary"],
                    used_refs=allocated_refs,
                )
                if cover_ref:
                    allocated_refs.add(cover_ref.ref)
                    manifest_row = {
                        "field_name": f"ref_{cover_ref.ref}",
                        "label": cover_ref.label,
                        "field_type": "text",
                        "status": "filled",
                        "value_preview": None,
                        "value_redacted": True,
                    }
                    fill_payloads.append(
                        {
                            "ref": cover_ref.ref,
                            "value": cover_letter_text,
                            "type": "text",
                            "label": cover_ref.label,
                            "normalized_label": _normalize_label_text(_ref_search_text(cover_ref)),
                        }
                    )
                    fill_manifest_rows.append(manifest_row)
                    planned_live_actions.append(
                        {
                            "action_type": "fill",
                            "field_type": "text",
                            "original_ref": cover_ref.ref,
                            "value": cover_letter_text,
                            "label": cover_ref.label,
                            "normalized_label": _normalize_label_text(_ref_search_text(cover_ref)),
                            "manifest_row": manifest_row,
                        }
                    )

            for index, answer in enumerate(answers, start=1):
                answer_text = _text(answer.get("answer"))
                question_text = _text(answer.get("question"))
                if not answer_text or _as_bool(constraints.get("skip_field_fills")):
                    continue
                mapped_question = normalize_canonical_key(question_text)
                if isinstance(mapped_question, dict):
                    continue
                keywords = _tokenize(question_text)[:6]
                ref = _find_text_ref(refs, keywords=keywords, used_refs=allocated_refs)
                if ref is None and index == 1:
                    ref = _find_generic_text_ref(refs, allocated_refs)
                if ref is None:
                    continue
                allocated_refs.add(ref.ref)
                manifest_row = {
                    "field_name": f"application_answer_{index}",
                    "label": ref.label,
                    "field_type": "text",
                    "status": "answered",
                    "value_preview": None,
                    "value_redacted": True,
                }
                fill_payloads.append(
                    {
                        "ref": ref.ref,
                        "value": answer_text,
                        "type": "text",
                        "label": ref.label,
                        "normalized_label": _normalize_label_text(_ref_search_text(ref)),
                    }
                )
                fill_manifest_rows.append(manifest_row)
                planned_live_actions.append(
                    {
                        "action_type": "fill",
                        "field_type": "text",
                        "original_ref": ref.ref,
                        "value": answer_text,
                        "label": ref.label,
                        "normalized_label": _normalize_label_text(_ref_search_text(ref)),
                        "manifest_row": manifest_row,
                    }
                )

            later_step_safe_skips = list(generic_plan.get("safe_skips") or [])
            later_step_optional_steps: list[dict[str, Any]] = []
            if _is_linkedin_easy_apply_target(payload) and str(page_diagnostics.get("linkedin_state") or "") in {
                "easy_apply_later_step",
                "easy_apply_review_step",
            }:
                if _linkedin_top_choice_optional_step(snapshot_text):
                    later_step_optional_steps.append(
                        {
                            "step": "top_choice",
                            "reason": "optional_top_choice_left_unchecked",
                        }
                    )
                for ref in _linkedin_follow_company_optional_refs(refs):
                    later_step_safe_skips.append(
                        {
                            "canonical_key": None,
                            "label": _ref_prompt_label(ref),
                            "reason": "optional_follow_company_left_unchecked",
                        }
                    )
                if not any(row.get("reason") == "optional_follow_company_left_unchecked" for row in later_step_safe_skips):
                    raw_snapshot_lower = snapshot_text.lower()
                    if "follow" in raw_snapshot_lower and "company" in raw_snapshot_lower:
                        later_step_safe_skips.append(
                            {
                                "canonical_key": None,
                                "label": "Follow company",
                                "reason": "optional_follow_company_left_unchecked",
                            }
                        )
                update_later_step_diagnostics(
                    page_diagnostics,
                    refs,
                    policy_matches=list(generic_plan.get("policy_matches") or []),
                    answers_applied=list(generic_plan.get("answers_applied") or []),
                    safe_skips=later_step_safe_skips,
                    optional_steps_skipped=later_step_optional_steps,
                    personal_answer_fallbacks=list(generic_plan.get("personal_answer_fallbacks_used") or []),
                )
                linkedin_last_policy_match = (
                    dict(generic_plan["policy_matches"][-1]) if list(generic_plan.get("policy_matches") or []) else linkedin_last_policy_match
                )
                sync_later_step_runtime_diagnostics(
                    page_diagnostics,
                    refs,
                    signature_info=iteration_signature_info,
                )

            generic_answer_diagnostics = {
                "answer_mappings": list(generic_plan["answer_mappings"]),
                "missing_required_fields": list(generic_plan["missing_required_fields"]),
                "required_fields_filled": list(generic_plan["required_fields_filled"]),
                "self_id_handling_mode": generic_plan["self_id_handling_mode"],
            }
            iteration_fill_work_attempted = bool(fill_payloads or select_actions)
            fill_work_attempted = fill_work_attempted or iteration_fill_work_attempted
            linkedin_live_execution = _is_linkedin_easy_apply_target(payload) and str(page_diagnostics.get("linkedin_state") or "") in {
                "easy_apply_later_step",
                "easy_apply_review_step",
            }
            if linkedin_live_execution:
                if iteration_signature:
                    starting_budget = int(linkedin_action_budget_by_signature.get(iteration_signature) or 0)
                    if starting_budget >= DEFAULT_LINKEDIN_LATER_STEP_MAX_ACTIONS_PER_SIGNATURE:
                        linkedin_repeated_state_detected = True
                        linkedin_repeated_state_reason = "later_step_action_budget_exhausted"
                        linkedin_later_step_guard_triggered = True
                        linkedin_later_step_guard_reason = linkedin_repeated_state_reason
                        warnings.append("linkedin_later_step_action_budget_exhausted")
                        sync_later_step_runtime_diagnostics(
                            page_diagnostics,
                            refs,
                            signature_info=iteration_signature_info,
                        )
                        break
                for planned_action in planned_live_actions:
                    if iteration_signature:
                        current_budget = int(linkedin_action_budget_by_signature.get(iteration_signature) or 0)
                        if current_budget >= DEFAULT_LINKEDIN_LATER_STEP_MAX_ACTIONS_PER_SIGNATURE:
                            linkedin_repeated_state_detected = True
                            linkedin_repeated_state_reason = "later_step_action_budget_exhausted"
                            linkedin_later_step_guard_triggered = True
                            linkedin_later_step_guard_reason = linkedin_repeated_state_reason
                            warnings.append("linkedin_later_step_action_budget_exhausted")
                            sync_later_step_runtime_diagnostics(
                                page_diagnostics,
                                refs,
                                signature_info=iteration_signature_info,
                            )
                            break
                    live_state = execute_linkedin_live_action(
                        planned_action,
                        current_state={
                            "page_title": page_title,
                            "current_url": current_url,
                            "checkpoint_urls": checkpoint_urls,
                            "snapshot_text": snapshot_text,
                            "refs": refs,
                            "upload_ref": upload_ref,
                            "contact_field_actions": contact_field_actions,
                            "form_diagnostics": form_diagnostics,
                            "page_diagnostics": page_diagnostics,
                        },
                    )
                    page_title = live_state["page_title"]
                    current_url = live_state["current_url"]
                    checkpoint_urls = live_state["checkpoint_urls"]
                    snapshot_text = live_state["snapshot_text"]
                    refs = live_state["refs"]
                    upload_ref = live_state["upload_ref"]
                    contact_field_actions = live_state["contact_field_actions"]
                    form_diagnostics = live_state["form_diagnostics"]
                    page_diagnostics = live_state["page_diagnostics"]
                    fields_filled_manifest.append(dict(planned_action.get("manifest_row") or {}))
                    if iteration_signature:
                        linkedin_action_budget_by_signature[iteration_signature] = int(
                            linkedin_action_budget_by_signature.get(iteration_signature) or 0
                        ) + 1
                if linkedin_later_step_guard_triggered:
                    break
            else:
                if fill_payloads:
                    client.fill(fill_payloads)
                for action in select_actions:
                    if isinstance(action, ContactFieldAction):
                        client.select(action.candidate.ref.ref, str(action.value))
                    else:
                        client.select(str(action["ref"]), str(action["value"]))
                fields_filled_manifest.extend(fill_manifest_rows)
            if iteration_fill_work_attempted and capture_screenshots:
                _capture_screenshot(
                    client,
                    screenshot_dir=screenshot_dir,
                    checkpoint_name="post-fill",
                    page_url=current_url,
                    screenshots=screenshots,
                    warnings=warnings,
                    screenshot_failures=screenshot_failures,
                    max_screenshots=max_screenshots,
                )

            live_state = capture_live_state()
            page_title = live_state["page_title"]
            current_url = live_state["current_url"]
            checkpoint_urls = live_state["checkpoint_urls"]
            snapshot_text = live_state["snapshot_text"]
            refs = live_state["refs"]
            upload_ref = live_state["upload_ref"]
            contact_field_actions = live_state["contact_field_actions"]
            form_diagnostics = live_state["form_diagnostics"]
            page_diagnostics = live_state["page_diagnostics"]
            final_refs = refs
            final_upload_ref = upload_ref
            final_contact_actions = contact_field_actions
            final_form_diagnostics = form_diagnostics
            final_page_diagnostics = page_diagnostics

            if linkedin_later_step_guard_triggered:
                break
            if not _is_linkedin_easy_apply_target(payload):
                break
            if str(page_diagnostics.get("linkedin_state") or "") == "easy_apply_review_step":
                break
            post_iteration_signature_info = _linkedin_step_signature(snapshot_text, refs, page_diagnostics)
            sync_later_step_runtime_diagnostics(
                page_diagnostics,
                refs,
                signature_info=post_iteration_signature_info,
            )
            if post_iteration_signature_info.get("review_like"):
                linkedin_later_step_review_handoff = True
                break
            if str(page_diagnostics.get("linkedin_state") or "") != "easy_apply_later_step":
                break
            if generic_answer_diagnostics["missing_required_fields"]:
                break
            chosen_ref = _text(page_diagnostics.get("next_ref") or page_diagnostics.get("next_button_ref"))
            chosen_label = _text(page_diagnostics.get("next_ref_label") or page_diagnostics.get("next_button_label"))
            if not chosen_ref:
                break
            previous_state = str(page_diagnostics.get("linkedin_state") or "")
            previous_url = current_url
            previous_excerpt = snapshot_text[:DEFAULT_MAX_SNAPSHOT_CHARS]
            previous_signature = _text(post_iteration_signature_info.get("signature") if post_iteration_signature_info else iteration_signature)
            live_state = execute_linkedin_live_action(
                {
                    "action_type": "click",
                    "field_type": "button",
                    "original_ref": chosen_ref,
                    "label": chosen_label,
                    "normalized_label": _normalize_label_text(chosen_label),
                }
            )
            page_title = live_state["page_title"]
            current_url = live_state["current_url"]
            checkpoint_urls = live_state["checkpoint_urls"]
            snapshot_text = live_state["snapshot_text"]
            refs = live_state["refs"]
            upload_ref = live_state["upload_ref"]
            contact_field_actions = live_state["contact_field_actions"]
            form_diagnostics = live_state["form_diagnostics"]
            page_diagnostics = live_state["page_diagnostics"]
            post_click_signature_info = _linkedin_step_signature(snapshot_text, refs, page_diagnostics)
            sync_later_step_runtime_diagnostics(
                page_diagnostics,
                refs,
                signature_info=post_click_signature_info,
            )
            advanced = bool(
                current_url != previous_url
                or page_diagnostics.get("linkedin_state") != previous_state
                or snapshot_text[:DEFAULT_MAX_SNAPSHOT_CHARS] != previous_excerpt
                or _text(post_click_signature_info.get("signature")) != previous_signature
            )
            record_linkedin_action(
                state=previous_state,
                action="click_next",
                reason="later_step_safe_to_advance",
                chosen_ref=chosen_ref,
                chosen_label=chosen_label,
                upload_ref_value=_text(page_diagnostics.get("upload_ref")),
                advanced=advanced,
            )
            final_refs = refs
            final_upload_ref = upload_ref
            final_contact_actions = contact_field_actions
            final_form_diagnostics = form_diagnostics
            final_page_diagnostics = page_diagnostics
            if not advanced:
                if _text(post_click_signature_info.get("signature")) == previous_signature:
                    linkedin_repeated_state_detected = True
                    linkedin_repeated_state_reason = "next_click_no_progress_same_signature"
                    linkedin_later_step_guard_triggered = True
                    linkedin_later_step_guard_reason = linkedin_repeated_state_reason
                    sync_later_step_runtime_diagnostics(
                        page_diagnostics,
                        refs,
                        signature_info=post_click_signature_info,
                    )
                warnings.append("linkedin_later_step_next_click_did_not_advance")
                break

        submit_decision = _submit_decision(
            answer_profile=answer_profile,
            missing_required_fields=list(generic_answer_diagnostics["missing_required_fields"]),
            answer_confidences=list(generic_plan["answer_confidences"]),
            review_step_visible=bool(
                final_page_diagnostics.get("linkedin_state") == "easy_apply_review_step"
                or bool(final_page_diagnostics.get("review_like_step_detected"))
                or "review your application" in snapshot_text.lower()
            ),
        )
        if _is_linkedin_easy_apply_target(payload):
            submit_decision = {
                **submit_decision,
                "should_auto_submit": False,
                "reason": (
                    "linkedin_manual_submit_only"
                    if (
                        final_page_diagnostics.get("linkedin_state") == "easy_apply_review_step"
                        or final_page_diagnostics.get("review_like_step_detected")
                    )
                    else submit_decision["reason"]
                ),
            }
        generic_answer_diagnostics["submit_decision"] = submit_decision
        final_page_diagnostics.update(
            {
                "self_id_handling_mode": generic_answer_diagnostics["self_id_handling_mode"],
                "answer_mappings": generic_answer_diagnostics["answer_mappings"],
                "confidence_score_used": submit_decision["confidence_score"],
                "submit_decision_reason": submit_decision["reason"],
                "should_auto_submit": submit_decision["should_auto_submit"],
                "submit_min_confidence": submit_decision["min_confidence"],
            }
        )
        final_form_diagnostics = {
            **final_form_diagnostics,
            "answer_mappings": generic_answer_diagnostics["answer_mappings"],
            "missing_required_fields": generic_answer_diagnostics["missing_required_fields"],
            "required_fields_filled": generic_answer_diagnostics["required_fields_filled"],
            "self_id_handling_mode": generic_answer_diagnostics["self_id_handling_mode"],
            "later_step_policy_matches": final_page_diagnostics.get("later_step_policy_matches") or [],
            "later_step_answers_applied": final_page_diagnostics.get("later_step_answers_applied") or [],
            "later_step_safe_skips": final_page_diagnostics.get("later_step_safe_skips") or [],
            "later_step_optional_steps_skipped": final_page_diagnostics.get("later_step_optional_steps_skipped") or [],
            "later_step_personal_answer_fallbacks_used": final_page_diagnostics.get("later_step_personal_answer_fallbacks_used") or [],
            "later_step_action_diagnostics": final_page_diagnostics.get("later_step_action_diagnostics") or [],
            "later_step_iteration_count": final_page_diagnostics.get("later_step_iteration_count") or 0,
            "repeated_signature_count": final_page_diagnostics.get("repeated_signature_count") or 0,
            "last_step_signature": final_page_diagnostics.get("last_step_signature"),
            "last_action_attempted": final_page_diagnostics.get("last_action_attempted"),
            "last_field_targeted": final_page_diagnostics.get("last_field_targeted"),
            "last_policy_match": final_page_diagnostics.get("last_policy_match"),
            "last_visible_labels": final_page_diagnostics.get("last_visible_labels") or [],
            "last_progress_percent": final_page_diagnostics.get("last_progress_percent"),
            "repeated_state_detected": bool(final_page_diagnostics.get("repeated_state_detected")),
            "repeated_state_reason": final_page_diagnostics.get("repeated_state_reason"),
            "confidence_score_used": submit_decision["confidence_score"],
            "submit_decision_reason": submit_decision["reason"],
            "should_auto_submit": submit_decision["should_auto_submit"],
            "submit_min_confidence": submit_decision["min_confidence"],
        }
        if _detect_keywords(_combine_text(current_url, page_title, snapshot_text), SUBMIT_HINTS) and "thank" in snapshot_text.lower():
            return _result(
                draft_status="partial_draft" if fields_filled_manifest else "not_started",
                source_status="unsafe_submit_attempted",
                awaiting_review=False,
                review_status="blocked",
                failure_category="unsafe_submit_attempted",
                blocking_reason="The page appeared to move beyond the draft stage unexpectedly, so Mission Control blocked the result.",
                fields_filled_manifest=fields_filled_manifest,
                screenshot_metadata_references=screenshots,
                checkpoint_urls=checkpoint_urls,
                page_title=page_title,
                warnings=warnings + ["unexpected_post_fill_submit_indicators"],
                errors=errors + ["unsafe_submit_indicators_detected"],
                page_diagnostics=final_page_diagnostics,
                form_diagnostics=final_form_diagnostics,
                debug_json=build_debug_json(),
            )

        if generic_answer_diagnostics["missing_required_fields"]:
            return _result(
                draft_status="partial_draft" if fields_filled_manifest else "not_started",
                source_status="manual_review_required",
                awaiting_review=bool(fields_filled_manifest),
                review_status="awaiting_review" if fields_filled_manifest else "blocked",
                failure_category="manual_review_required",
                blocking_reason="Automation stopped because one or more required application questions did not have a safe high-confidence answer.",
                fields_filled_manifest=fields_filled_manifest,
                screenshot_metadata_references=screenshots,
                checkpoint_urls=checkpoint_urls,
                page_title=page_title,
                warnings=warnings,
                errors=errors + ["missing_required_fields_for_safe_progression"],
                page_diagnostics=final_page_diagnostics,
                form_diagnostics=final_form_diagnostics,
                debug_json=build_debug_json(),
            )

        if linkedin_later_step_guard_triggered and not linkedin_later_step_review_handoff:
            return _result(
                draft_status="partial_draft" if fields_filled_manifest else "not_started",
                source_status="manual_review_required",
                awaiting_review=bool(fields_filled_manifest),
                review_status="awaiting_review" if fields_filled_manifest else "blocked",
                failure_category="manual_review_required",
                blocking_reason=(
                    "LinkedIn later-step automation stopped after repeated unchanged steps to avoid timing out before review."
                    if not linkedin_later_step_guard_reason
                    else f"LinkedIn later-step automation stopped safely: {linkedin_later_step_guard_reason}."
                ),
                fields_filled_manifest=fields_filled_manifest,
                screenshot_metadata_references=screenshots,
                checkpoint_urls=checkpoint_urls,
                page_title=page_title,
                warnings=warnings,
                errors=errors + ["linkedin_later_step_loop_guard_triggered"],
                page_diagnostics=final_page_diagnostics,
                form_diagnostics=final_form_diagnostics,
                debug_json=build_debug_json(),
            )

        if not fields_filled_manifest:
            return _result(
                draft_status="not_started",
                source_status="manual_review_required",
                awaiting_review=False,
                review_status="blocked",
                failure_category="manual_review_required",
                blocking_reason="Automation opened the application safely, but did not find any high-confidence fields to fill.",
                screenshot_metadata_references=screenshots,
                checkpoint_urls=checkpoint_urls,
                page_title=page_title,
                warnings=warnings,
                errors=errors + ["no_high_confidence_fields_filled"],
                page_diagnostics=final_page_diagnostics,
                form_diagnostics=final_form_diagnostics,
                debug_json=build_debug_json(),
            )

        return _result(
            draft_status="draft_ready",
            source_status="success",
            awaiting_review=True,
            review_status="awaiting_review",
            failure_category=None,
            blocking_reason=None,
            fields_filled_manifest=fields_filled_manifest,
            screenshot_metadata_references=screenshots,
            checkpoint_urls=checkpoint_urls,
            page_title=page_title,
            warnings=warnings,
            errors=errors,
            page_diagnostics=final_page_diagnostics,
            form_diagnostics=final_form_diagnostics,
            debug_json=build_debug_json(),
        )
    except BrowserCommandError as exc:
        last_error = exc
        return _result(
            draft_status="not_started" if not fields_filled_manifest else "partial_draft",
            source_status=exc.failure_category,
            awaiting_review=False,
            review_status="blocked",
            failure_category=exc.failure_category,
            blocking_reason=exc.blocking_reason,
            fields_filled_manifest=fields_filled_manifest,
            screenshot_metadata_references=screenshots,
            checkpoint_urls=checkpoint_urls,
            page_title=None,
            warnings=warnings,
            errors=errors + exc.errors,
            page_diagnostics=page_diagnostics,
            form_diagnostics=form_diagnostics,
            debug_json=build_debug_json(),
        )
    finally:
        if staged_upload_path is not None:
            staged_upload_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for the repo-local OpenClaw apply backend.

    Example:
    `python3 scripts/openclaw_apply_browser_backend.py < payload.json`

    Example with file input:
    `python3 scripts/openclaw_apply_browser_backend.py --input-json-file payload.json`
    """

    parser = argparse.ArgumentParser(description="Mission Control OpenClaw browser backend")
    parser.add_argument("--input-json-file", dest="input_json_file", help="Path to an input JSON payload file.")
    args = parser.parse_args(argv)

    try:
        payload = read_payload(args.input_json_file)
        result = run_backend(payload)
    except ValueError as exc:
        result = invalid_input_result([str(exc)])
    print(json.dumps(result, ensure_ascii=True))
    return 0
