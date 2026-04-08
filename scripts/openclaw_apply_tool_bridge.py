#!/usr/bin/env python3
"""Bridge from Mission Control's stable tool command to a real OpenClaw backend command.

This script exists to keep Mission Control configuration stable even when the
underlying OpenClaw browser/apply entrypoint changes. It reads JSON from stdin
and forwards it to `OPENCLAW_APPLY_BROWSER_COMMAND` when configured.

Examples:
- `python3 scripts/openclaw_apply_tool_bridge.py < payload.json`
- `OPENCLAW_APPLY_BROWSER_COMMAND="openclaw <real-subcommand>" python3 scripts/openclaw_apply_tool_bridge.py < payload.json`
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def _result(
    *,
    draft_status: str = "not_started",
    source_status: str,
    failure_category: str,
    blocking_reason: str,
    warnings: list[str] | None = None,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "draft_status": draft_status,
        "source_status": source_status,
        "awaiting_review": False,
        "review_status": "blocked",
        "submitted": False,
        "failure_category": failure_category,
        "blocking_reason": blocking_reason,
        "fields_filled_manifest": [],
        "screenshot_metadata_references": [],
        "checkpoint_urls": [],
        "page_title": None,
        "warnings": warnings or [],
        "errors": errors or [],
        "notify_decision": {"should_notify": False, "reason": failure_category, "channels": []},
        "account_created": False,
        "safe_to_retry": False,
    }


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            json.dumps(
                _result(
                    source_status="invalid_input",
                    failure_category="invalid_input",
                    blocking_reason=f"Bridge received invalid JSON: {exc.msg}",
                    errors=[f"invalid_json:{exc.msg}"],
                ),
                ensure_ascii=True,
            )
        )
        return 0
    if not isinstance(payload, dict):
        print(
            json.dumps(
                _result(
                    source_status="invalid_input",
                    failure_category="invalid_input",
                    blocking_reason="Bridge expected a JSON object payload.",
                    errors=["payload_must_be_object"],
                ),
                ensure_ascii=True,
            )
        )
        return 0

    browser_command_raw = str(os.getenv("OPENCLAW_APPLY_BROWSER_COMMAND") or "").strip()
    mounted_openclaw_path = Path("/opt/openclaw/npm-global/bin/openclaw")
    openclaw_path = shutil.which("openclaw")
    if not openclaw_path and mounted_openclaw_path.exists():
        openclaw_path = str(mounted_openclaw_path)
    if not openclaw_path:
        print(
            json.dumps(
                _result(
                    source_status="tool_unavailable",
                    failure_category="tool_unavailable",
                    blocking_reason="OpenClaw CLI is not available inside the worker container.",
                    warnings=["openclaw_cli_not_found_in_container"],
                ),
                ensure_ascii=True,
            )
        )
        return 0
    if not browser_command_raw:
        print(
            json.dumps(
                _result(
                    source_status="tool_unavailable",
                    failure_category="tool_unavailable",
                    blocking_reason=(
                        "OpenClaw CLI is installed, but OPENCLAW_APPLY_BROWSER_COMMAND is not configured with "
                        "the real browser apply command."
                    ),
                    warnings=["openclaw_apply_browser_command_not_configured"],
                ),
                ensure_ascii=True,
            )
        )
        return 0

    command = [part for part in shlex.split(browser_command_raw) if part.strip()]
    if not command:
        print(
            json.dumps(
                _result(
                    source_status="tool_unavailable",
                    failure_category="tool_unavailable",
                    blocking_reason="OPENCLAW_APPLY_BROWSER_COMMAND is empty after parsing.",
                    warnings=["openclaw_apply_browser_command_empty"],
                ),
                ensure_ascii=True,
            )
        )
        return 0

    completed = subprocess.run(
        command,
        input=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        print(
            json.dumps(
                _result(
                    source_status="navigation_failed",
                    failure_category="navigation_failed",
                    blocking_reason=f"OpenClaw browser command exited with code {completed.returncode}.",
                    errors=[f"openclaw_apply_browser_command_failed_exit_{completed.returncode}"],
                ),
                ensure_ascii=True,
            )
        )
        return 0

    stdout = completed.stdout.decode("utf-8", errors="replace").strip()
    if not stdout:
        print(
            json.dumps(
                _result(
                    source_status="navigation_failed",
                    failure_category="navigation_failed",
                    blocking_reason="OpenClaw browser command returned no output.",
                    errors=["openclaw_apply_browser_command_empty_output"],
                ),
                ensure_ascii=True,
            )
        )
        return 0

    print(stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
