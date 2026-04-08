import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PATH = ROOT / "scripts" / "openclaw_apply_tool_bridge.py"


def _run_bridge(payload: dict, *, env: dict[str, str]) -> dict:
    completed = subprocess.run(
        [sys.executable, str(BRIDGE_PATH)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=str(ROOT),
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_bridge_returns_tool_unavailable_when_openclaw_missing(tmp_path) -> None:
    env = os.environ.copy()
    env["PATH"] = "/usr/bin:/bin"
    env.pop("OPENCLAW_APPLY_BROWSER_COMMAND", None)

    result = _run_bridge({}, env=env)

    assert result["failure_category"] == "tool_unavailable"
    assert result["source_status"] == "tool_unavailable"


def test_bridge_returns_tool_unavailable_when_backend_command_missing(tmp_path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    openclaw = fake_bin / "openclaw"
    openclaw.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    openclaw.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["OPENCLAW_APPLY_BROWSER_COMMAND"] = ""

    result = _run_bridge({}, env=env)

    assert result["failure_category"] == "tool_unavailable"
    assert "OPENCLAW_APPLY_BROWSER_COMMAND" in result["blocking_reason"]
