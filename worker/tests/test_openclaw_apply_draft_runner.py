import json
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "worker"))

from integrations.openclaw_apply_runner import RunnerConfig, _materialize_resume_file, build_artifact_paths, execute_apply_draft


SCRIPT_PATH = ROOT / "scripts" / "openclaw_apply_draft.py"


def _base_payload() -> dict:
    return {
        "action": "apply_draft",
        "inspect_only": False,
        "application_target": {
            "job_id": "job-1",
            "title": "Senior ML Engineer",
            "company": "Acme AI",
            "source": "linkedin",
            "source_url": "https://linkedin.example/jobs/1",
            "application_url": "https://linkedin.example/jobs/view/1",
        },
        "resume_variant": {
            "resume_variant_name": "Tailored Resume - Acme AI",
            "resume_variant_text": "Tailored resume body",
            "resume_file_name": "tailored_resume_acme_ai.txt",
        },
        "application_answers": [
            {"question": "Why are you interested?", "answer": "Strong fit.", "answer_type": "motivation"}
        ],
        "cover_letter_text": "Dear Hiring Team, ...",
        "capture_screenshots": True,
        "max_screenshots": 4,
        "lineage": {
            "pipeline_id": "pipe-apply-1",
            "task_id": "task-draft-1",
            "run_id": "run-draft-1",
        },
    }


def _isolated_runner_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("OPENCLAW_APPLY_") or key.startswith("OPENCLAW_BROWSER_") or key.startswith("FAKE_OPENCLAW_"):
            env.pop(key, None)
    return env


def _write_fake_openclaw_tool(tmp_path: Path) -> Path:
    tool_path = tmp_path / "fake_openclaw_tool.py"
    tool_path.write_text(
        textwrap.dedent(
            """
            import json
            import os
            import sys
            from pathlib import Path

            request = json.load(sys.stdin)
            response = json.loads(os.environ["FAKE_OPENCLAW_RESPONSE"])
            if os.environ.get("FAKE_OPENCLAW_WRITE_SCREENSHOT") == "1":
                screenshot_dir = Path(request["artifacts"]["screenshot_dir"])
                screenshot_dir.mkdir(parents=True, exist_ok=True)
                screenshot_path = screenshot_dir / "checkpoint-application-form.png"
                screenshot_path.write_bytes(b"PNG")
                response.setdefault(
                    "screenshot_metadata_references",
                    [
                        {
                            "label": "application-form",
                            "path": str(screenshot_path),
                            "kind": "checkpoint",
                            "captured_at": "2026-03-31T00:00:00Z",
                        }
                    ],
                )
            if response.pop("_echo_resume_upload", False):
                resume_upload_path = request.get("artifacts", {}).get("resume_upload_path")
                response.setdefault("fields_filled_manifest", []).append(
                    {
                        "field_name": "resume_upload",
                        "status": "uploaded",
                        "value_redacted": True,
                        "value_preview": Path(resume_upload_path).name if resume_upload_path else None,
                    }
                )
            print(json.dumps(response, ensure_ascii=True))
            """
        ),
        encoding="utf-8",
    )
    return tool_path


def _write_fake_openclaw_browser_cli(tmp_path: Path) -> Path:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    cli_path = fake_bin / "openclaw"
    cli_path.write_text(
        (
            "#!/usr/bin/env python3\n"
            "import json\n"
            "import sys\n"
            "import tempfile\n"
            "from pathlib import Path\n"
            "\n"
            "args = sys.argv[1:]\n"
            "if not args or args[0] != 'browser':\n"
            "    print('Missing browser subcommand.', file=sys.stderr)\n"
            "    raise SystemExit(2)\n"
            "\n"
            "index = 1\n"
            "while index < len(args) and args[index].startswith('--'):\n"
            "    flag = args[index]\n"
            "    if '=' in flag:\n"
            "        index += 1\n"
            "        continue\n"
            "    index += 2\n"
            "\n"
            "if index >= len(args):\n"
            "    print('Missing subcommand. Try: \"openclaw browser status\"', file=sys.stderr)\n"
            "    raise SystemExit(2)\n"
            "\n"
            "subcommand = args[index]\n"
            "remaining = args[index + 1 :]\n"
            "\n"
            "if subcommand == 'status':\n"
            "    print('OK')\n"
            "    raise SystemExit(0)\n"
            "if subcommand == 'tabs':\n"
            "    print('[]')\n"
            "    raise SystemExit(0)\n"
            "if subcommand in {'open', 'wait', 'upload', 'fill'}:\n"
            "    raise SystemExit(0)\n"
            "if subcommand == 'evaluate':\n"
            "    fn_index = remaining.index('--fn') + 1\n"
            "    fn_source = remaining[fn_index]\n"
            "    if 'document.title' in fn_source:\n"
            "        print(json.dumps('Apply - Senior ML Engineer'))\n"
            "    elif 'window.location.href' in fn_source:\n"
            "        print(json.dumps('https://linkedin.example/jobs/view/1'))\n"
            "    else:\n"
            "        print('null')\n"
            "    raise SystemExit(0)\n"
            "if subcommand == 'snapshot':\n"
            "    out_index = remaining.index('--out') + 1\n"
            "    snapshot_path = Path(remaining[out_index])\n"
            "    snapshot_path.write_text('[10] input \"Resume upload\"\\n[20] textarea \"Cover letter\"\\n[21] textarea \"Why are you interested?\"\\n[99] button \"Submit application\"', encoding='utf-8')\n"
            "    raise SystemExit(0)\n"
            "if subcommand == 'screenshot':\n"
            "    with tempfile.NamedTemporaryFile(prefix='fake-openclaw-shot-', suffix='.png', delete=False) as handle:\n"
            "        Path(handle.name).write_bytes(b'PNG')\n"
            "        print(f'MEDIA:{handle.name}')\n"
            "    raise SystemExit(0)\n"
            "\n"
            "print(f'Unexpected subcommand: {subcommand}', file=sys.stderr)\n"
            "raise SystemExit(3)\n"
        ),
        encoding="utf-8",
    )
    cli_path.chmod(0o755)
    return cli_path


