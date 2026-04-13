#!/usr/bin/env python3
"""Mission Control draft-only OpenClaw runner.

This script accepts Mission Control's structured apply-draft payload from either
stdin or `--input-json-file`, delegates the browser work to an OpenClaw adapter,
captures deterministic local artifacts, and always stops before final submit.

Example:
`python3 scripts/openclaw_apply_draft.py < payload.json`

Example with file input:
`python3 scripts/openclaw_apply_draft.py --input-json-file payload.json`
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations.openclaw_apply_runner import execute_apply_draft, invalid_input_result, read_payload


def _safe_auto_submit_signal(result: dict) -> bool:
    page_diagnostics = result.get("page_diagnostics") if isinstance(result.get("page_diagnostics"), dict) else {}
    form_diagnostics = result.get("form_diagnostics") if isinstance(result.get("form_diagnostics"), dict) else {}
    return bool(result.get("submitted")) and bool(
        (page_diagnostics.get("auto_submit_allowed") or form_diagnostics.get("auto_submit_allowed"))
        and (page_diagnostics.get("auto_submit_succeeded") or form_diagnostics.get("auto_submit_succeeded"))
    )


def _enforce_script_no_submit(result: dict) -> dict:
    if not bool(result.get("submitted")) or _safe_auto_submit_signal(result):
        return result
    guarded = dict(result)
    guarded["submitted"] = False
    guarded["awaiting_review"] = False
    guarded["review_status"] = "blocked"
    guarded["source_status"] = "unsafe_submit_attempted"
    guarded["failure_category"] = "unsafe_submit_attempted"
    guarded["blocking_reason"] = "Script-level no-submit guard blocked an adapter submit signal."
    warnings = list(guarded.get("warnings") or [])
    warnings.append("script_level_unsafe_submit_guard_triggered")
    guarded["warnings"] = warnings
    guarded["notify_decision"] = {
        "should_notify": False,
        "reason": "unsafe_submit_attempted",
        "channels": [],
    }
    return guarded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mission Control draft-only OpenClaw runner")
    parser.add_argument("--input-json-file", dest="input_json_file", help="Path to an input JSON payload file.")
    args = parser.parse_args(argv)

    try:
        payload = read_payload(args.input_json_file)
        result = execute_apply_draft(payload)
    except ValueError as exc:
        result = invalid_input_result([str(exc)])
    result = _enforce_script_no_submit(result)
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
