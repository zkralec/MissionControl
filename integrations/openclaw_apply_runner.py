from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, replace
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
DEFAULT_HOST_GATEWAY_URL = "ws://127.0.0.1:18789"
DEFAULT_HOST_CDP_URL = "http://127.0.0.1:18800"

DEFAULT_TIMEOUT_SECONDS = 240
MAX_TIMEOUT_SECONDS = 1200
DEFAULT_MAX_STEPS = 24
MAX_MAX_STEPS = 200
DEFAULT_MAX_SCREENSHOTS = 8
MAX_MAX_SCREENSHOTS = 20
DEFAULT_ALLOWED_RESUME_EXTENSIONS = (".pdf", ".doc", ".docx", ".txt", ".rtf")
LINKEDIN_ALLOWED_RESUME_EXTENSIONS = (".pdf", ".docx", ".doc")

MEANINGFUL_DRAFT_STATUSES = {"draft_ready", "partial_draft"}

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
    "anti_bot_blocked": "The page blocked automation with anti-bot defenses and should be handled manually.",
    "session_expired": "The existing browser session expired before the draft could be prepared.",
    "unsupported_form": "The form structure could not be safely automated in draft-only mode.",
    "upload_failed": "The tailored resume could not be uploaded successfully.",
    "redirected_off_target": "The browser was redirected away from the intended application target.",
    "navigation_failed": "The application page could not be opened or safely progressed.",
    "timed_out": "The browser runner hit its time budget before reaching a safe review checkpoint.",
    "manual_review_required": "Automation stopped at a manual review checkpoint without enough confidence to continue.",
    "unsafe_submit_attempted": "The browser runner reported a submit attempt, so Mission Control blocked the result.",
    "tool_unavailable": "No OpenClaw adapter is currently available for Mission Control to invoke.",
    "invalid_input": "Mission Control sent an invalid apply-draft payload.",
    "unsupported_resume_upload_format": "The target site requires a PDF or Word resume upload, but Mission Control did not have a compatible file artifact.",
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


def _running_in_docker() -> bool:
    return Path("/.dockerenv").exists()


def _host_visible_path(path: Path) -> str:
    resolved = str(path.resolve())
    if resolved == "/data":
        return "data"
    if resolved.startswith("/data/"):
        return "data/" + resolved[len("/data/") :]
    return resolved