def _run_script(payload: dict, *, env: dict[str, str], input_via_file: bool) -> dict:
    command = [sys.executable, str(SCRIPT_PATH)]
    input_text = None
    if input_via_file:
        payload_path = Path(env["TEST_TMPDIR"]) / "payload.json"
        payload_path.write_text(json.dumps(payload), encoding="utf-8")
        command.extend(["--input-json-file", str(payload_path)])
    else:
        input_text = json.dumps(payload)

    completed = subprocess.run(
        command,
        input=input_text,
        text=True,
        capture_output=True,
        cwd=str(ROOT),
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_openclaw_apply_draft_runner_success_path_with_command_adapter(tmp_path) -> None:
    payload = _base_payload()
    fake_tool = _write_fake_openclaw_tool(tmp_path)
    env = _isolated_runner_env()
    env.update(
        {
            "OPENCLAW_APPLY_ADAPTER": "command",
            "OPENCLAW_APPLY_TOOL_COMMAND": f"{sys.executable} {fake_tool}",
            "OPENCLAW_APPLY_SCREENSHOT_DIR": str(tmp_path / "screenshots"),
            "OPENCLAW_APPLY_RECEIPT_DIR": str(tmp_path / "receipts"),
            "OPENCLAW_APPLY_RESUME_DIR": str(tmp_path / "resume_uploads"),
            "OPENCLAW_APPLY_HEADLESS": "true",
            "OPENCLAW_APPLY_TIMEOUT_SECONDS": "30",
            "OPENCLAW_APPLY_MAX_STEPS": "12",
            "FAKE_OPENCLAW_WRITE_SCREENSHOT": "1",
            "FAKE_OPENCLAW_RESPONSE": json.dumps(
                {
                    "draft_status": "draft_ready",
                    "source_status": "success",
                    "awaiting_review": True,
                    "review_status": "awaiting_review",
                    "submitted": False,
                    "failure_category": None,
                    "blocking_reason": None,
                    "fields_filled_manifest": [
                        {"field_name": "first_name", "status": "filled", "value_redacted": True},
                        {"field_name": "motivation_answer", "status": "answered", "value_redacted": True},
                    ],
                    "_echo_resume_upload": True,
                    "checkpoint_urls": ["https://linkedin.example/jobs/view/1"],
                    "page_title": "Apply - Senior ML Engineer",
                    "warnings": [],
                    "errors": [],
                    "notify_decision": {
                        "should_notify": True,
                        "reason": "draft_ready_for_review",
                        "channels": ["discord"],
                    },
                    "debug_json": {
                        "browser_runtime": {"attach_mode": True, "attach_probe_succeeded": True},
                        "openclaw_commands": [{"stage": "probe_status", "exit_code": 0, "stdout": "OK", "stderr": ""}],
                    },
                }
            ),
            "TEST_TMPDIR": str(tmp_path),
        }
    )

    result = _run_script(payload, env=env, input_via_file=True)

    assert result["draft_status"] == "draft_ready"
    assert result["source_status"] == "success"
    assert result["awaiting_review"] is True
    assert result["review_status"] == "awaiting_review"
    assert result["submitted"] is False
    assert result["notify_decision"]["should_notify"] is True
    assert len(result["fields_filled_manifest"]) == 3
    assert len(result["screenshot_metadata_references"]) == 1
    assert result["debug_json"]["adapter_command"]["exit_code"] == 0
    assert result["debug_json"]["browser_runtime"]["attach_mode"] is True
    screenshot_path = Path(result["screenshot_metadata_references"][0]["path"])
    assert screenshot_path.exists()
    assert screenshot_path.parent.name == "pipe-apply-1-task-draft-1-run-draft-1-job-1-acme-ai-senior-ml-engineer"


def test_openclaw_apply_draft_runner_returns_tool_unavailable_when_command_adapter_is_unconfigured(tmp_path) -> None:
    payload = _base_payload()
    env = _isolated_runner_env()
    env.update(
        {
            "OPENCLAW_APPLY_ADAPTER": "command",
            "OPENCLAW_APPLY_SCREENSHOT_DIR": str(tmp_path / "screenshots"),
            "OPENCLAW_APPLY_RECEIPT_DIR": str(tmp_path / "receipts"),
            "OPENCLAW_APPLY_RESUME_DIR": str(tmp_path / "resume_uploads"),
            "TEST_TMPDIR": str(tmp_path),
        }
    )
    env.pop("OPENCLAW_APPLY_TOOL_COMMAND", None)
    env.pop("OPENCLAW_APPLY_PYTHON_ENTRYPOINT", None)

    result = _run_script(payload, env=env, input_via_file=False)

    assert result["draft_status"] == "not_started"
    assert result["awaiting_review"] is False
    assert result["failure_category"] == "tool_unavailable"
    assert result["notify_decision"]["should_notify"] is False


def test_openclaw_apply_draft_runner_returns_login_required_when_session_is_missing(tmp_path) -> None:
    payload = _base_payload()
    fake_tool = _write_fake_openclaw_tool(tmp_path)
    env = _isolated_runner_env()
    env.update(
        {
            "OPENCLAW_APPLY_ADAPTER": "command",
            "OPENCLAW_APPLY_TOOL_COMMAND": f"{sys.executable} {fake_tool}",
            "OPENCLAW_APPLY_SCREENSHOT_DIR": str(tmp_path / "screenshots"),
            "OPENCLAW_APPLY_RECEIPT_DIR": str(tmp_path / "receipts"),
            "OPENCLAW_APPLY_RESUME_DIR": str(tmp_path / "resume_uploads"),
            "FAKE_OPENCLAW_RESPONSE": json.dumps(
                {
                    "draft_status": "not_started",
                    "source_status": "login_required",
                    "awaiting_review": False,
                    "review_status": "blocked",
                    "submitted": False,
                    "failure_category": "login_required",
                    "blocking_reason": "Login wall detected.",
                    "fields_filled_manifest": [],
                    "checkpoint_urls": ["https://linkedin.example/jobs/view/1"],
                    "warnings": [],
                    "errors": [],
                    "notify_decision": {"should_notify": False, "reason": "login_required", "channels": []},
                }
            ),
            "TEST_TMPDIR": str(tmp_path),
        }
    )

    result = _run_script(payload, env=env, input_via_file=False)

    assert result["source_status"] == "login_required"
    assert result["failure_category"] == "login_required"
    assert result["awaiting_review"] is False
    assert result["submitted"] is False


def test_openclaw_apply_draft_runner_enforces_no_submit_safety(tmp_path) -> None:
    payload = _base_payload()
    fake_tool = _write_fake_openclaw_tool(tmp_path)
    env = _isolated_runner_env()
    env.update(
        {
            "OPENCLAW_APPLY_ADAPTER": "command",
            "OPENCLAW_APPLY_TOOL_COMMAND": f"{sys.executable} {fake_tool}",
            "OPENCLAW_APPLY_SCREENSHOT_DIR": str(tmp_path / "screenshots"),
            "OPENCLAW_APPLY_RECEIPT_DIR": str(tmp_path / "receipts"),
            "OPENCLAW_APPLY_RESUME_DIR": str(tmp_path / "resume_uploads"),
            "FAKE_OPENCLAW_WRITE_SCREENSHOT": "1",
            "FAKE_OPENCLAW_RESPONSE": json.dumps(
                {
                    "draft_status": "partial_draft",
                    "source_status": "success",
                    "awaiting_review": True,
                    "review_status": "awaiting_review",
                    "submitted": True,
                    "fields_filled_manifest": [
                        {"field_name": "first_name", "status": "filled", "value_redacted": True}
                    ],
                    "warnings": [],
                    "errors": [],
                }
            ),
            "TEST_TMPDIR": str(tmp_path),
        }
    )

    result = _run_script(payload, env=env, input_via_file=False)

    assert result["submitted"] is False
    assert result["failure_category"] == "unsafe_submit_attempted"
    assert "unsafe_submit_attempted_detected" in result["warnings"]
    assert result["awaiting_review"] is False


def test_openclaw_apply_draft_runner_allows_safe_auto_submit_signal(tmp_path) -> None:
    payload = _base_payload()
    fake_tool = _write_fake_openclaw_tool(tmp_path)
    env = _isolated_runner_env()
    env.update(
        {
            "OPENCLAW_APPLY_ADAPTER": "command",
            "OPENCLAW_APPLY_TOOL_COMMAND": f"{sys.executable} {fake_tool}",
            "OPENCLAW_APPLY_SCREENSHOT_DIR": str(tmp_path / "screenshots"),
            "OPENCLAW_APPLY_RECEIPT_DIR": str(tmp_path / "receipts"),
            "OPENCLAW_APPLY_RESUME_DIR": str(tmp_path / "resume_uploads"),
            "FAKE_OPENCLAW_WRITE_SCREENSHOT": "1",
            "FAKE_OPENCLAW_RESPONSE": json.dumps(
                {
                    "draft_status": "draft_ready",
                    "source_status": "success",
                    "awaiting_review": False,
                    "review_status": "submitted",
                    "submitted": True,
                    "fields_filled_manifest": [
                        {"field_name": "first_name", "status": "filled", "value_redacted": True}
                    ],
                    "checkpoint_urls": ["https://linkedin.example/jobs/view/1"],
                    "page_title": "Application submitted",
                    "warnings": [],
                    "errors": [],
                    "page_diagnostics": {
                        "auto_submit_allowed": True,
                        "auto_submit_attempted": True,
                        "auto_submit_succeeded": True,
                        "submit_confidence": "high",
                    },
                    "form_diagnostics": {
                        "fallback_answers_used": [{"label": "Veteran status"}],
                    },
                }
            ),
            "TEST_TMPDIR": str(tmp_path),
        }
    )

    result = _run_script(payload, env=env, input_via_file=False)

    assert result["submitted"] is True
    assert result["failure_category"] is None
    assert result["source_status"] == "success"
    assert result["review_status"] == "submitted"
    assert result["awaiting_review"] is False
    assert result["notify_decision"]["should_notify"] is False


def test_openclaw_apply_draft_runner_blocks_incomplete_review_package(tmp_path) -> None:
    payload = _base_payload()
    fake_tool = _write_fake_openclaw_tool(tmp_path)
    env = _isolated_runner_env()
    env.update(
        {
            "OPENCLAW_APPLY_ADAPTER": "command",
            "OPENCLAW_APPLY_TOOL_COMMAND": f"{sys.executable} {fake_tool}",
            "OPENCLAW_APPLY_SCREENSHOT_DIR": str(tmp_path / "screenshots"),
            "OPENCLAW_APPLY_RECEIPT_DIR": str(tmp_path / "receipts"),
            "OPENCLAW_APPLY_RESUME_DIR": str(tmp_path / "resume_uploads"),
            "FAKE_OPENCLAW_RESPONSE": json.dumps(
                {
                    "draft_status": "draft_ready",
                    "source_status": "success",
                    "awaiting_review": True,
                    "review_status": "awaiting_review",
                    "submitted": False,
                    "fields_filled_manifest": [
                        {"field_name": "first_name", "status": "filled", "value_redacted": True}
                    ],
                    "checkpoint_urls": ["https://linkedin.example/jobs/view/1"],
                    "warnings": [],
                    "errors": [],
                    "notify_decision": {"should_notify": True, "reason": "draft_ready_for_review", "channels": ["discord"]},
                }
            ),
            "TEST_TMPDIR": str(tmp_path),
        }
    )

    result = _run_script(payload, env=env, input_via_file=False)

    assert result["draft_status"] == "partial_draft"
    assert result["awaiting_review"] is False
    assert result["failure_category"] == "manual_review_required"
    assert result["notify_decision"]["should_notify"] is False


def test_openclaw_apply_draft_runner_generates_screenshot_metadata_paths(tmp_path) -> None:
    payload = _base_payload()

    class FakeAdapter:
        def run(self, request: dict) -> dict:
            screenshot_dir = Path(request["artifacts"]["screenshot_dir"])
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            relative_path = screenshot_dir / "checkpoint-relative.png"
            relative_path.write_bytes(b"PNG")
            return {
                "draft_status": "partial_draft",
                "source_status": "manual_review_required",
                "awaiting_review": True,
                "review_status": "awaiting_review",
                "submitted": False,
                "fields_filled_manifest": [
                    {"field_name": "resume_upload", "status": "uploaded", "value_redacted": True}
                ],
                "screenshot_metadata_references": [
                    {
                        "label": "relative-shot",
                        "path": "checkpoint-relative.png",
                        "kind": "checkpoint",
                        "captured_at": "2026-03-31T00:00:00Z",
                    }
                ],
                "checkpoint_urls": ["https://linkedin.example/jobs/view/1"],
                "warnings": [],
                "errors": [],
            }

    config = RunnerConfig(
        adapter="auto",
        tool_command=None,
        python_entrypoint=None,
        headless=True,
        screenshot_root=tmp_path / "screenshots",
        receipt_root=tmp_path / "receipts",
        resume_root=tmp_path / "resume_uploads",
        timeout_seconds=30,
        max_steps=12,
        log_level="INFO",
        inspect_only=False,
        allowed_resume_extensions=(".pdf", ".doc", ".docx", ".txt", ".rtf"),
        auth_strategy=None,
        storage_state_path=None,
        browser_profile_path=None,
        browser_attach_mode=False,
        skip_browser_start=False,
        allow_browser_start=True,
        gateway_url=None,
        cdp_url=None,
        host_gateway_alias=None,
        run_on_host=False,
        host_gateway_url="ws://127.0.0.1:18789",
        host_cdp_url="http://127.0.0.1:18800",
    )

    result = execute_apply_draft(payload, config=config, adapter=FakeAdapter())

    assert result["awaiting_review"] is True
    assert len(result["screenshot_metadata_references"]) == 1
    screenshot_ref = result["screenshot_metadata_references"][0]
    assert screenshot_ref["path"] == str(
        (
            tmp_path
            / "screenshots"
            / "pipe-apply-1-task-draft-1-run-draft-1-job-1-acme-ai-senior-ml-engineer"
            / "checkpoint-relative.png"
        ).resolve()
    )
    assert Path(screenshot_ref["path"]).exists()


def test_openclaw_apply_draft_runner_supports_inspect_only_mode(tmp_path) -> None:
    payload = _base_payload()
    payload["inspect_only"] = True
    captured_request: dict = {}

    class FakeAdapter:
        def run(self, request: dict) -> dict:
            captured_request.update(request)
            screenshot_dir = Path(request["artifacts"]["screenshot_dir"])
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            screenshot_path = screenshot_dir / "inspect-only.png"
            screenshot_path.write_bytes(b"PNG")
            return {
                "draft_status": "inspect_only",
                "source_status": "success",
                "awaiting_review": False,
                "review_status": "inspect_only",
                "submitted": False,
                "screenshot_metadata_references": [
                    {"label": "inspect-only", "path": str(screenshot_path), "kind": "checkpoint"}
                ],
                "checkpoint_urls": ["https://linkedin.example/jobs/view/1"],
                "page_diagnostics": {"form_count": 2},
                "form_diagnostics": {"supported": False},
                "warnings": [],
                "errors": [],
            }

    config = RunnerConfig(
        adapter="auto",
        tool_command=None,
        python_entrypoint=None,
        headless=True,
        screenshot_root=tmp_path / "screenshots",
        receipt_root=tmp_path / "receipts",
        resume_root=tmp_path / "resume_uploads",
        timeout_seconds=30,
        max_steps=12,
        log_level="INFO",
        inspect_only=True,
        allowed_resume_extensions=(".pdf", ".doc", ".docx", ".txt", ".rtf"),
        auth_strategy=None,
        storage_state_path=None,
        browser_profile_path=None,
        browser_attach_mode=False,
        skip_browser_start=False,
        allow_browser_start=True,
        gateway_url=None,
        cdp_url=None,
        host_gateway_alias=None,
        run_on_host=False,
        host_gateway_url="ws://127.0.0.1:18789",
        host_cdp_url="http://127.0.0.1:18800",
    )

    result = execute_apply_draft(payload, config=config, adapter=FakeAdapter())

    assert captured_request["inspect_only"] is True
    assert captured_request["constraints"]["skip_field_fills"] is True
    assert captured_request["application_answers"] == []
    assert result["draft_status"] == "inspect_only"
    assert result["awaiting_review"] is False
    assert result["notify_decision"]["should_notify"] is False


def test_openclaw_apply_draft_runner_recovers_progress_snapshot_on_timeout(tmp_path) -> None:
    payload = _base_payload()

    class TimeoutAdapter:
        def run(self, request: dict) -> dict:
            progress_path = Path(request["artifacts"]["progress_snapshot_path"])
            progress_path.parent.mkdir(parents=True, exist_ok=True)
            progress_path.write_text(
                json.dumps(
                    {
                        "progress_stage": "later_step_diagnostics",
                        "page_title": "Apply to Acme AI",
                        "checkpoint_urls": ["https://linkedin.example/jobs/view/1"],
                        "page_diagnostics": {
                            "review_step_detected": True,
                            "submit_button_present": False,
                            "later_step_decision": "continue_flow",
                            "last_step_signature": "review-your-application-2",
                        },
                        "form_diagnostics": {
                            "submit_button_present": False,
                            "later_step_decision": "continue_flow",
                        },
                    }
                ),
                encoding="utf-8",
            )
            raise subprocess.TimeoutExpired(cmd=["fake-openclaw"], timeout=30)

    config = RunnerConfig(
        adapter="auto",
        tool_command=None,
        python_entrypoint=None,
        headless=True,
        screenshot_root=tmp_path / "screenshots",
        receipt_root=tmp_path / "receipts",
        resume_root=tmp_path / "resume_uploads",
        timeout_seconds=30,
        max_steps=12,
        log_level="INFO",
        inspect_only=False,
        allowed_resume_extensions=(".pdf", ".doc", ".docx", ".txt", ".rtf"),
        auth_strategy=None,
        storage_state_path=None,
        browser_profile_path=None,
        browser_attach_mode=False,
        skip_browser_start=False,
        allow_browser_start=True,
        gateway_url=None,
        cdp_url=None,
        host_gateway_alias=None,
        run_on_host=False,
        host_gateway_url="ws://127.0.0.1:18789",
        host_cdp_url="http://127.0.0.1:18800",
    )

    result = execute_apply_draft(payload, config=config, adapter=TimeoutAdapter())

    assert result["source_status"] == "timed_out"
    assert result["failure_category"] == "timed_out"
    assert result["page_diagnostics"]["review_step_detected"] is True
    assert result["page_diagnostics"]["last_step_signature"] == "review-your-application-2"
    assert result["form_diagnostics"]["later_step_decision"] == "continue_flow"
    assert "timeout_progress_snapshot_recovered" in result["warnings"]
    assert result["debug_json"]["draft_progress"]["progress_stage"] == "later_step_diagnostics"


def test_openclaw_apply_draft_runner_host_mode_uses_host_local_browser_urls(tmp_path) -> None:
    payload = _base_payload()
    captured_request: dict = {}

    class FakeAdapter:
        def run(self, request: dict) -> dict:
            captured_request.update(request)
            return {
                "draft_status": "not_started",
                "source_status": "manual_review_required",
                "awaiting_review": False,
                "review_status": "blocked",
                "submitted": False,
                "checkpoint_urls": [request["application_target"]["application_url"]],
                "warnings": [],
                "errors": [],
            }

    config = RunnerConfig(
        adapter="auto",
        tool_command=None,
        python_entrypoint=None,
        headless=True,
        screenshot_root=tmp_path / "screenshots",
        receipt_root=tmp_path / "receipts",
        resume_root=tmp_path / "resume_uploads",
        timeout_seconds=30,
        max_steps=12,
        log_level="INFO",
        inspect_only=False,
        allowed_resume_extensions=(".pdf", ".doc", ".docx", ".txt", ".rtf"),
        auth_strategy=None,
        storage_state_path=None,
        browser_profile_path=None,
        browser_attach_mode=True,
        skip_browser_start=True,
        allow_browser_start=False,
        gateway_url="ws://host.docker.internal:18789",
        cdp_url="http://host.docker.internal:18800",
        host_gateway_alias="host.docker.internal",
        run_on_host=True,
        host_gateway_url="ws://127.0.0.1:18789",
        host_cdp_url="http://127.0.0.1:18800",
    )

    execute_apply_draft(payload, config=config, adapter=FakeAdapter())

    assert captured_request["browser"]["run_on_host"] is True
    assert captured_request["browser"]["gateway_url"] == "ws://127.0.0.1:18789"
    assert captured_request["browser"]["cdp_url"] == "http://127.0.0.1:18800"


def test_openclaw_apply_draft_runner_host_mode_in_docker_emits_handoff(tmp_path, monkeypatch) -> None:
    payload = _base_payload()
    monkeypatch.setattr("integrations.openclaw_apply_runner._running_in_docker", lambda: True)

    class FakeAdapter:
        def run(self, request: dict) -> dict:
            raise AssertionError("adapter should not be invoked for host handoff")

    config = RunnerConfig(
        adapter="auto",
        tool_command=None,
        python_entrypoint=None,
        headless=True,
        screenshot_root=tmp_path / "screenshots",
        receipt_root=tmp_path / "receipts",
        resume_root=tmp_path / "resume_uploads",
        timeout_seconds=30,
        max_steps=12,
        log_level="INFO",
        inspect_only=False,
        allowed_resume_extensions=(".pdf", ".doc", ".docx", ".txt", ".rtf"),
        auth_strategy=None,
        storage_state_path=None,
        browser_profile_path=None,
        browser_attach_mode=True,
        skip_browser_start=True,
        allow_browser_start=False,
        gateway_url="ws://host.docker.internal:18789",
        cdp_url="http://host.docker.internal:18800",
        host_gateway_alias="host.docker.internal",
        run_on_host=True,
        host_gateway_url="ws://127.0.0.1:18789",
        host_cdp_url="http://127.0.0.1:18800",
    )

    result = execute_apply_draft(payload, config=config, adapter=FakeAdapter())

    assert result["failure_category"] == "manual_review_required"
    assert "host_handoff" in result["debug_json"]
    assert Path(tmp_path / "receipts" / "host_handoff").exists()
    request_path = Path(result["debug_json"]["host_handoff"]["request_path"])
    assert request_path.name.endswith(".input.json")
    assert "python3 scripts/openclaw_apply_draft.py --input-json-file" in result["debug_json"]["host_handoff"]["runner_command"]
    handoff_request = json.loads(request_path.read_text(encoding="utf-8"))
    assert handoff_request["resume_variant"]["resume_upload_path"] == str(
        (tmp_path / "resume_uploads" / "pipe-apply-1-task-draft-1-run-draft-1-job-1-acme-ai-senior-ml-engineer.txt").resolve()
    )
    assert handoff_request["artifacts"]["resume_upload_path"] == handoff_request["resume_variant"]["resume_upload_path"]


def test_host_handoff_script_normalizes_base_tool_command_and_appends_browser_subcommands(tmp_path) -> None:
    payload = _base_payload()
    payload["browser"] = {
        "run_on_host": True,
        "attach_mode": True,
        "skip_browser_start": True,
        "allow_browser_start": False,
        "gateway_url": "ws://127.0.0.1:18789",
        "cdp_url": "http://127.0.0.1:18800",
    }
    fake_cli = _write_fake_openclaw_browser_cli(tmp_path)
    env = _isolated_runner_env()
    env.update(
        {
            "PATH": f"{fake_cli.parent}:{env.get('PATH', '')}",
            "OPENCLAW_APPLY_ADAPTER": "command",
            "OPENCLAW_APPLY_BROWSER_ATTACH_MODE": "true",
            "OPENCLAW_APPLY_SKIP_BROWSER_START": "true",
            "OPENCLAW_APPLY_ALLOW_BROWSER_START": "false",
            "OPENCLAW_APPLY_TOOL_COMMAND": "openclaw browser --url ws://127.0.0.1:18789 --token test-token --browser-profile openclaw",
            "OPENCLAW_APPLY_BROWSER_COMMAND": "python /app/scripts/openclaw_apply_browser_backend.py",
            "OPENCLAW_BROWSER_BASE_COMMAND": "openclaw browser --url ws://stale.example:9999 --token stale-token --browser-profile stale-profile",
            "OPENCLAW_APPLY_GATEWAY_URL": "ws://127.0.0.1:18789",
            "OPENCLAW_APPLY_CDP_URL": "http://127.0.0.1:18800",
            "OPENCLAW_APPLY_HOST_GATEWAY_URL": "ws://127.0.0.1:18789",
            "OPENCLAW_APPLY_HOST_CDP_URL": "http://127.0.0.1:18800",
            "OPENCLAW_APPLY_SCREENSHOT_DIR": str(tmp_path / "screenshots"),
            "OPENCLAW_APPLY_RECEIPT_DIR": str(tmp_path / "receipts"),
            "OPENCLAW_APPLY_RESUME_DIR": str(tmp_path / "resume_uploads"),
            "TEST_TMPDIR": str(tmp_path),
        }
    )

    result = _run_script(payload, env=env, input_via_file=True)

    assert result["draft_status"] == "draft_ready"
    assert Path(result["screenshot_metadata_references"][0]["path"]).exists()
    adapter_command = result["debug_json"]["adapter_command"]["command"]
    assert adapter_command[0] == sys.executable
    assert adapter_command[-1].endswith("scripts/openclaw_apply_tool_bridge.py")
    commands = result["debug_json"]["openclaw_commands"]
    assert [row["stage"] for row in commands[:4]] == ["probe_status", "probe_tabs", "navigate_open", "wait_domcontentloaded"]
    assert commands[0]["command"][-1] == "status"
    assert commands[1]["command"][-1] == "tabs"
    assert commands[2]["command"][-2] == "open"
    assert all("Missing subcommand" not in row.get("stderr", "") for row in commands)
    assert commands[0]["command"][:8] == [
        "openclaw",
        "browser",
        "--url",
        "ws://127.0.0.1:18789",
        "--token",
        "<redacted>",
        "--browser-profile",
        "openclaw",
    ]


def test_materialize_resume_file_normalizes_container_resume_path_to_host_repo_path(tmp_path, monkeypatch) -> None:
    host_repo_root = tmp_path / "repo"
    host_repo_root.mkdir(parents=True, exist_ok=True)
    host_resume_path = host_repo_root / "data" / "openclaw_apply_drafts" / "resume_uploads" / "resume.txt"
    host_resume_path.parent.mkdir(parents=True, exist_ok=True)
    host_resume_path.write_text("Tailored resume", encoding="utf-8")

    import integrations.openclaw_apply_runner as runner_module

    monkeypatch.setattr(runner_module, "ROOT", host_repo_root)
    paths = build_artifact_paths(
        {
            "application_target": {"job_id": "job-1", "company": "Acme AI", "title": "Senior ML Engineer"},
            "resume_variant": {"resume_file_name": "resume.txt"},
            "lineage": {"pipeline_id": "pipe-apply-1", "task_id": "task-draft-1", "run_id": "run-draft-1"},
        },
        RunnerConfig(
            adapter="command",
            tool_command="python fake_tool.py",
            python_entrypoint=None,
            headless=True,
            screenshot_root=tmp_path / "screenshots",
            receipt_root=tmp_path / "receipts",
            resume_root=tmp_path / "resume_uploads",
            timeout_seconds=30,
            max_steps=12,
            log_level="INFO",
            inspect_only=False,
            allowed_resume_extensions=(".txt",),
            auth_strategy=None,
            storage_state_path=None,
            browser_profile_path=None,
            browser_attach_mode=False,
            skip_browser_start=False,
            allow_browser_start=True,
            gateway_url=None,
            cdp_url=None,
            host_gateway_alias=None,
            run_on_host=True,
            host_gateway_url="ws://127.0.0.1:18789",
            host_cdp_url="http://127.0.0.1:18800",
        ),
    )

    materialized_path, warnings = _materialize_resume_file(
        {
            "resume_variant": {
                "resume_upload_path": "/app/data/openclaw_apply_drafts/resume_uploads/resume.txt",
                "resume_variant_text": "Fallback text should not be used",
                "resume_file_name": "resume.txt",
            }
        },
        paths,
        runner_module.logging.getLogger("test_openclaw_apply_draft_runner"),
    )

    assert materialized_path == str(host_resume_path.resolve())
    assert warnings == []


def test_materialize_resume_file_prefers_pdf_for_linkedin_easy_apply(tmp_path) -> None:
    resume_dir = tmp_path / "resume_uploads"
    resume_dir.mkdir(parents=True, exist_ok=True)
    txt_path = resume_dir / "resume.txt"
    pdf_path = resume_dir / "resume.pdf"
    docx_path = resume_dir / "resume.docx"
    txt_path.write_text("Tailored resume text", encoding="utf-8")
    pdf_path.write_bytes(b"%PDF-1.4\n%Test\n")
    docx_path.write_bytes(b"PK\x03\x04")
    payload = {
        "application_target": {
            "job_id": "job-1",
            "company": "Acme AI",
            "title": "Senior ML Engineer",
            "application_url": "https://www.linkedin.com/jobs/view/123/apply/?openSDUIApplyFlow=true",
        },
        "resume_variant": {
            "resume_upload_path": str(txt_path),
            "resume_variant_text": "Fallback text should not be used",
            "resume_file_name": "resume.txt",
        },
        "lineage": {"pipeline_id": "pipe-apply-1", "task_id": "task-draft-1", "run_id": "run-draft-1"},
    }
    paths = build_artifact_paths(
        payload,
        RunnerConfig(
            adapter="command",
            tool_command="python fake_tool.py",
            python_entrypoint=None,
            headless=True,
            screenshot_root=tmp_path / "screenshots",
            receipt_root=tmp_path / "receipts",
            resume_root=resume_dir,
            timeout_seconds=30,
            max_steps=12,
            log_level="INFO",
            inspect_only=False,
            allowed_resume_extensions=(".pdf", ".doc", ".docx", ".txt", ".rtf"),
            auth_strategy=None,
            storage_state_path=None,
            browser_profile_path=None,
            browser_attach_mode=False,
            skip_browser_start=False,
            allow_browser_start=True,
            gateway_url=None,
            cdp_url=None,
            host_gateway_alias=None,
            run_on_host=False,
            host_gateway_url="ws://127.0.0.1:18789",
            host_cdp_url="http://127.0.0.1:18800",
        ),
    )

    materialized_path, warnings = _materialize_resume_file(
        payload,
        paths,
        logging.getLogger("test_openclaw_apply_draft_runner"),
    )

    assert materialized_path == str(pdf_path.resolve())
    assert "resume_upload_path_incompatible_with_target_site" in warnings


def test_materialize_resume_file_prefers_docx_for_linkedin_when_pdf_is_absent(tmp_path) -> None:
    resume_dir = tmp_path / "resume_uploads"
    resume_dir.mkdir(parents=True, exist_ok=True)
    txt_path = resume_dir / "resume.txt"
    docx_path = resume_dir / "resume.docx"
    txt_path.write_text("Tailored resume text", encoding="utf-8")
    docx_path.write_bytes(b"PK\x03\x04")
    payload = {
        "application_target": {
            "job_id": "job-1",
            "company": "Acme AI",
            "title": "Senior ML Engineer",
            "application_url": "https://www.linkedin.com/jobs/view/123/apply/?openSDUIApplyFlow=true",
        },
        "resume_variant": {
            "resume_upload_path": str(txt_path),
            "resume_variant_text": "Fallback text should not be used",
            "resume_file_name": "resume.txt",
        },
        "lineage": {"pipeline_id": "pipe-apply-1", "task_id": "task-draft-1", "run_id": "run-draft-1"},
    }
    paths = build_artifact_paths(
        payload,
        RunnerConfig(
            adapter="command",
            tool_command="python fake_tool.py",
            python_entrypoint=None,
            headless=True,
            screenshot_root=tmp_path / "screenshots",
            receipt_root=tmp_path / "receipts",
            resume_root=resume_dir,
            timeout_seconds=30,
            max_steps=12,
            log_level="INFO",
            inspect_only=False,
            allowed_resume_extensions=(".pdf", ".doc", ".docx", ".txt", ".rtf"),
            auth_strategy=None,
            storage_state_path=None,
            browser_profile_path=None,
            browser_attach_mode=False,
            skip_browser_start=False,
            allow_browser_start=True,
            gateway_url=None,
            cdp_url=None,
            host_gateway_alias=None,
            run_on_host=False,
            host_gateway_url="ws://127.0.0.1:18789",
            host_cdp_url="http://127.0.0.1:18800",
        ),
    )

    materialized_path, warnings = _materialize_resume_file(
        payload,
        paths,
        logging.getLogger("test_openclaw_apply_draft_runner"),
    )

    assert materialized_path == str(docx_path.resolve())
    assert "resume_upload_path_incompatible_with_target_site" in warnings


def test_openclaw_apply_draft_runner_rejects_text_only_linkedin_resume_upload(tmp_path) -> None:
    payload = _base_payload()
    payload["application_target"]["application_url"] = "https://www.linkedin.com/jobs/view/123/apply/?openSDUIApplyFlow=true"
    payload["application_target"]["source_url"] = "https://www.linkedin.com/jobs/view/123/"

    config = RunnerConfig(
        adapter="command",
        tool_command="python fake_tool.py",
        python_entrypoint=None,
        headless=True,
        screenshot_root=tmp_path / "screenshots",
        receipt_root=tmp_path / "receipts",
        resume_root=tmp_path / "resume_uploads",
        timeout_seconds=30,
        max_steps=12,
        log_level="INFO",
        inspect_only=False,
        allowed_resume_extensions=(".pdf", ".doc", ".docx", ".txt", ".rtf"),
        auth_strategy=None,
        storage_state_path=None,
        browser_profile_path=None,
        browser_attach_mode=False,
        skip_browser_start=False,
        allow_browser_start=True,
        gateway_url=None,
        cdp_url=None,
        host_gateway_alias=None,
        run_on_host=False,
        host_gateway_url="ws://127.0.0.1:18789",
        host_cdp_url="http://127.0.0.1:18800",
    )

    result = execute_apply_draft(payload, config=config)

    assert result["draft_status"] == "not_started"
    assert result["failure_category"] == "unsupported_resume_upload_format"
    assert result["errors"] == [
        "unsupported_resume_upload_format:text_only_resume_variant",
        "resume_upload_site:linkedin_easy_apply",
    ]
