import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "worker"))

from integrations.openclaw_apply_runner import RunnerConfig, execute_apply_draft


SCRIPT_PATH = ROOT / "scripts" / "openclaw_apply_draft.py"


def _base_payload() -> dict:
    return {
        "action": "apply_draft",
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
    env = os.environ.copy()
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
    screenshot_path = Path(result["screenshot_metadata_references"][0]["path"])
    assert screenshot_path.exists()
    assert screenshot_path.parent.name == "pipe-apply-1-task-draft-1-run-draft-1-job-1-acme-ai-senior-ml-engineer"


def test_openclaw_apply_draft_runner_returns_tool_unavailable_without_adapter(tmp_path) -> None:
    payload = _base_payload()
    env = os.environ.copy()
    env.update(
        {
            "OPENCLAW_APPLY_SCREENSHOT_DIR": str(tmp_path / "screenshots"),
            "OPENCLAW_APPLY_RECEIPT_DIR": str(tmp_path / "receipts"),
            "OPENCLAW_APPLY_RESUME_DIR": str(tmp_path / "resume_uploads"),
            "TEST_TMPDIR": str(tmp_path),
        }
    )
    env.pop("OPENCLAW_APPLY_ADAPTER", None)
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
    env = os.environ.copy()
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
    env = os.environ.copy()
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
    assert result["failure_category"] == "manual_review_required"
    assert "submit_signal_detected_and_overridden" in result["warnings"]
    assert result["awaiting_review"] is True


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
        auth_strategy=None,
        storage_state_path=None,
        browser_profile_path=None,
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