def _host_compatible_path(path_text: str | None) -> str | None:
    raw = str(path_text or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if candidate.exists():
        return str(candidate.resolve())

    prefixes = {
        "/app/data": ROOT / "data",
        "/data": ROOT / "data",
        "/app": ROOT,
    }
    for prefix, replacement_root in prefixes.items():
        if raw == prefix:
            replacement = replacement_root
        elif raw.startswith(prefix + "/"):
            replacement = replacement_root / raw[len(prefix) + 1 :]
        else:
            continue
        if replacement.exists():
            return str(replacement.resolve())
    return raw


def _host_visible_request(request: dict[str, Any]) -> dict[str, Any]:
    host_request = dict(request)

    resume_variant = dict(host_request.get("resume_variant") or {})
    resume_upload_path = _host_compatible_path(resume_variant.get("resume_upload_path"))
    if resume_upload_path:
        resume_variant["resume_upload_path"] = resume_upload_path
    host_request["resume_variant"] = resume_variant

    artifacts = dict(host_request.get("artifacts") or {})
    for key in ("receipt_path", "screenshot_dir", "resume_upload_path", "progress_snapshot_path"):
        normalized = _host_compatible_path(artifacts.get(key))
        if normalized:
            artifacts[key] = normalized
    host_request["artifacts"] = artifacts
    return host_request


def _looks_sensitive(name: str | None, label: str | None, field_type: str | None) -> bool:
    text = " ".join(filter(None, [name, label, field_type])).lower()
    return any(token in text for token in ("password", "secret", "token", "otp", "passcode"))


def _file_ext(file_name: str | None, *, default: str = ".txt") -> str:
    suffix = Path(str(file_name or "").strip()).suffix
    return suffix if suffix else default


def _target_host(payload: dict[str, Any]) -> str:
    target = payload.get("application_target") if isinstance(payload.get("application_target"), dict) else {}
    application_url = str(target.get("application_url") or target.get("source_url") or "").strip()
    if not application_url:
        return ""
    return urlparse(application_url).netloc.lower()


def _is_linkedin_easy_apply_target(payload: dict[str, Any]) -> bool:
    host = _target_host(payload)
    return host.endswith("linkedin.com")


def _target_resume_extensions(payload: dict[str, Any], *, default_extensions: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    if _is_linkedin_easy_apply_target(payload):
        return "linkedin_easy_apply", LINKEDIN_ALLOWED_RESUME_EXTENSIONS
    return "default", default_extensions


def _resume_extensions_from_env() -> tuple[str, ...]:
    raw = str(os.getenv("OPENCLAW_APPLY_ALLOWED_RESUME_EXTENSIONS") or "").strip()
    if not raw:
        return DEFAULT_ALLOWED_RESUME_EXTENSIONS
    output: list[str] = []
    for item in raw.split(","):
        normalized = item.strip().lower()
        if not normalized:
            continue
        output.append(normalized if normalized.startswith(".") else f".{normalized}")
    return tuple(output) or DEFAULT_ALLOWED_RESUME_EXTENSIONS


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
    inspect_only: bool
    allowed_resume_extensions: tuple[str, ...]
    auth_strategy: str | None
    storage_state_path: str | None
    browser_profile_path: str | None
    browser_attach_mode: bool
    skip_browser_start: bool
    allow_browser_start: bool
    gateway_url: str | None
    cdp_url: str | None
    host_gateway_alias: str | None
    run_on_host: bool
    host_gateway_url: str
    host_cdp_url: str


@dataclass(frozen=True)
class ArtifactPaths:
    run_key: str
    screenshot_dir: Path
    receipt_path: Path
    progress_snapshot_path: Path
    generated_resume_path: Path
    host_handoff_request_path: Path
    host_handoff_result_path: Path


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
        command_debug = {
            "command": list(self._command),
            "exit_code": completed.returncode,
            "stdout": completed.stdout.decode("utf-8", errors="replace").strip(),
            "stderr": completed.stderr.decode("utf-8", errors="replace").strip(),
        }
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
                "debug_json": {"adapter_command": command_debug},
            }
        stdout = command_debug["stdout"]
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
                "debug_json": {"adapter_command": command_debug},
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
                "debug_json": {"adapter_command": command_debug},
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
                "debug_json": {"adapter_command": command_debug},
            }
        debug_json = parsed.get("debug_json") if isinstance(parsed.get("debug_json"), dict) else {}
        parsed["debug_json"] = {"adapter_command": command_debug, **debug_json}
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
        inspect_only=bool(_as_bool(os.getenv("OPENCLAW_APPLY_INSPECT_ONLY"), default=False)),
        allowed_resume_extensions=_resume_extensions_from_env(),
        auth_strategy=str(os.getenv("OPENCLAW_APPLY_AUTH_STRATEGY") or "").strip() or None,
        storage_state_path=str(os.getenv("OPENCLAW_APPLY_STORAGE_STATE_PATH") or "").strip() or None,
        browser_profile_path=str(os.getenv("OPENCLAW_APPLY_BROWSER_PROFILE_PATH") or "").strip() or None,
        browser_attach_mode=bool(_as_bool(os.getenv("OPENCLAW_APPLY_BROWSER_ATTACH_MODE"), default=False)),
        skip_browser_start=bool(
            _as_bool(
                os.getenv("OPENCLAW_APPLY_SKIP_BROWSER_START"),
                default=_as_bool(os.getenv("OPENCLAW_APPLY_BROWSER_ATTACH_MODE"), default=False),
            )
        ),
        allow_browser_start=bool(
            _as_bool(
                os.getenv("OPENCLAW_APPLY_ALLOW_BROWSER_START"),
                default=not bool(_as_bool(os.getenv("OPENCLAW_APPLY_BROWSER_ATTACH_MODE"), default=False)),
            )
        ),
        gateway_url=str(os.getenv("OPENCLAW_APPLY_GATEWAY_URL") or "").strip() or None,
        cdp_url=str(os.getenv("OPENCLAW_APPLY_CDP_URL") or "").strip() or None,
        host_gateway_alias=str(os.getenv("OPENCLAW_APPLY_HOST_GATEWAY_ALIAS") or "").strip() or None,
        run_on_host=bool(_as_bool(os.getenv("OPENCLAW_APPLY_RUN_ON_HOST"), default=False)),
        host_gateway_url=str(os.getenv("OPENCLAW_APPLY_HOST_GATEWAY_URL") or DEFAULT_HOST_GATEWAY_URL).strip() or DEFAULT_HOST_GATEWAY_URL,
        host_cdp_url=str(os.getenv("OPENCLAW_APPLY_HOST_CDP_URL") or DEFAULT_HOST_CDP_URL).strip() or DEFAULT_HOST_CDP_URL,
    )


def _host_local_script_command(script_name: str) -> str:
    return f"{shlex.quote(sys.executable or 'python3')} {shlex.quote(str(ROOT / 'scripts' / script_name))}"


def _looks_like_openclaw_browser_base_command(command_text: str | None) -> bool:
    parts = [part for part in shlex.split(str(command_text or "")) if part.strip()]
    if not parts:
        return False
    executable_name = Path(parts[0]).name.lower()
    if "openclaw" not in executable_name:
        return False
    return "browser" in parts[1:]


def _payload_requests_host_run(payload: dict[str, Any]) -> bool:
    browser = payload.get("browser") if isinstance(payload.get("browser"), dict) else {}
    return bool(_as_bool(browser.get("run_on_host"), default=False))


def _host_mode_normalized_config(config: RunnerConfig, logger: logging.Logger) -> RunnerConfig:
    if not config.run_on_host or _running_in_docker():
        return config

    tool_command = str(config.tool_command or "").strip() or None
    browser_command_env = str(os.getenv("OPENCLAW_APPLY_BROWSER_COMMAND") or "").strip() or None
    browser_base_command = str(os.getenv("OPENCLAW_BROWSER_BASE_COMMAND") or "").strip() or None
    host_tool_bridge = _host_local_script_command("openclaw_apply_tool_bridge.py")
    host_browser_backend = _host_local_script_command("openclaw_apply_browser_backend.py")
    selected_browser_base_command = browser_base_command
    tool_command_is_browser_base = _looks_like_openclaw_browser_base_command(tool_command)
    browser_command_is_browser_base = _looks_like_openclaw_browser_base_command(browser_command_env)

    if tool_command_is_browser_base:
        selected_browser_base_command = str(tool_command)
        tool_command = host_tool_bridge
        logger.warning("Host mode reinterpreted OPENCLAW_APPLY_TOOL_COMMAND as a browser base command and routed it through the local bridge.")

    if browser_command_is_browser_base:
        if not tool_command_is_browser_base:
            selected_browser_base_command = str(browser_command_env)
        os.environ["OPENCLAW_APPLY_BROWSER_COMMAND"] = host_browser_backend
        logger.warning("Host mode reinterpreted OPENCLAW_APPLY_BROWSER_COMMAND as a browser base command and routed it through the local backend.")
    elif browser_command_env is None or "/app/scripts/openclaw_apply_browser_backend.py" in browser_command_env:
        os.environ["OPENCLAW_APPLY_BROWSER_COMMAND"] = host_browser_backend

    if selected_browser_base_command:
        os.environ["OPENCLAW_BROWSER_BASE_COMMAND"] = selected_browser_base_command

    if tool_command is None or "/app/scripts/openclaw_apply_tool_bridge.py" in str(tool_command):
        tool_command = host_tool_bridge

    return replace(config, tool_command=tool_command)


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
    host_handoff_dir = config.receipt_root / "host_handoff"
    host_handoff_dir.mkdir(parents=True, exist_ok=True)
    resume_variant = payload.get("resume_variant") if isinstance(payload.get("resume_variant"), dict) else {}
    generated_resume_path = config.resume_root / f"{run_key}{_file_ext(str(resume_variant.get('resume_file_name') or ''), default='.txt')}"
    receipt_path = config.receipt_root / f"{run_key}.json"
    progress_snapshot_path = config.receipt_root / f"{run_key}.progress.json"
    return ArtifactPaths(
        run_key=run_key,
        screenshot_dir=screenshot_dir,
        receipt_path=receipt_path,
        progress_snapshot_path=progress_snapshot_path,
        generated_resume_path=generated_resume_path,
        host_handoff_request_path=host_handoff_dir / f"{run_key}.input.json",
        host_handoff_result_path=host_handoff_dir / f"{run_key}.result.json",
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


def _resume_upload_candidates(resume_variant: dict[str, Any], requested_path: str | None) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add(raw_path: Any) -> None:
        normalized = _host_compatible_path(str(raw_path or "").strip())
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        candidates.append(normalized)

    add(requested_path)
    for key in ("resume_pdf_path", "resume_docx_path", "resume_doc_path"):
        add(resume_variant.get(key))

    extra_candidates = resume_variant.get("resume_upload_candidates")
    if isinstance(extra_candidates, list):
        for raw_path in extra_candidates:
            add(raw_path)

    if requested_path:
        base_candidate = Path(requested_path).expanduser()
        for extension in LINKEDIN_ALLOWED_RESUME_EXTENSIONS:
            add(str(base_candidate.with_suffix(extension)))
    return candidates


def _select_existing_resume_upload_path(
    *,
    payload: dict[str, Any],
    resume_variant: dict[str, Any],
    requested_path: str | None,
    logger: logging.Logger,
    warnings: list[str],
) -> str | None:
    candidates = _resume_upload_candidates(resume_variant, requested_path)
    existing_paths: list[str] = []
    for candidate_text in candidates:
        candidate = Path(candidate_text).expanduser()
        if candidate.exists() and candidate.is_file():
            existing_paths.append(str(candidate.resolve()))

    if not existing_paths:
        return None

    site_key, preferred_extensions = _target_resume_extensions(payload, default_extensions=DEFAULT_ALLOWED_RESUME_EXTENSIONS)
    if site_key == "linkedin_easy_apply":
        for extension in preferred_extensions:
            for path_text in existing_paths:
                if Path(path_text).suffix.lower() == extension:
                    if requested_path and Path(path_text).suffix.lower() != Path(requested_path).suffix.lower():
                        warnings.append("resume_upload_path_incompatible_with_target_site")
                    logger.info("Using %s resume upload file %s for LinkedIn Easy Apply", extension.lstrip("."), Path(path_text).name)
                    return path_text

    selected = existing_paths[0]
    logger.info("Using existing resume upload file %s", Path(selected).name)
    return selected


def _materialize_resume_file(payload: dict[str, Any], paths: ArtifactPaths, logger: logging.Logger) -> tuple[str | None, list[str]]:
    warnings: list[str] = []
    resume_variant = _require_dict(payload, "resume_variant")
    requested_path = _host_compatible_path(resume_variant.get("resume_upload_path"))
    if requested_path:
        candidate = Path(requested_path).expanduser()
        if not candidate.exists():
            warnings.append("configured_resume_upload_path_missing")
    selected_existing_path = _select_existing_resume_upload_path(
        payload=payload,
        resume_variant=resume_variant,
        requested_path=requested_path,
        logger=logger,
        warnings=warnings,
    )
    if selected_existing_path:
        return selected_existing_path, warnings

    resume_text = str(resume_variant.get("resume_variant_text") or "").strip()
    if not resume_text:
        return None, warnings

    if _is_linkedin_easy_apply_target(payload):
        warnings.append("resume_upload_text_only_for_linkedin")
        logger.warning("LinkedIn Easy Apply requires PDF, DOCX, or DOC resume uploads; plain-text fallback was skipped.")
        return None, warnings

    paths.generated_resume_path.write_text(resume_text + "\n", encoding="utf-8")
    logger.info("Materialized tailored resume upload file %s", paths.generated_resume_path.name)
    return str(paths.generated_resume_path.resolve()), warnings


def _validate_resume_upload_path(
    payload: dict[str, Any],
    resume_upload_path: str | None,
    *,
    allowed_extensions: tuple[str, ...],
    inspect_only: bool,
) -> dict[str, Any] | None:
    if inspect_only:
        return None
    path_text = str(_host_compatible_path(resume_upload_path) or "").strip()
    resume_text_present = bool(
        str(
            (
                _require_dict(payload, "resume_variant")
            ).get("resume_variant_text")
            or ""
        ).strip()
    )
    site_key, site_extensions = _target_resume_extensions(payload, default_extensions=allowed_extensions)
    if not path_text:
        if site_key == "linkedin_easy_apply" and resume_text_present:
            return invalid_input_result(
                [
                    "unsupported_resume_upload_format:text_only_resume_variant",
                    "resume_upload_site:linkedin_easy_apply",
                ],
                failure_category="unsupported_resume_upload_format",
            )
        return invalid_input_result(["resume_upload_path_missing"], failure_category="upload_failed")
    candidate = Path(path_text)
    if not candidate.exists() or not candidate.is_file():
        return invalid_input_result(["resume_upload_path_missing_or_not_file"], failure_category="upload_failed")
    extension = candidate.suffix.lower()
    if site_key == "linkedin_easy_apply" and extension not in set(site_extensions):
        return invalid_input_result(
            [
                f"unsupported_resume_upload_format:{extension or 'none'}",
                "resume_upload_site:linkedin_easy_apply",
                "resume_upload_allowed_extensions:.pdf,.docx,.doc",
            ],
            failure_category="unsupported_resume_upload_format",
        )
    if extension not in set(allowed_extensions):
        return invalid_input_result(
            [f"resume_upload_extension_not_allowed:{extension or 'none'}"],
            failure_category="upload_failed",
        )
    return None


def _sanitize_request_for_receipt(payload: dict[str, Any], *, materialized_resume_path: str | None) -> dict[str, Any]:
    target = payload.get("application_target") if isinstance(payload.get("application_target"), dict) else {}
    resume_variant = payload.get("resume_variant") if isinstance(payload.get("resume_variant"), dict) else {}
    candidate_profile = payload.get("candidate_profile") if isinstance(payload.get("candidate_profile"), dict) else {}
    contact_profile = payload.get("contact_profile") if isinstance(payload.get("contact_profile"), dict) else {}
    answers = payload.get("application_answers") if isinstance(payload.get("application_answers"), list) else []
    lineage = payload.get("lineage") if isinstance(payload.get("lineage"), dict) else {}
    browser = payload.get("browser") if isinstance(payload.get("browser"), dict) else {}
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
        "candidate_profile": {
            "resume_source": candidate_profile.get("resume_source"),
            "resume_name": candidate_profile.get("resume_name"),
            "contact_profile_fields": sorted(str(key) for key in contact_profile.keys()),
        },
        "answer_count": len(answers),
        "cover_letter_present": bool(str(payload.get("cover_letter_text") or "").strip()),
        "capture_screenshots": bool(payload.get("capture_screenshots", True)),
        "max_screenshots": payload.get("max_screenshots"),
        "inspect_only": bool(payload.get("inspect_only", False)),
        "browser": {
            "run_on_host": bool(browser.get("run_on_host", False)),
            "attach_mode": bool(browser.get("attach_mode", False)),
            "skip_browser_start": bool(browser.get("skip_browser_start", False)),
            "allow_browser_start": bool(browser.get("allow_browser_start", False)),
            "gateway_url": browser.get("gateway_url"),
            "cdp_url": browser.get("cdp_url"),
            "host_gateway_alias": browser.get("host_gateway_alias"),
        },
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


def _read_progress_snapshot(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _has_submit_signal(raw_result: dict[str, Any]) -> bool:
    return bool(raw_result.get("submitted")) or bool(raw_result.get("submit_clicked")) or bool(raw_result.get("final_submit_clicked"))


def _safe_auto_submit_signal(raw_result: dict[str, Any]) -> bool:
    page_diagnostics = raw_result.get("page_diagnostics") if isinstance(raw_result.get("page_diagnostics"), dict) else {}
    form_diagnostics = raw_result.get("form_diagnostics") if isinstance(raw_result.get("form_diagnostics"), dict) else {}
    auto_submit_allowed = bool(page_diagnostics.get("auto_submit_allowed") or form_diagnostics.get("auto_submit_allowed"))
    auto_submit_succeeded = bool(page_diagnostics.get("auto_submit_succeeded") or form_diagnostics.get("auto_submit_succeeded"))
    return bool(raw_result.get("submitted")) and auto_submit_allowed and auto_submit_succeeded


def _meaningful_draft_status(value: Any) -> bool:
    return str(value or "").strip().lower() in MEANINGFUL_DRAFT_STATUSES


def _normalize_source_status(raw_result: dict[str, Any], *, failure_category: str | None, meaningful_progress: bool) -> str:
    source_status = str(raw_result.get("source_status") or raw_result.get("status") or "").strip().lower()
    if source_status:
        return source_status
    if failure_category:
        return failure_category
    if meaningful_progress:
        return "success"
    return "navigation_failed"


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
    inspect_only = bool(raw_result.get("inspect_only")) or bool(payload.get("inspect_only"))

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
    failure_category = str(raw_result.get("failure_category") or "").strip().lower() or None
    blocking_reason = str(raw_result.get("blocking_reason") or "").strip() or _default_blocking_reason(failure_category)

    submitted_signal = _has_submit_signal(raw_result)
    safe_submitted = _safe_auto_submit_signal(raw_result)
    if submitted_signal and not safe_submitted:
        warnings.append("unsafe_submit_attempted_detected")
        errors.append("adapter_reported_submit_signal")
        failure_category = "unsafe_submit_attempted"
        blocking_reason = _default_blocking_reason(failure_category)

    draft_status = str(raw_result.get("draft_status") or "").strip().lower()
    if not draft_status:
        if inspect_only:
            draft_status = "inspect_only"
        elif meaningful_progress and not failure_category:
            draft_status = "draft_ready"
        elif meaningful_progress:
            draft_status = "partial_draft"
        else:
            draft_status = "not_started"

    checkpoint_urls = _dedupe_text(
        [str(raw_result.get("current_url") or "").strip()]
        + _as_text_list(raw_result.get("checkpoint_urls"))
        + [_application_url(payload)]
    )
    if str(raw_result.get("redirect_target_url") or "").strip() and not checkpoint_urls:
        checkpoint_urls.append(str(raw_result.get("redirect_target_url") or "").strip())

    if bool(raw_result.get("redirected_off_target")):
        failure_category = failure_category or "redirected_off_target"
        blocking_reason = blocking_reason or _default_blocking_reason(failure_category)

    if bool(raw_result.get("session_expired")):
        failure_category = failure_category or "session_expired"
        blocking_reason = blocking_reason or _default_blocking_reason(failure_category)

    if bool(raw_result.get("anti_bot_blocked")):
        failure_category = failure_category or "anti_bot_blocked"
        blocking_reason = blocking_reason or _default_blocking_reason(failure_category)

    if bool(raw_result.get("captcha_or_bot_challenge")):
        failure_category = failure_category or "captcha_or_bot_challenge"
        blocking_reason = blocking_reason or _default_blocking_reason(failure_category)

    source_status = _normalize_source_status(raw_result, failure_category=failure_category, meaningful_progress=meaningful_progress)
    if submitted_signal and not safe_submitted:
        source_status = "unsafe_submit_attempted"
    if inspect_only and source_status == "success":
        source_status = "inspect_only"

    review_requirements = {
        "draft_status": _meaningful_draft_status(draft_status),
        "fields_filled_manifest": len(fields_filled_manifest) > 0,
        "screenshot_metadata_references": len(screenshots) > 0,
        "checkpoint_urls": len(checkpoint_urls) > 0,
    }
    awaiting_review_raw = bool(raw_result.get("awaiting_review"))
    review_status_raw = str(raw_result.get("review_status") or "").strip().lower()
    review_ready = all(review_requirements.values())
    awaiting_review = (
        not inspect_only
        and not submitted_signal
        and review_ready
        and meaningful_progress
        and (awaiting_review_raw or review_status_raw == "awaiting_review" or source_status == "success")
    )
    missing_review_requirements = [name for name, present in review_requirements.items() if not present]
    if not awaiting_review and meaningful_progress and not submitted_signal and not inspect_only and missing_review_requirements:
        warnings.append("review_ready_validation_failed")
        errors.append("incomplete_review_package")
        failure_category = failure_category or "manual_review_required"
        blocking_reason = blocking_reason or (
            "Draft progress was recorded, but the review package is incomplete: " + ", ".join(missing_review_requirements)
        )
        source_status = "manual_review_required"
        if draft_status == "draft_ready":
            draft_status = "partial_draft"

    if awaiting_review:
        review_status = "awaiting_review"
    elif inspect_only:
        review_status = "inspect_only"
    elif review_status_raw and review_status_raw != "awaiting_review":
        review_status = review_status_raw
    else:
        review_status = "blocked"

    notify_decision = raw_result.get("notify_decision") if isinstance(raw_result.get("notify_decision"), dict) else {}
    debug_json = raw_result.get("debug_json") if isinstance(raw_result.get("debug_json"), dict) else {}
    notify_reason = str(notify_decision.get("reason") or "").strip() or (
        "application_submitted" if safe_submitted else ("draft_ready_for_review" if awaiting_review else (review_status or source_status or "blocked"))
    )
    should_notify = awaiting_review and not submitted_signal and len(screenshots) > 0 and bool(notify_decision.get("should_notify", True))
    normalized = {
        "status": "success" if safe_submitted else ("awaiting_review" if awaiting_review else (source_status or "upstream_failure")),
        "draft_status": draft_status,
        "source_status": source_status,
        "awaiting_review": awaiting_review and not safe_submitted,
        "review_status": "submitted" if safe_submitted else review_status,
        "submitted": safe_submitted,
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
            "channels": _as_text_list(notify_decision.get("channels")) if should_notify else [],
        },
        "account_created": bool(raw_result.get("account_created", False)),
        "safe_to_retry": bool(raw_result.get("safe_to_retry", source_status in {"timed_out", "navigation_failed"})),
        "inspect_only": inspect_only,
        "page_diagnostics": raw_result.get("page_diagnostics") if isinstance(raw_result.get("page_diagnostics"), dict) else {},
        "form_diagnostics": raw_result.get("form_diagnostics") if isinstance(raw_result.get("form_diagnostics"), dict) else {},
        "debug_json": debug_json,
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
    payload = dict(payload)
    if _payload_requests_host_run(payload) and not config.run_on_host:
        config = replace(config, run_on_host=True)
    config = _host_mode_normalized_config(config, logger)

    target = _require_dict(payload, "application_target")
    _require_dict(payload, "resume_variant")
    application_url = str(target.get("application_url") or target.get("source_url") or "").strip()
    if not application_url:
        return invalid_input_result(["missing_application_url"])
    inspect_only = bool(_as_bool(payload.get("inspect_only"), default=config.inspect_only))
    payload["inspect_only"] = inspect_only

    paths = build_artifact_paths(payload, config)
    auth_context, auth_warnings = _build_auth_context(logger, config)
    materialized_resume_path, resume_warnings = (None, []) if inspect_only else _materialize_resume_file(payload, paths, logger)
    resume_validation_error = _validate_resume_upload_path(
        payload,
        materialized_resume_path,
        allowed_extensions=config.allowed_resume_extensions,
        inspect_only=inspect_only,
    )
    if resume_validation_error is not None:
        return resume_validation_error

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
        "candidate_profile": payload.get("candidate_profile") if isinstance(payload.get("candidate_profile"), dict) else {},
        "contact_profile": payload.get("contact_profile") if isinstance(payload.get("contact_profile"), dict) else {},
        "default_answer_profile": payload.get("default_answer_profile") if isinstance(payload.get("default_answer_profile"), dict) else {},
        "constraints": {
            "stop_before_submit": True,
            "submit": False,
            "headless": config.headless,
            "timeout_seconds": config.timeout_seconds,
            "max_steps": config.max_steps,
            "allow_account_creation": False,
            "inspect_only": inspect_only,
            "skip_field_fills": inspect_only,
            "skip_resume_upload": inspect_only,
        },
        "auth": auth_context,
        "browser": {
            "run_on_host": config.run_on_host,
            "attach_mode": config.browser_attach_mode,
            "skip_browser_start": config.skip_browser_start,
            "allow_browser_start": config.allow_browser_start,
            "gateway_url": config.host_gateway_url if config.run_on_host else config.gateway_url,
            "cdp_url": config.host_cdp_url if config.run_on_host else config.cdp_url,
            "host_gateway_alias": config.host_gateway_alias,
        },
        "artifacts": {
            "run_key": paths.run_key,
            "receipt_path": str(paths.receipt_path.resolve()),
            "screenshot_dir": str(paths.screenshot_dir.resolve()),
            "resume_upload_path": materialized_resume_path,
            "progress_snapshot_path": str(paths.progress_snapshot_path.resolve()),
        },
        "lineage": payload.get("lineage") if isinstance(payload.get("lineage"), dict) else {},
        "inspect_only": inspect_only,
    }
    if inspect_only:
        request["application_answers"] = []
        request["cover_letter_text"] = ""
    request_summary = _sanitize_request_for_receipt(request, materialized_resume_path=materialized_resume_path)

    preflight_warnings = auth_warnings + resume_warnings
    if config.run_on_host and _running_in_docker():
        host_request = _host_visible_request(request)
        paths.host_handoff_request_path.write_text(json.dumps(host_request, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        host_request_path = _host_visible_path(paths.host_handoff_request_path)
        host_result_path = _host_visible_path(paths.host_handoff_result_path)
        raw_result = {
            "draft_status": "not_started",
            "source_status": "manual_review_required",
            "awaiting_review": False,
            "review_status": "blocked",
            "submitted": False,
            "failure_category": "manual_review_required",
            "blocking_reason": (
                "Host-run mode was requested, but this draft runner is executing inside Docker. "
                "Run the generated host command on the mini-PC host so the browser step can reach 127.0.0.1 OpenClaw endpoints."
            ),
            "fields_filled_manifest": [],
            "screenshot_metadata_references": [],
            "checkpoint_urls": [application_url],
            "page_title": None,
            "warnings": ["openclaw_host_handoff_required"],
            "errors": [],
            "notify_decision": {"should_notify": False, "reason": "manual_review_required", "channels": []},
            "account_created": False,
            "safe_to_retry": True,
            "debug_json": {
                "host_handoff": {
                    "request_path": host_request_path,
                    "result_path": host_result_path,
                    "runner_command": (
                        f"python3 scripts/openclaw_apply_draft.py --input-json-file {shlex.quote(host_request_path)} "
                        f"> {shlex.quote(host_result_path)}"
                    ),
                    "run_on_host": True,
                    "host_gateway_url": config.host_gateway_url,
                    "host_cdp_url": config.host_cdp_url,
                }
            },
        }
    else:
        try:
            raw_result = resolved_adapter.run(request)
        except subprocess.TimeoutExpired:
            progress_snapshot = _read_progress_snapshot(paths.progress_snapshot_path)
            progress_fields = (
                progress_snapshot.get("fields_filled_manifest")
                if isinstance(progress_snapshot.get("fields_filled_manifest"), list)
                else []
            )
            progress_screenshots = (
                progress_snapshot.get("screenshot_metadata_references")
                if isinstance(progress_snapshot.get("screenshot_metadata_references"), list)
                else []
            )
            progress_checkpoint_urls = (
                progress_snapshot.get("checkpoint_urls")
                if isinstance(progress_snapshot.get("checkpoint_urls"), list)
                else [application_url]
            )
            progress_page_diagnostics = (
                progress_snapshot.get("page_diagnostics")
                if isinstance(progress_snapshot.get("page_diagnostics"), dict)
                else {}
            )
            progress_form_diagnostics = (
                progress_snapshot.get("form_diagnostics")
                if isinstance(progress_snapshot.get("form_diagnostics"), dict)
                else {}
            )
            progress_warnings = _as_text_list(progress_snapshot.get("warnings"))
            progress_errors = _as_text_list(progress_snapshot.get("errors"))
            raw_result = {
                "draft_status": "partial_draft" if progress_fields else "not_started",
                "source_status": "timed_out",
                "awaiting_review": False,
                "review_status": "blocked",
                "submitted": False,
                "failure_category": "timed_out",
                "blocking_reason": _default_blocking_reason("timed_out"),
                "fields_filled_manifest": progress_fields,
                "screenshot_metadata_references": progress_screenshots,
                "checkpoint_urls": progress_checkpoint_urls,
                "page_title": str(progress_snapshot.get("page_title") or "").strip() or None,
                "warnings": _dedupe_text(progress_warnings + (["timeout_progress_snapshot_recovered"] if progress_snapshot else [])),
                "errors": _dedupe_text(progress_errors + ["openclaw_apply_timed_out"]),
                "notify_decision": {"should_notify": False, "reason": "timed_out", "channels": []},
                "account_created": False,
                "safe_to_retry": True,
                "page_diagnostics": progress_page_diagnostics,
                "form_diagnostics": progress_form_diagnostics,
                "debug_json": {
                    "adapter_timeout_seconds": config.timeout_seconds,
                    "progress_snapshot_path": str(paths.progress_snapshot_path.resolve()),
                    "draft_progress": progress_snapshot,
                },
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
                "debug_json": {"adapter_exception": type(exc).__name__},
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
