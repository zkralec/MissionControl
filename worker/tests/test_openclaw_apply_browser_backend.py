import logging
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "worker"))

from integrations.openclaw_apply_browser_backend import (
    BrowserCommandError,
    OpenClawBrowserClient,
    _choose_next_candidate,
    _current_form_date,
    _normalize_browser_base_command,
    _resolve_runtime_config,
    _sanitize_next_candidate,
    run_backend,
)


def _payload(tmp_path: Path, *, inspect_only: bool = False) -> dict:
    resume_path = tmp_path / "resume.txt"
    resume_path.write_text("Tailored resume", encoding="utf-8")
    screenshot_dir = tmp_path / "screenshots"
    screenshot_dir.mkdir()
    return {
        "submit": False,
        "stop_before_submit": True,
        "inspect_only": inspect_only,
        "application_target": {
            "job_id": "job-1",
            "company": "Acme AI",
            "title": "Senior ML Engineer",
            "application_url": "https://jobs.example/apply/1",
        },
        "resume_variant": {
            "resume_file_name": "resume.txt",
            "resume_upload_path": str(resume_path),
        },
        "application_answers": [
            {"question": "Why are you interested?", "answer": "Strong fit and mission alignment."}
        ],
        "cover_letter_text": "Dear Hiring Team",
        "capture_screenshots": True,
        "max_screenshots": 4,
        "constraints": {
            "submit": False,
            "stop_before_submit": True,
            "inspect_only": inspect_only,
            "skip_field_fills": inspect_only,
            "skip_resume_upload": inspect_only,
            "timeout_seconds": 30,
        },
        "auth": {"session_available": True},
        "artifacts": {
            "run_key": "run-1",
            "screenshot_dir": str(screenshot_dir),
            "resume_upload_path": str(resume_path),
        },
    }


class FakeBrowserClient:
    def __init__(
        self,
        *,
        page_title: str = "Apply - Senior ML Engineer",
        current_url: str = "https://jobs.example/apply/1",
        snapshots: list[str] | None = None,
        fail_upload: bool = False,
    ) -> None:
        self.page_title = page_title
        self.current_url = current_url
        self.snapshots = list(
            snapshots
            or [
                '[10] input "Resume upload"\n[20] textarea "Cover letter"\n[21] textarea "Why are you interested?"\n[99] button "Submit application"',
                '[20] textarea "Cover letter"\n[21] textarea "Why are you interested?"\n[99] button "Submit application"',
                '[20] textarea "Cover letter"\n[21] textarea "Why are you interested?"\n[99] button "Submit application"',
                '[20] textarea "Cover letter"\n[21] textarea "Why are you interested?"\n[99] button "Submit application"',
            ]
        )
        self.fail_upload = fail_upload
        self.fill_calls: list[list[dict]] = []
        self.select_calls: list[tuple[str, str]] = []
        self.click_calls: list[str] = []
        self.upload_calls: list[tuple[str | None, str]] = []
        self.start_calls = 0
        self.status_calls = 0
        self.tabs_calls = 0
        self.submit_probe_result = None
        self.submit_click_result = None
        self.active_step_probe_result = None
        self.next_click_result = None
        self.dom_submit_clicks = 0
        self.dom_next_clicks = 0

    def start(self) -> None:
        self.start_calls += 1
        return None

    def status(self) -> str:
        self.status_calls += 1
        return "ok"

    def tabs(self) -> str:
        self.tabs_calls += 1
        return "[]"

    def open(self, url: str) -> None:
        self.current_url = url

    def click(self, ref: str) -> None:
        self.click_calls.append(ref)

    def wait_for_load(self, load_state: str) -> None:
        return None

    def snapshot(self) -> str:
        if self.snapshots:
            return self.snapshots.pop(0)
        return ""

    def screenshot(self, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"PNG")
        return destination

    def evaluate_json(self, fn_source: str):
        if "document.title" in fn_source:
            return self.page_title
        if "window.location.href" in fn_source:
            return self.current_url
        if "__openclaw_linkedin_submit_probe__" in fn_source:
            return self.submit_probe_result
        if "__openclaw_linkedin_submit_click__" in fn_source:
            self.dom_submit_clicks += 1
            return self.submit_click_result
        if "__openclaw_linkedin_active_step_probe__" in fn_source:
            return self.active_step_probe_result
        if "__openclaw_linkedin_next_click__" in fn_source:
            self.dom_next_clicks += 1
            return self.next_click_result
        return None

    def upload(self, staged_path: Path, *, input_ref: str | None = None) -> None:
        if self.fail_upload:
            raise BrowserCommandError(
                failure_category="upload_failed",
                blocking_reason="Upload failed.",
                errors=["upload_failed"],
            )
        self.upload_calls.append((input_ref, staged_path.name))

    def fill(self, fields: list[dict]) -> None:
        self.fill_calls.append(fields)

    def select(self, ref: str, value: str) -> None:
        self.select_calls.append((ref, value))

    def command_debug(self) -> list[dict]:
        return []


def _flatten_fill_calls(client: FakeBrowserClient) -> dict[str, object]:
    flattened: dict[str, object] = {}
    for batch in client.fill_calls:
        for row in batch:
            flattened[str(row["ref"])] = row["value"]
    return flattened


def _post_upload_client(form_snapshot: str, *, final_snapshot: str | None = None) -> FakeBrowserClient:
    return FakeBrowserClient(
        snapshots=[
            '[10] input "Resume upload"',
            form_snapshot,
            final_snapshot or form_snapshot,
        ]
    )


def _configure_linkedin_easy_apply_payload(payload: dict, tmp_path: Path) -> None:
    pdf_path = tmp_path / "resume.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%Test\n")
    payload["application_target"]["application_url"] = "https://www.linkedin.com/jobs/view/4354740729/apply/?openSDUIApplyFlow=true"
    payload["application_target"]["source_url"] = "https://www.linkedin.com/jobs/view/4354740729/"
    payload["resume_variant"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_file_name"] = "resume.pdf"
    payload["artifacts"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_variant_text"] = (
        "Zachary Kralec\n"
        "zkralec@icloud.com\n"
        "240-555-0101\n"
    )
    payload["contact_profile"] = {
        "city": "Saint Mary's City",
        "state_or_province": "MD",
        "postal_code": "20686",
        "country": "United States",
        "primary_phone_number": "240-555-0101",
        "phone_type": "mobile",
    }


class HighConfidenceLinkedInSubmitClient(FakeBrowserClient):
    def __init__(self, *, review_snapshot: str, submitted_snapshot: str, dom_submit_advances: bool = True) -> None:
        super().__init__(
            page_title="Apply to Acme AI",
            current_url="https://www.linkedin.com/jobs/view/4354740729/",
            snapshots=[],
        )
        self.stage = "contact"
        self.review_snapshot = review_snapshot
        self.submitted_snapshot = submitted_snapshot
        self.dom_submit_advances = dom_submit_advances
        self.values: dict[str, object] = {}

    def snapshot(self) -> str:
        if self.stage == "contact":
            return "\n".join(
                [
                    '[dialog-root] dialog "Apply to Acme AI" [active]',
                    '[step-heading] heading "Contact info"',
                    '[first-name] textbox "First name*": Zachary',
                    '[last-name] textbox "Last name*": Kralec',
                    '[email-address] combobox "Email address *": zkralec@icloud.com selected',
                    '[city-input] textbox "City*": Saint Mary\'s City',
                    '[state-input] textbox "State or Province*": MD',
                    '[zip-input] textbox "Zip/Postal Code*": 20686',
                    '[country-select] combobox "Country *": United States selected',
                    '[phone-input] textbox "Primary Phone Number*": 240-555-0101',
                    '[phone-type-mobile] radio "Mobile" checked',
                    '[contact-next] button "Continue to next step"',
                ]
            )
        if self.stage == "resume":
            return "\n".join(
                [
                    '[dialog-root] dialog "Apply to Acme AI" [active]',
                    '[step-heading] heading "Resume"',
                    'generic "Selected"',
                    '[resume-file] heading "Zachary Kralec Resume 04_01_26.pdf"',
                    '[selected-resume] radio "Deselect resume Zachary Kralec Resume 04_01_26.pdf" checked',
                    '[resume-next] button "Continue to next step"',
                ]
            )
        if self.stage == "top_choice":
            return "\n".join(
                [
                    '[dialog-root] dialog "Apply to Acme AI" [active]',
                    '[step-heading] heading "Additional questions"',
                    'text "Mark this job as a top choice (Optional)"',
                    '[top-choice] checkbox "Mark this job as a top choice"',
                    '[top-choice-next] button "Continue to next step"',
                ]
            )
        if self.stage == "screening":
            return "\n".join(
                [
                    '[dialog-root] dialog "Apply to Acme AI" [active]',
                    '[step-heading] heading "Additional questions"',
                    '[auth] combobox "Are you authorized to work in the US without a sponsor visa? *"',
                    '[recruiter] combobox "Have you been working with a recruiter at Acme AI? *"',
                    '[clearance] combobox "Do you currently hold an active government issued security clearance? *"',
                    '[clearance-level] combobox "Security clearance level *"',
                    '[polygraph] combobox "Do you have an active polygraph? *"',
                    '[salary] textbox "Desired salary *"',
                    '[start-date] textbox "Available start date *"',
                    '[hear-about] combobox "How did you learn about our company? *"',
                    '- Certification statement:',
                    '  [cert-name] textbox "Full name *"',
                    '  [cert-date] textbox "Today\'s date *"',
                    '  [cert-confirm] checkbox "I have read and understand the above statement *"',
                    '[screening-next] button "Continue to next step"',
                ]
            )
        if self.stage == "review":
            return self.review_snapshot
        return self.submitted_snapshot

    def click(self, ref: str) -> None:
        super().click(ref)
        if ref == "contact-next":
            self.stage = "resume"
        elif ref == "resume-next":
            self.stage = "top_choice"
        elif ref == "top-choice-next":
            self.stage = "screening"
        elif ref == "screening-next":
            self.stage = "review"
        elif ref == "submit-application":
            self.stage = "submitted"
        elif ref == "submit-button":
            self.stage = "submitted"

    def evaluate_json(self, fn_source: str):
        result = super().evaluate_json(fn_source)
        if (
            "__openclaw_linkedin_submit_click__" in fn_source
            and self.dom_submit_advances
            and isinstance(result, dict)
            and bool(result.get("clicked"))
        ):
            self.stage = "submitted"
        return result

    def fill(self, fields: list[dict]) -> None:
        super().fill(fields)
        for row in fields:
            self.values[str(row["ref"])] = row["value"]

    def select(self, ref: str, value: str) -> None:
        super().select(ref, value)
        self.values[ref] = value


def test_backend_success_path_returns_review_ready_payload(tmp_path: Path) -> None:
    result = run_backend(_payload(tmp_path), client=FakeBrowserClient())

    assert result["draft_status"] == "draft_ready"
    assert result["awaiting_review"] is True
    assert result["submitted"] is False
    assert len(result["fields_filled_manifest"]) >= 2
    assert len(result["screenshot_metadata_references"]) >= 2
    assert result["notify_decision"]["should_notify"] is True


def test_backend_inspect_only_skips_fills_and_uploads(tmp_path: Path) -> None:
    client = FakeBrowserClient()
    result = run_backend(_payload(tmp_path, inspect_only=True), client=client)

    assert result["draft_status"] == "inspect_only"
    assert result["source_status"] == "inspect_only"
    assert result["awaiting_review"] is False
    assert result["notify_decision"]["should_notify"] is False
    assert client.upload_calls == []
    assert client.fill_calls == []
    assert len(result["screenshot_metadata_references"]) == 1


def test_backend_returns_login_required_when_login_wall_detected(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["auth"] = {"session_available": False}

    class RedirectedLoginClient(FakeBrowserClient):
        def open(self, url: str) -> None:
            return None

    client = RedirectedLoginClient(
        page_title="Sign in",
        current_url="https://auth.example/login",
        snapshots=['[1] heading "Sign in"\n[2] textbox "Email"'],
    )

    result = run_backend(payload, client=client)

    assert result["failure_category"] == "login_required"
    assert result["awaiting_review"] is False


def test_backend_linkedin_checkpoint_reports_login_required_with_precise_diagnostics(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["auth"] = {"session_available": False}
    payload["application_target"]["application_url"] = "https://www.linkedin.com/jobs/view/4354740729/apply/?openSDUIApplyFlow=true"

    class LinkedInCheckpointClient(FakeBrowserClient):
        def open(self, url: str) -> None:
            return None

    client = LinkedInCheckpointClient(
        page_title="Security Verification | LinkedIn",
        current_url="https://www.linkedin.com/checkpoint/challenge/123",
        snapshots=['[1] heading "Let\'s do a quick security check"\n[2] textbox "Email or phone"'],
    )

    result = run_backend(payload, client=client)

    assert result["failure_category"] == "login_required"
    assert result["page_diagnostics"]["final_url"] == "https://www.linkedin.com/checkpoint/challenge/123"
    assert result["page_diagnostics"]["login_or_checkpoint_markers_present"] is True
    assert result["page_diagnostics"]["explicit_login_url_detected"] is True
    assert "checkpoint" in result["page_diagnostics"]["checkpoint_marker_matches"]


def test_backend_rejects_submit_capable_request(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["submit"] = True

    result = run_backend(payload, client=FakeBrowserClient())

    assert result["failure_category"] == "unsafe_submit_attempted"
    assert result["submitted"] is False
    assert result["notify_decision"]["should_notify"] is False


def test_backend_returns_upload_failed_diagnostics(tmp_path: Path) -> None:
    result = run_backend(_payload(tmp_path), client=FakeBrowserClient(fail_upload=True))

    assert result["failure_category"] == "upload_failed"
    assert result["awaiting_review"] is False
    assert "upload_failed" in result["errors"]


def test_backend_linkedin_rejects_text_resume_upload_format(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["application_target"]["application_url"] = "https://www.linkedin.com/jobs/view/4354740729/apply/?openSDUIApplyFlow=true"
    payload["resume_variant"]["resume_file_name"] = "resume.txt"
    payload["artifacts"]["resume_upload_path"] = payload["resume_variant"]["resume_upload_path"]
    client = FakeBrowserClient(
        page_title="Apply to The Amatriot Group",
        snapshots=['[upload-input] input "Resume upload"\n[step-heading] heading "Resume"'],
    )

    result = run_backend(payload, client=client)

    assert result["draft_status"] == "not_started"
    assert result["failure_category"] == "unsupported_resume_upload_format"
    assert result["blocking_reason"] == "LinkedIn Easy Apply only accepts PDF, DOCX, or DOC resume uploads."
    assert "unsupported_resume_upload_format:.txt" in result["errors"]


def test_backend_requires_file_input_before_resume_upload(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    pdf_path = tmp_path / "resume.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%Test\n")
    payload["application_target"]["application_url"] = "https://www.linkedin.com/jobs/view/4354740729/apply/?openSDUIApplyFlow=true"
    payload["resume_variant"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_file_name"] = "resume.pdf"
    payload["artifacts"]["resume_upload_path"] = str(pdf_path)
    client = FakeBrowserClient(
        snapshots=[
            '[dialog-root] dialog "Apply to The Amatriot Group" [active]\n[10] button "Upload resume"\n[20] textarea "Cover letter"\n[99] button "Submit application"',
        ]
    )

    result = run_backend(payload, client=client)

    assert result["draft_status"] == "not_started"
    assert result["failure_category"] == "unsupported_form"
    assert "resume_upload_ref_not_file_input" in result["errors"]
    assert result["page_diagnostics"]["selected_resume_detected"] is False
    assert result["page_diagnostics"]["selected_resume_label"] is None
    assert result["page_diagnostics"]["selected_resume_verified"] is False
    assert result["page_diagnostics"]["upload_required"] is True
    assert result["page_diagnostics"]["continue_button_ref"] is None
    assert result["page_diagnostics"]["continue_clicked"] is False
    assert result["page_diagnostics"]["continue_verified"] is False
    assert client.upload_calls == []


def test_backend_linkedin_selected_resume_continues_without_upload(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    pdf_path = tmp_path / "resume.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%Test\n")
    payload["application_target"]["application_url"] = "https://www.linkedin.com/jobs/view/4354740729/apply/?openSDUIApplyFlow=true"
    payload["application_target"]["source_url"] = "https://www.linkedin.com/jobs/view/4354740729/"
    payload["resume_variant"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_file_name"] = "resume.pdf"
    payload["artifacts"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_variant_text"] = (
        "Zachary Kralec\n"
        "zkralec@icloud.com\n"
        "240-555-0101\n"
    )
    payload["contact_profile"] = {
        "city": "Saint Mary's City",
        "state_or_province": "MD",
        "postal_code": "20686",
        "country": "United States",
        "primary_phone_number": "240-555-0101",
        "phone_type": "mobile",
    }

    class LinkedInSelectedResumeClient(FakeBrowserClient):
        def __init__(self) -> None:
            super().__init__(
                page_title="Apply to The Amatriot Group",
                current_url="https://www.linkedin.com/jobs/view/4354740729/",
                snapshots=[],
            )
            self.stage = "contact"

        def snapshot(self) -> str:
            if self.stage == "contact":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                        '[step-heading] heading "Contact info"',
                        '[first-name] textbox "First name*": Zachary',
                        '[last-name] textbox "Last name*": Kralec',
                        '[email-address] combobox "Email address *": zkralec@icloud.com selected',
                        '[city-input] textbox "City*": Saint Mary\'s City',
                        '[state-input] textbox "State or Province*": MD',
                        '[zip-input] textbox "Zip/Postal Code*": 20686',
                        '[country-select] combobox "Country *": United States selected',
                        '[phone-input] textbox "Primary Phone Number*": 240-555-0101',
                        '[phone-type-mobile] radio "Mobile" checked',
                        '[contact-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "resume":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                        '[step-heading] heading "Resume"',
                        'generic "Selected"',
                        '[resume-file] heading "Zachary Kralec Resume 04_01_26.pdf"',
                        '[selected-resume] radio "Deselect resume Zachary Kralec Resume 04_01_26.pdf" checked',
                        '[resume-continue] button "Continue to next step"',
                    ]
                )
            return "\n".join(
                [
                    '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                    '[step-heading] heading "Review your application"',
                    '[review-note] note "Review your application before submitting"',
                ]
            )

        def click(self, ref: str) -> None:
            super().click(ref)
            if ref == "contact-next":
                self.stage = "resume"
            elif ref == "resume-continue":
                self.stage = "review"

    client = LinkedInSelectedResumeClient()
    result = run_backend(payload, client=client)

    assert result["failure_category"] is None
    assert result["draft_status"] == "draft_ready"
    assert result["page_diagnostics"]["linkedin_state"] == "easy_apply_review_step"
    assert result["page_diagnostics"]["selected_resume_detected"] is True
    assert result["page_diagnostics"]["selected_resume_label"] == "Zachary Kralec Resume 04_01_26.pdf"
    assert result["page_diagnostics"]["selected_resume_verified"] is True
    assert result["page_diagnostics"]["upload_required"] is False
    assert result["page_diagnostics"]["continue_button_ref"] == "resume-continue"
    assert result["page_diagnostics"]["continue_clicked"] is True
    assert result["page_diagnostics"]["continue_verified"] is True
    assert client.click_calls == ["contact-next", "resume-continue"]
    assert client.upload_calls == []


def test_backend_linkedin_later_steps_auto_submit_with_high_confidence_answers(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    pdf_path = tmp_path / "resume.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%Test\n")
    payload["application_target"]["application_url"] = "https://www.linkedin.com/jobs/view/4354740729/apply/?openSDUIApplyFlow=true"
    payload["application_target"]["source_url"] = "https://www.linkedin.com/jobs/view/4354740729/"
    payload["resume_variant"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_file_name"] = "resume.pdf"
    payload["artifacts"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_variant_text"] = (
        "Zachary Kralec\n"
        "zkralec@icloud.com\n"
        "240-555-0101\n"
    )
    payload["contact_profile"] = {
        "city": "Saint Mary's City",
        "state_or_province": "MD",
        "postal_code": "20686",
        "country": "United States",
        "primary_phone_number": "240-555-0101",
        "phone_type": "mobile",
    }

    class LinkedInLaterStepsClient(FakeBrowserClient):
        def __init__(self) -> None:
            super().__init__(
                page_title="Apply to Acme AI",
                current_url="https://www.linkedin.com/jobs/view/4354740729/",
                snapshots=[],
            )
            self.stage = "contact"
            self.values: dict[str, object] = {}

        def snapshot(self) -> str:
            if self.stage == "contact":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Contact info"',
                        '[first-name] textbox "First name*": Zachary',
                        '[last-name] textbox "Last name*": Kralec',
                        '[email-address] combobox "Email address *": zkralec@icloud.com selected',
                        '[city-input] textbox "City*": Saint Mary\'s City',
                        '[state-input] textbox "State or Province*": MD',
                        '[zip-input] textbox "Zip/Postal Code*": 20686',
                        '[country-select] combobox "Country *": United States selected',
                        '[phone-input] textbox "Primary Phone Number*": 240-555-0101',
                        '[phone-type-mobile] radio "Mobile" checked',
                        '[contact-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "resume":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Resume"',
                        'generic "Selected"',
                        '[resume-file] heading "Zachary Kralec Resume 04_01_26.pdf"',
                        '[selected-resume] radio "Deselect resume Zachary Kralec Resume 04_01_26.pdf" checked',
                        '[resume-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "top_choice":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Additional questions"',
                        'text "Mark this job as a top choice (Optional)"',
                        '[top-choice] checkbox "Mark this job as a top choice"',
                        '[top-choice-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "screening":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Additional questions"',
                        '[auth] combobox "Are you authorized to work in the US without a sponsor visa? *"',
                        '[recruiter] combobox "Have you been working with a recruiter at Acme AI? *"',
                        '[clearance] combobox "Do you currently hold an active government issued security clearance? *"',
                        '[clearance-level] combobox "Security clearance level *"',
                        '[polygraph] combobox "Do you have an active polygraph? *"',
                        '[salary] textbox "Desired salary *"',
                        '[start-date] textbox "Available start date *"',
                        '[hear-about] combobox "How did you learn about our company? *"',
                        '- Certification statement:',
                        '  [cert-name] textbox "Full name *"',
                        '  [cert-date] textbox "Today\'s date *"',
                        '  [cert-confirm] checkbox "I have read and understand the above statement *"',
                        '[follow-company] checkbox "Follow Acme AI to stay up to date with their page"',
                        '[screening-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "review":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Review your application"',
                        '[submit-application] button "Submit application"',
                    ]
                )
            return "\n".join(
                [
                    '[dialog-root] dialog "Apply to Acme AI" [active]',
                    '[step-heading] heading "Application submitted"',
                    'text "Thank you for applying"',
                ]
            )

        def click(self, ref: str) -> None:
            super().click(ref)
            if ref == "contact-next":
                self.stage = "resume"
            elif ref == "resume-next":
                self.stage = "top_choice"
            elif ref == "top-choice-next":
                self.stage = "screening"
            elif ref == "screening-next":
                self.stage = "review"
            elif ref == "submit-application":
                self.stage = "submitted"

        def fill(self, fields: list[dict]) -> None:
            super().fill(fields)
            for row in fields:
                self.values[str(row["ref"])] = row["value"]

        def select(self, ref: str, value: str) -> None:
            super().select(ref, value)
            self.values[ref] = value

    client = LinkedInLaterStepsClient()
    result = run_backend(payload, client=client)
    filled = _flatten_fill_calls(client)
    current_date = _current_form_date()

    assert result["failure_category"] is None
    assert result["draft_status"] == "draft_ready"
    assert result["review_status"] == "submitted"
    assert result["awaiting_review"] is False
    assert result["submitted"] is True
    assert client.click_calls == ["contact-next", "resume-next", "top-choice-next", "screening-next", "submit-application"]
    assert ("auth", "Yes") in client.select_calls
    assert ("recruiter", "I have not worked with a recruiter") in client.select_calls
    assert ("clearance", "No") in client.select_calls
    assert ("clearance-level", "None") in client.select_calls
    assert ("polygraph", "No") in client.select_calls
    assert ("hear-about", "LinkedIn") in client.select_calls
    assert filled["salary"] == "100000"
    assert filled["start-date"] == current_date
    assert filled["cert-name"] == "Zachary Kralec"
    assert filled["cert-date"] == current_date
    assert filled["cert-confirm"] is True
    assert not any(ref == "follow-company" for batch in client.fill_calls for ref in [row["ref"] for row in batch])
    assert result["page_diagnostics"]["final_step_detected"] is True
    assert result["page_diagnostics"]["later_step_decision"] == "safe_auto_submit"
    assert result["page_diagnostics"]["submit_confidence"] == "high"
    assert result["page_diagnostics"]["auto_submit_allowed"] is True
    assert result["page_diagnostics"]["auto_submit_attempted"] is True
    assert result["page_diagnostics"]["auto_submit_succeeded"] is True
    assert result["page_diagnostics"]["should_auto_submit"] is True
    assert result["page_diagnostics"]["submit_button_present"] is True
    assert result["page_diagnostics"]["submit_signal_type"] == "text"
    assert result["page_diagnostics"]["submit_blocked_reason"] is None
    assert result["page_diagnostics"]["attempted_submit_without_button"] is False
    assert "final_step_detected" in result["page_diagnostics"]["submit_confidence_reasons"]
    assert "no_unresolved_fields" in result["page_diagnostics"]["submit_confidence_reasons"]
    assert "submit_visible_and_ready" in result["page_diagnostics"]["submit_confidence_reasons"]
    assert "confidence_below_threshold" not in result["page_diagnostics"]["submit_confidence_reasons"]


def test_linkedin_next_candidate_prefers_live_test_button() -> None:
    candidates = [
        _sanitize_next_candidate(
            {
                "refHint": "plain-next",
                "label": "Next",
                "tag": "button",
                "role": "",
                "attributes": {},
                "score": 10,
            }
        ),
        _sanitize_next_candidate(
            {
                "refHint": "[data-live-test-easy-apply-next-button]",
                "label": "Next",
                "tag": "button",
                "role": "",
                "attributes": {
                    "aria-label": "Continue to next step",
                    "data-live-test-easy-apply-next-button": "",
                },
                "score": 1000,
            }
        ),
    ]

    chosen = _choose_next_candidate([row for row in candidates if row], None)

    assert chosen is not None
    assert chosen["ref_hint"] == "[data-live-test-easy-apply-next-button]"
    assert chosen["next_signal_type"] == "data-live-test"


def test_backend_linkedin_top_choice_step_advances_and_verifies_heading_change(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    _configure_linkedin_easy_apply_payload(payload, tmp_path)

    class TopChoiceHeadingAdvanceClient(FakeBrowserClient):
        def __init__(self) -> None:
            super().__init__(page_title="Apply to Acme AI", current_url="https://www.linkedin.com/jobs/view/4354740729/", snapshots=[])
            self.stage = "contact"

        def snapshot(self) -> str:
            if self.stage == "contact":
                return "\n".join(['[dialog-root] dialog "Apply to Acme AI" [active]', '[step-heading] heading "Contact info"', '[contact-next] button "Continue to next step"'])
            if self.stage == "resume":
                return "\n".join(['[dialog-root] dialog "Apply to Acme AI" [active]', '[step-heading] heading "Resume"', 'generic "Selected"', '[selected-resume] radio "Deselect resume Zachary Kralec Resume.pdf" checked', '[resume-next] button "Continue to next step"'])
            if self.stage == "top_choice":
                return "\n".join(['[dialog-root] dialog "Apply to The Amatriot Group" [active]', '[step-heading] heading "Apply to The Amatriot Group"', 'text "Mark this job as a top choice (Optional)"', '[top-choice] checkbox "Mark this job as a top choice"', '[top-choice-next] button "Continue to next step"'])
            return "\n".join(['[dialog-root] dialog "Apply to Acme AI" [active]', '[step-heading] heading "Voluntary self identification"', 'group "Are you authorized to work in the US without a sponsor visa? * Required"', '[auth-yes] radio "Yes" checked', '[screening-next] button "Continue to next step"'])

        def click(self, ref: str) -> None:
            super().click(ref)
            if ref == "contact-next":
                self.stage = "resume"
            elif ref == "resume-next":
                self.stage = "top_choice"
            elif ref == "top-choice-next":
                self.stage = "screening"

    client = TopChoiceHeadingAdvanceClient()
    result = run_backend(payload, client=client)

    assert "top-choice" not in client.click_calls
    assert "top-choice-next" in client.click_calls
    top_choice_actions = [
        row for row in result["debug_json"]["linkedin_progression"] if row.get("chosen_ref") == "top-choice-next"
    ]
    assert top_choice_actions
    assert top_choice_actions[-1]["advanced_to_new_step"] is True
    assert result["page_diagnostics"]["top_choice_step_detected"] is True
    assert result["page_diagnostics"]["top_choice_skip_attempted"] is True
    assert result["page_diagnostics"]["top_choice_interaction_performed"] is False
    assert result["page_diagnostics"]["step_advance_attempted"] is True
    assert any(row["reason"] == "optional_top_choice_left_unchecked" for row in result["page_diagnostics"]["later_step_optional_steps_skipped"])


def test_backend_linkedin_later_step_advance_verified_when_progress_changes(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    _configure_linkedin_easy_apply_payload(payload, tmp_path)

    class ProgressAdvanceClient(FakeBrowserClient):
        def __init__(self) -> None:
            super().__init__(page_title="Apply to Acme AI", current_url="https://www.linkedin.com/jobs/view/4354740729/", snapshots=[])
            self.stage = "contact"

        def snapshot(self) -> str:
            if self.stage == "contact":
                return "\n".join(['[dialog-root] dialog "Apply to Acme AI" [active]', '[step-heading] heading "Contact info"', '[contact-next] button "Continue to next step"'])
            if self.stage == "resume":
                return "\n".join(['[dialog-root] dialog "Apply to Acme AI" [active]', '[step-heading] heading "Resume"', 'generic "Selected"', '[selected-resume] radio "Deselect resume Zachary Kralec Resume.pdf" checked', '[resume-next] button "Continue to next step"'])
            if self.stage == "screening_a":
                return "\n".join(['[dialog-root] dialog "Apply to Acme AI" [active]', '[step-heading] heading "Additional questions"', 'text "67%"', '[screening-next] button "Continue to next step"'])
            return "\n".join(['[dialog-root] dialog "Apply to Acme AI" [active]', '[step-heading] heading "Additional questions"', 'text "100%"', '[review-note] heading "Review your application"'])

        def click(self, ref: str) -> None:
            super().click(ref)
            if ref == "contact-next":
                self.stage = "resume"
            elif ref == "resume-next":
                self.stage = "screening_a"
            elif ref == "screening-next":
                self.stage = "screening_b"

    client = ProgressAdvanceClient()
    result = run_backend(payload, client=client)

    assert "screening-next" in client.click_calls
    assert result["page_diagnostics"]["step_advance_attempted"] is True
    assert result["page_diagnostics"]["step_advance_verified"] is True
    assert result["page_diagnostics"]["active_step_progress_percent"] == 100


def test_backend_linkedin_no_progress_after_two_attempts_returns_precise_blocking_reason(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    _configure_linkedin_easy_apply_payload(payload, tmp_path)

    class NoProgressNextClient(FakeBrowserClient):
        def __init__(self) -> None:
            super().__init__(page_title="Apply to Acme AI", current_url="https://www.linkedin.com/jobs/view/4354740729/", snapshots=[])
            self.stage = "contact"

        def snapshot(self) -> str:
            if self.stage == "contact":
                return "\n".join(['[dialog-root] dialog "Apply to Acme AI" [active]', '[step-heading] heading "Contact info"', '[contact-next] button "Continue to next step"'])
            if self.stage == "resume":
                return "\n".join(['[dialog-root] dialog "Apply to Acme AI" [active]', '[step-heading] heading "Resume"', 'generic "Selected"', '[selected-resume] radio "Deselect resume Zachary Kralec Resume.pdf" checked', '[resume-next] button "Continue to next step"'])
            return "\n".join(['[dialog-root] dialog "Apply to Acme AI" [active]', '[step-heading] heading "Additional questions"', '[screening-next] button "Continue to next step"'])

        def click(self, ref: str) -> None:
            super().click(ref)
            if ref == "contact-next":
                self.stage = "resume"
            elif ref == "resume-next":
                self.stage = "screening"

    client = NoProgressNextClient()
    result = run_backend(payload, client=client)

    assert result["failure_category"] == "manual_review_required"
    assert result["page_diagnostics"]["step_advance_attempted"] is True
    assert result["page_diagnostics"]["step_advance_verified"] is False
    assert result["page_diagnostics"]["step_advance_retry_attempted"] is True
    assert result["page_diagnostics"]["step_advance_retry_verified"] is False
    assert result["page_diagnostics"]["step_advance_blocking_reason"] == "active_step_signature_unchanged_after_next_click"
    assert "active_step_signature_unchanged_after_next_click" in result["blocking_reason"]
    assert client.click_calls.count("screening-next") == 2


def test_backend_linkedin_active_step_parsing_ignores_off_step_controls(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    _configure_linkedin_easy_apply_payload(payload, tmp_path)

    class ActiveStepScopeClient(FakeBrowserClient):
        def __init__(self) -> None:
            super().__init__(page_title="Apply to Acme AI", current_url="https://www.linkedin.com/jobs/view/4354740729/", snapshots=[])
            self.stage = "contact"

        def snapshot(self) -> str:
            if self.stage == "contact":
                return "\n".join(['[dialog-root] dialog "Apply to Acme AI" [active]', '[step-heading] heading "Contact info"', '[contact-next] button "Continue to next step"'])
            if self.stage == "resume":
                return "\n".join(['[dialog-root] dialog "Apply to Acme AI" [active]', '[step-heading] heading "Resume"', 'generic "Selected"', '[selected-resume] radio "Deselect resume Zachary Kralec Resume.pdf" checked', '[resume-next] button "Continue to next step"'])
            return "\n".join(
                [
                    '[dialog-root] dialog "Apply to Acme AI" [active]',
                    '[step-heading] heading "Additional questions"',
                    '[current-q] textbox "Desired salary *"',
                    '[off-step-auth] textbox "Are you authorized to work in the US without a sponsor visa? *"',
                    '[screening-next] button "Continue to next step"',
                ]
            )

        def click(self, ref: str) -> None:
            super().click(ref)
            if ref == "contact-next":
                self.stage = "resume"
            elif ref == "resume-next":
                self.stage = "screening"

        def evaluate_json(self, fn_source: str):
            if "__openclaw_linkedin_active_step_probe__" in fn_source and self.stage == "screening":
                return {
                    "probeKind": "__openclaw_linkedin_active_step_probe__",
                    "activeStepHeading": "Additional questions",
                    "activeStepProgressPercent": 67,
                    "activeStepRequiredLabels": ["Desired salary *"],
                    "activeStepVisibleLabels": ["Desired salary *"],
                    "nextCandidates": [
                        {
                            "refHint": "[data-test-easy-apply-next-button]",
                            "label": "Next",
                            "tag": "button",
                            "role": "",
                            "attributes": {
                                "aria-label": "Continue to next step",
                                "data-test-easy-apply-next-button": "",
                            },
                            "score": 500,
                        }
                    ],
                    "chosenNext": {
                        "refHint": "[data-test-easy-apply-next-button]",
                        "label": "Next",
                        "tag": "button",
                        "role": "",
                        "attributes": {
                            "aria-label": "Continue to next step",
                            "data-test-easy-apply-next-button": "",
                        },
                        "score": 500,
                    },
                }
            return super().evaluate_json(fn_source)

    client = ActiveStepScopeClient()
    result = run_backend(payload, client=client)

    assert result["page_diagnostics"]["active_step_required_labels"] == ["Desired salary *"]
    assert "Are you authorized to work in the US without a sponsor visa?" not in result["page_diagnostics"]["active_step_required_labels"]


def test_backend_linkedin_submit_probe_ignores_paragraph_and_chooses_submit_button(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    _configure_linkedin_easy_apply_payload(payload, tmp_path)
    review_snapshot = "\n".join(
        [
            '[dialog-root] dialog "Apply to Acme AI" [active]',
            '[warning-copy] paragraph "Submitting this application won\'t change your profile"',
            '[submit-button] button "Submit application"',
            '[review] heading "Review your application"',
        ]
    )
    submitted_snapshot = "\n".join(
        [
            '[dialog-root] dialog "Apply to Acme AI" [active]',
            '[success] heading "Application submitted"',
            'text "Thank you for applying"',
        ]
    )
    client = HighConfidenceLinkedInSubmitClient(review_snapshot=review_snapshot, submitted_snapshot=submitted_snapshot)
    client.submit_probe_result = {
        "probeKind": "__openclaw_linkedin_submit_probe__",
        "candidates": [
            {
                "refHint": "warning-copy",
                "label": "Submitting this application won't change your profile",
                "tag": "p",
                "role": "",
                "attributes": {},
                "score": 99,
            },
            {
                "refHint": "[data-live-test-easy-apply-submit-button]",
                "label": "Submit application",
                "tag": "button",
                "role": "",
                "attributes": {
                    "aria-label": "Submit application",
                    "data-live-test-easy-apply-submit-button": "",
                },
                "score": 1000,
            },
        ],
        "chosen": {
            "refHint": "warning-copy",
            "label": "Submitting this application won't change your profile",
            "tag": "p",
            "role": "",
            "attributes": {},
            "score": 99,
        },
    }
    client.submit_click_result = {
        "probeKind": "__openclaw_linkedin_submit_click__",
        "clicked": True,
        "chosen": {
            "refHint": "[data-live-test-easy-apply-submit-button]",
            "label": "Submit application",
            "tag": "button",
            "role": "",
            "attributes": {
                "aria-label": "Submit application",
                "data-live-test-easy-apply-submit-button": "",
            },
            "score": 1000,
        },
    }

    result = run_backend(payload, client=client)

    assert result["submitted"] is True
    assert result["page_diagnostics"]["submit_step_detected"] is True
    assert result["page_diagnostics"]["submit_button_present"] is True
    assert result["page_diagnostics"]["submit_candidate_tags"] == ["button"]
    assert result["page_diagnostics"]["chosen_submit_ref"] == "[data-live-test-easy-apply-submit-button]"
    assert result["page_diagnostics"]["chosen_submit_tag"] == "button"
    assert result["page_diagnostics"]["chosen_submit_attributes"]["aria-label"] == "Submit application"
    assert "data-live-test-easy-apply-submit-button" in result["page_diagnostics"]["chosen_submit_attributes"]
    assert client.dom_submit_clicks == 1


def test_backend_linkedin_submit_probe_prioritizes_live_test_submit_button(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    _configure_linkedin_easy_apply_payload(payload, tmp_path)
    review_snapshot = "\n".join(
        [
            '[dialog-root] dialog "Apply to Acme AI" [active]',
            '[primary-footer-submit] button "Submit application"',
            '[live-test-submit] button "Submit application"',
            '[review] heading "Review your application"',
        ]
    )
    submitted_snapshot = "\n".join(
        [
            '[dialog-root] dialog "Apply to Acme AI" [active]',
            '[success] heading "Application submitted"',
            'text "Thank you for applying"',
        ]
    )
    client = HighConfidenceLinkedInSubmitClient(review_snapshot=review_snapshot, submitted_snapshot=submitted_snapshot)
    client.submit_probe_result = {
        "probeKind": "__openclaw_linkedin_submit_probe__",
        "candidates": [
            {
                "refHint": "primary-footer-submit",
                "label": "Submit application",
                "tag": "button",
                "role": "",
                "attributes": {"aria-label": "Submit application"},
                "score": 28,
            },
            {
                "refHint": "[data-live-test-easy-apply-submit-button]",
                "label": "Submit application",
                "tag": "button",
                "role": "",
                "attributes": {
                    "aria-label": "Submit application",
                    "data-live-test-easy-apply-submit-button": "",
                },
                "score": 1000,
            },
        ],
        "chosen": {
            "refHint": "primary-footer-submit",
            "label": "Submit application",
            "tag": "button",
            "role": "",
            "attributes": {"aria-label": "Submit application"},
            "score": 28,
        },
    }
    client.submit_click_result = {
        "probeKind": "__openclaw_linkedin_submit_click__",
        "clicked": True,
        "chosen": {
            "refHint": "[data-live-test-easy-apply-submit-button]",
            "label": "Submit application",
            "tag": "button",
            "role": "",
            "attributes": {
                "aria-label": "Submit application",
                "data-live-test-easy-apply-submit-button": "",
            },
            "score": 1000,
        },
    }

    result = run_backend(payload, client=client)

    assert result["submitted"] is True
    assert result["page_diagnostics"]["submit_step_detected"] is True
    assert result["page_diagnostics"]["chosen_submit_ref"] == "[data-live-test-easy-apply-submit-button]"
    assert result["page_diagnostics"]["chosen_submit_attributes"]["data-live-test-easy-apply-submit-button"] == ""


def test_backend_linkedin_submit_fallback_uses_button_when_live_test_attribute_missing(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    _configure_linkedin_easy_apply_payload(payload, tmp_path)
    review_snapshot = "\n".join(
        [
            '[dialog-root] dialog "Apply to Acme AI" [active]',
            '[dismiss-button] button "Dismiss"',
            '[submit-button] button "Submit application"',
            '[review] heading "Review your application"',
        ]
    )
    submitted_snapshot = "\n".join(
        [
            '[dialog-root] dialog "Apply to Acme AI" [active]',
            '[success] heading "Application submitted"',
            'text "Thank you for applying"',
        ]
    )
    client = HighConfidenceLinkedInSubmitClient(review_snapshot=review_snapshot, submitted_snapshot=submitted_snapshot)
    client.submit_probe_result = {
        "probeKind": "__openclaw_linkedin_submit_probe__",
        "candidates": [
            {
                "refHint": "dismiss-button",
                "label": "Dismiss",
                "tag": "button",
                "role": "",
                "attributes": {"aria-label": "Dismiss"},
                "score": 0,
            },
            {
                "refHint": "submit-button",
                "label": "Submit application",
                "tag": "button",
                "role": "",
                "attributes": {"aria-label": "Submit application"},
                "score": 25,
            },
        ],
        "chosen": {
            "refHint": "submit-button",
            "label": "Submit application",
            "tag": "button",
            "role": "",
            "attributes": {"aria-label": "Submit application"},
            "score": 25,
        },
    }

    result = run_backend(payload, client=client)

    assert result["submitted"] is True
    assert result["page_diagnostics"]["chosen_submit_ref"] == "submit-button"
    assert result["page_diagnostics"]["chosen_submit_tag"] == "button"
    assert client.click_calls[-1] == "submit-button"


def test_backend_linkedin_submit_failure_detected_when_click_has_no_effect(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    _configure_linkedin_easy_apply_payload(payload, tmp_path)
    review_snapshot = "\n".join(
        [
            '[dialog-root] dialog "Apply to Acme AI" [active]',
            '[submit-button] button "Submit application"',
            '[review] heading "Review your application"',
        ]
    )
    client = HighConfidenceLinkedInSubmitClient(
        review_snapshot=review_snapshot,
        submitted_snapshot=review_snapshot,
        dom_submit_advances=False,
    )
    client.submit_probe_result = {
        "probeKind": "__openclaw_linkedin_submit_probe__",
        "candidates": [
            {
                "refHint": "[data-live-test-easy-apply-submit-button]",
                "label": "Submit application",
                "tag": "button",
                "role": "",
                "attributes": {
                    "aria-label": "Submit application",
                    "data-live-test-easy-apply-submit-button": "",
                },
                "score": 1000,
            }
        ],
        "chosen": {
            "refHint": "[data-live-test-easy-apply-submit-button]",
            "label": "Submit application",
            "tag": "button",
            "role": "",
            "attributes": {
                "aria-label": "Submit application",
                "data-live-test-easy-apply-submit-button": "",
            },
            "score": 1000,
        },
    }
    client.submit_click_result = {
        "probeKind": "__openclaw_linkedin_submit_click__",
        "clicked": True,
        "chosen": client.submit_probe_result["chosen"],
    }

    result = run_backend(payload, client=client)

    assert result["submitted"] is False
    assert result["failure_category"] == "manual_review_required"
    assert result["page_diagnostics"]["auto_submit_attempted"] is True
    assert result["page_diagnostics"]["auto_submit_succeeded"] is False
    assert result["page_diagnostics"]["submit_decision_reason"] == "submit_click_no_effect"
    assert "submit_click_no_effect" in result["page_diagnostics"]["submit_confidence_reasons"]
    assert "auto_submit_confirmation_missing" in result["errors"]


def test_backend_linkedin_review_step_with_only_next_visible_does_not_auto_submit(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    _configure_linkedin_easy_apply_payload(payload, tmp_path)
    review_snapshot = "\n".join(
        [
            '[dialog-root] dialog "Apply to Acme AI" [active]',
            '[step-heading] heading "Review your application"',
            '[review-next] button "Continue to next step"',
        ]
    )

    class ReviewOnlyNextClient(HighConfidenceLinkedInSubmitClient):
        def __init__(self) -> None:
            super().__init__(review_snapshot=review_snapshot, submitted_snapshot=review_snapshot, dom_submit_advances=False)

    client = ReviewOnlyNextClient()
    client.submit_probe_result = {
        "probeKind": "__openclaw_linkedin_submit_probe__",
        "candidates": [
            {
                "refHint": "review-next",
                "label": "Next",
                "tag": "button",
                "role": "",
                "attributes": {"aria-label": "Continue to next step"},
                "score": 0,
            }
        ],
        "chosen": {
            "refHint": "review-next",
            "label": "Next",
            "tag": "button",
            "role": "",
            "attributes": {"aria-label": "Continue to next step"},
            "score": 0,
        },
    }

    result = run_backend(payload, client=client)

    assert result["submitted"] is False
    assert result["page_diagnostics"]["review_step_detected"] is True
    assert result["page_diagnostics"]["submit_step_detected"] is False
    assert result["page_diagnostics"]["submit_button_present"] is False
    assert result["page_diagnostics"]["submit_signal_type"] == "none"
    assert result["page_diagnostics"]["final_step_detected"] is False
    assert result["page_diagnostics"]["later_step_decision"] == "safe_review_only"
    assert result["page_diagnostics"]["auto_submit_allowed"] is False
    assert result["page_diagnostics"]["auto_submit_attempted"] is False
    assert result["page_diagnostics"]["attempted_submit_without_button"] is False
    assert result["page_diagnostics"]["pre_submit_transition_attempted"] is True
    assert result["page_diagnostics"]["pre_submit_transition_succeeded"] is False
    assert result["page_diagnostics"]["submit_candidate_refs"] == []
    assert "submit_step_not_detected" in result["page_diagnostics"]["submit_confidence_reasons"]
    assert client.dom_submit_clicks == 0


def test_backend_linkedin_final_stage_misclassification_prefers_continue_then_reprobes_submit(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    _configure_linkedin_easy_apply_payload(payload, tmp_path)
    review_next_snapshot = "\n".join(
        [
            '[dialog-root] dialog "Apply to Acme AI" [active]',
            '[step-heading] heading "Review your application"',
            '[review-next] button "Continue to next step"',
        ]
    )
    submit_snapshot = "\n".join(
        [
            '[dialog-root] dialog "Apply to Acme AI" [active]',
            '[step-heading] heading "Review your application"',
            '[submit-button] button "Submit application"',
        ]
    )
    submitted_snapshot = "\n".join(
        [
            '[dialog-root] dialog "Apply to Acme AI" [active]',
            '[success] heading "Application submitted"',
            'text "Thank you for applying"',
        ]
    )

    class ReviewThenSubmitClient(HighConfidenceLinkedInSubmitClient):
        def __init__(self) -> None:
            super().__init__(review_snapshot=review_next_snapshot, submitted_snapshot=submitted_snapshot)

        def snapshot(self) -> str:
            if self.stage == "review_submit":
                return submit_snapshot
            return super().snapshot()

        def click(self, ref: str) -> None:
            super().click(ref)
            if ref == "review-next":
                self.stage = "review_submit"

        def evaluate_json(self, fn_source: str):
            if "__openclaw_linkedin_submit_probe__" in fn_source:
                if self.stage == "review":
                    return {
                        "probeKind": "__openclaw_linkedin_submit_probe__",
                        "candidates": [
                            {
                                "refHint": "review-next",
                                "label": "Next",
                                "tag": "button",
                                "role": "",
                                "attributes": {"aria-label": "Continue to next step"},
                                "score": 0,
                            }
                        ],
                        "chosen": {
                            "refHint": "review-next",
                            "label": "Next",
                            "tag": "button",
                            "role": "",
                            "attributes": {"aria-label": "Continue to next step"},
                            "score": 0,
                        },
                    }
                if self.stage == "review_submit":
                    return {
                        "probeKind": "__openclaw_linkedin_submit_probe__",
                        "candidates": [
                            {
                                "refHint": "[data-live-test-easy-apply-submit-button]",
                                "label": "Submit application",
                                "tag": "button",
                                "role": "",
                                "attributes": {
                                    "aria-label": "Submit application",
                                    "data-live-test-easy-apply-submit-button": "",
                                },
                                "score": 1000,
                            }
                        ],
                        "chosen": {
                            "refHint": "[data-live-test-easy-apply-submit-button]",
                            "label": "Submit application",
                            "tag": "button",
                            "role": "",
                            "attributes": {
                                "aria-label": "Submit application",
                                "data-live-test-easy-apply-submit-button": "",
                            },
                            "score": 1000,
                        },
                    }
            if "__openclaw_linkedin_submit_click__" in fn_source and self.stage == "review_submit":
                self.dom_submit_clicks += 1
                self.stage = "submitted"
                return {
                    "probeKind": "__openclaw_linkedin_submit_click__",
                    "clicked": True,
                    "chosen": {
                        "refHint": "[data-live-test-easy-apply-submit-button]",
                        "label": "Submit application",
                        "tag": "button",
                        "role": "",
                        "attributes": {
                            "aria-label": "Submit application",
                            "data-live-test-easy-apply-submit-button": "",
                        },
                        "score": 1000,
                    },
                }
            return super().evaluate_json(fn_source)

    client = ReviewThenSubmitClient()
    result = run_backend(payload, client=client)

    assert result["submitted"] is True
    assert "review-next" in client.click_calls
    assert result["page_diagnostics"]["pre_submit_transition_attempted"] is True
    assert result["page_diagnostics"]["pre_submit_transition_succeeded"] is True
    assert result["page_diagnostics"]["submit_step_detected"] is True
    assert result["page_diagnostics"]["submit_button_present"] is True
    assert result["debug_json"]["linkedin_progression"][-2]["action"] == "click_next"
    assert result["debug_json"]["linkedin_progression"][-2]["reason"] == "pre_submit_transition"
    assert result["debug_json"]["linkedin_progression"][-1]["action"] == "click_submit"
    assert result["page_diagnostics"]["chosen_submit_ref"] == "[data-live-test-easy-apply-submit-button]"
    assert client.dom_submit_clicks == 1


def test_backend_linkedin_submit_transition_probe_cap_stops_with_manual_review(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    _configure_linkedin_easy_apply_payload(payload, tmp_path)
    review_snapshots = {
        "review": "\n".join(
            [
                '[dialog-root] dialog "Apply to Acme AI" [active]',
                '[step-heading] heading "Review your application 1"',
                '[review-next] button "Continue to next step"',
            ]
        ),
        "review_2": "\n".join(
            [
                '[dialog-root] dialog "Apply to Acme AI" [active]',
                '[step-heading] heading "Review your application 2"',
                '[review-next] button "Continue to next step"',
            ]
        ),
        "review_3": "\n".join(
            [
                '[dialog-root] dialog "Apply to Acme AI" [active]',
                '[step-heading] heading "Review your application 3"',
                '[review-next] button "Continue to next step"',
            ]
        ),
    }

    class ReviewProbeLoopClient(HighConfidenceLinkedInSubmitClient):
        def __init__(self) -> None:
            super().__init__(review_snapshot=review_snapshots["review"], submitted_snapshot=review_snapshots["review_3"])

        def snapshot(self) -> str:
            if self.stage in review_snapshots:
                return review_snapshots[self.stage]
            return super().snapshot()

        def click(self, ref: str) -> None:
            super().click(ref)
            if ref == "review-next":
                if self.stage == "review":
                    self.stage = "review_2"
                elif self.stage == "review_2":
                    self.stage = "review_3"

        def evaluate_json(self, fn_source: str):
            if "__openclaw_linkedin_submit_probe__" in fn_source and self.stage in review_snapshots:
                return {
                    "probeKind": "__openclaw_linkedin_submit_probe__",
                    "candidates": [
                        {
                            "refHint": "review-next",
                            "label": "Next",
                            "tag": "button",
                            "role": "",
                            "attributes": {"aria-label": "Continue to next step"},
                            "score": 0,
                        }
                    ],
                    "chosen": {
                        "refHint": "review-next",
                        "label": "Next",
                        "tag": "button",
                        "role": "",
                        "attributes": {"aria-label": "Continue to next step"},
                        "score": 0,
                    },
                }
            return super().evaluate_json(fn_source)

    client = ReviewProbeLoopClient()
    result = run_backend(payload, client=client)

    assert result["source_status"] == "manual_review_required"
    assert result["failure_category"] == "manual_review_required"
    assert "submit_transition_probe_cap_reached" in result["blocking_reason"]
    assert result["page_diagnostics"]["submit_blocked_reason"] == "submit_transition_probe_cap_reached"
    assert result["page_diagnostics"]["pre_submit_transition_attempted"] is True
    assert result["page_diagnostics"]["auto_submit_attempted"] is False
    assert client.click_calls.count("review-next") == 2
    assert client.dom_submit_clicks == 0


def test_backend_linkedin_final_review_stops_for_safe_review_when_submit_confidence_is_ambiguous(
    tmp_path: Path, monkeypatch
) -> None:
    payload = _payload(tmp_path)
    payload["application_answers"] = []
    pdf_path = tmp_path / "resume.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%Test\n")
    payload["application_target"]["application_url"] = "https://www.linkedin.com/jobs/view/4354740729/apply/?openSDUIApplyFlow=true"
    payload["application_target"]["source_url"] = "https://www.linkedin.com/jobs/view/4354740729/"
    payload["resume_variant"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_file_name"] = "resume.pdf"
    payload["artifacts"]["resume_upload_path"] = str(pdf_path)

    monkeypatch.setattr(
        "integrations.openclaw_apply_browser_backend.motivation_answer",
        lambda **_: {
            "answer": "I am excited about this role.",
            "source": "llm_generated",
            "confidence": 0.86,
            "reason": "llm_generated",
        },
    )

    class LinkedInReviewOnlyClient(FakeBrowserClient):
        def __init__(self) -> None:
            super().__init__(
                page_title="Apply to Acme AI",
                current_url="https://www.linkedin.com/jobs/view/4354740729/",
                snapshots=[],
            )
            self.stage = "contact"

        def snapshot(self) -> str:
            if self.stage == "contact":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Contact info"',
                        '[first-name] textbox "First name*": Zachary',
                        '[last-name] textbox "Last name*": Kralec',
                        '[contact-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "resume":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Resume"',
                        'generic "Selected"',
                        '[selected-resume] radio "Deselect resume Zachary Kralec Resume.pdf" checked',
                        '[resume-next] button "Continue to next step"',
                    ]
                )
            return "\n".join(
                [
                    '[dialog-root] dialog "Apply to Acme AI" [active]',
                    '[step-heading] heading "Review your application"',
                    '[motivation] textarea "Why are you interested in this role? *"',
                    '[submit-application] button "Submit application"',
                ]
            )

        def click(self, ref: str) -> None:
            super().click(ref)
            if ref == "contact-next":
                self.stage = "resume"
            elif ref == "resume-next":
                self.stage = "review"

    client = LinkedInReviewOnlyClient()
    result = run_backend(payload, client=client)

    assert result["failure_category"] is None
    assert result["draft_status"] == "draft_ready"
    assert result["awaiting_review"] is True
    assert result["submitted"] is False
    assert client.click_calls == ["contact-next", "resume-next"]
    assert result["page_diagnostics"]["final_step_detected"] is True
    assert result["page_diagnostics"]["later_step_decision"] == "safe_review_only"
    assert result["page_diagnostics"]["submit_confidence"] == "medium"
    assert result["page_diagnostics"]["auto_submit_allowed"] is False
    assert result["page_diagnostics"]["auto_submit_attempted"] is False
    assert result["page_diagnostics"]["auto_submit_succeeded"] is False
    assert result["page_diagnostics"]["should_auto_submit"] is False
    assert "final_step_detected" in result["page_diagnostics"]["submit_confidence_reasons"]
    assert "no_unresolved_fields" in result["page_diagnostics"]["submit_confidence_reasons"]
    assert "submit_visible_and_ready" in result["page_diagnostics"]["submit_confidence_reasons"]
    assert any(
        reason in result["page_diagnostics"]["submit_confidence_reasons"]
        for reason in ("heuristic_answers_present", "confidence_below_threshold", "required_disclosures_uncertain")
    )


def test_backend_linkedin_later_step_recovers_from_stale_select_ref(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    pdf_path = tmp_path / "resume.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%Test\n")
    payload["application_target"]["application_url"] = "https://www.linkedin.com/jobs/view/4354740729/apply/?openSDUIApplyFlow=true"
    payload["application_target"]["source_url"] = "https://www.linkedin.com/jobs/view/4354740729/"
    payload["resume_variant"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_file_name"] = "resume.pdf"
    payload["artifacts"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_variant_text"] = "Zachary Kralec\nzkralec@icloud.com\n240-555-0101\n"
    payload["contact_profile"] = {
        "city": "Saint Mary's City",
        "state_or_province": "MD",
        "postal_code": "20686",
        "country": "United States",
        "primary_phone_number": "240-555-0101",
        "phone_type": "mobile",
    }

    class LinkedInStaleSelectRetryClient(FakeBrowserClient):
        def __init__(self) -> None:
            super().__init__(
                page_title="Apply to Acme AI",
                current_url="https://www.linkedin.com/jobs/view/4354740729/",
                snapshots=[],
            )
            self.stage = "contact"
            self.stale_triggered = False

        def snapshot(self) -> str:
            if self.stage == "contact":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Contact info"',
                        '[first-name] textbox "First name*": Zachary',
                        '[last-name] textbox "Last name*": Kralec',
                        '[email-address] combobox "Email address *": zkralec@icloud.com selected',
                        '[city-input] textbox "City*": Saint Mary\'s City',
                        '[state-input] textbox "State or Province*": MD',
                        '[zip-input] textbox "Zip/Postal Code*": 20686',
                        '[country-select] combobox "Country *": United States selected',
                        '[phone-input] textbox "Primary Phone Number*": 240-555-0101',
                        '[phone-type-mobile] radio "Mobile" checked',
                        '[contact-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "resume":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Resume"',
                        'generic "Selected"',
                        '[resume-file] heading "Zachary Kralec Resume 04_01_26.pdf"',
                        '[selected-resume] radio "Deselect resume Zachary Kralec Resume 04_01_26.pdf" checked',
                        '[resume-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "screening":
                clearance_ref = "clearance-new" if self.stale_triggered else "clearance-old"
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Additional questions"',
                        f'[{clearance_ref}] combobox "Do you currently hold an active government issued security clearance? *"',
                        '[screening-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "review":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Review your application"',
                        '[submit-application] button "Submit application"',
                    ]
                )
            return "\n".join(
                [
                    '[dialog-root] dialog "Apply to Acme AI" [active]',
                    '[step-heading] heading "Application submitted"',
                    'text "Thank you for applying"',
                ]
            )

        def click(self, ref: str) -> None:
            super().click(ref)
            if ref == "contact-next":
                self.stage = "resume"
            elif ref == "resume-next":
                self.stage = "screening"
            elif ref == "screening-next":
                self.stage = "review"
            elif ref == "submit-application":
                self.stage = "submitted"

        def select(self, ref: str, value: str) -> None:
            super().select(ref, value)
            if ref == "clearance-old" and not self.stale_triggered:
                self.stale_triggered = True
                raise BrowserCommandError(
                    failure_category="navigation_failed",
                    blocking_reason="OpenClaw browser command failed: select clearance-old No",
                    errors=["openclaw_browser_command_failed:1"],
                    stage="select_field",
                    error_kind="navigation_failure",
                    command_debug={
                        "stderr": 'Element "clearance-old" not found or not visible. Run a new snapshot to see current page elements.',
                        "stdout": "",
                        "args": ["select", "clearance-old", value],
                    },
                )

    client = LinkedInStaleSelectRetryClient()
    result = run_backend(payload, client=client)

    assert result["failure_category"] is None
    assert result["draft_status"] == "draft_ready"
    assert result["submitted"] is True
    assert ("clearance-old", "No") in client.select_calls
    assert ("clearance-new", "No") in client.select_calls
    action_diag = next(
        row for row in result["page_diagnostics"]["later_step_action_diagnostics"] if row.get("canonical_key") == "security_clearance"
    )
    assert action_diag["action_attempted"] == "select"
    assert action_diag["stale_ref_detected"] is True
    assert action_diag["retry_attempted"] is True
    assert action_diag["retry_succeeded"] is True
    assert action_diag["original_ref"] == "clearance-old"
    assert action_diag["replacement_ref"] == "clearance-new"


def test_backend_linkedin_later_step_answers_required_work_authorization_radio_yes(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    _configure_linkedin_easy_apply_payload(payload, tmp_path)

    class LinkedInLaterStepRadioClient(FakeBrowserClient):
        def __init__(self) -> None:
            super().__init__(
                page_title="Apply to Acme AI",
                current_url="https://www.linkedin.com/jobs/view/4354740729/",
                snapshots=[],
            )
            self.stage = "contact"
            self.selected_auth: str | None = None

        def snapshot(self) -> str:
            if self.stage == "contact":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Contact info"',
                        '[first-name] textbox "First name*": Zachary',
                        '[last-name] textbox "Last name*": Kralec',
                        '[email-address] combobox "Email address *": zkralec@icloud.com selected',
                        '[contact-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "resume":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Resume"',
                        'generic "Selected"',
                        '[selected-resume] radio "Deselect resume Zachary Kralec Resume.pdf" checked',
                        '[resume-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "screening":
                yes_line = '[auth-yes] radio "Yes" checked' if self.selected_auth == "Yes" else '[auth-yes] radio "Yes"'
                no_line = '[auth-no] radio "No" checked' if self.selected_auth == "No" else '[auth-no] radio "No"'
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Voluntary self identification"',
                        'group "Are you authorized to work in the US without a sponsor visa? * Required"',
                        yes_line,
                        no_line,
                        '[screening-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "review":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Review your application"',
                        '[submit-application] button "Submit application"',
                    ]
                )
            return "\n".join(
                [
                    '[dialog-root] dialog "Application submitted"',
                    'heading "Your application was sent"',
                ]
            )

        def click(self, ref: str) -> None:
            super().click(ref)
            if ref == "contact-next":
                self.stage = "resume"
            elif ref == "resume-next":
                self.stage = "screening"
            elif ref == "auth-yes":
                self.selected_auth = "Yes"
            elif ref == "auth-no":
                self.selected_auth = "No"
            elif ref == "screening-next" and self.selected_auth == "Yes":
                self.stage = "review"
            elif ref == "submit-application":
                self.stage = "submitted"

    client = LinkedInLaterStepRadioClient()
    result = run_backend(payload, client=client)

    assert result["failure_category"] is None
    assert "auth-yes" in client.click_calls
    assert "screening-next" in client.click_calls
    assert result["page_diagnostics"]["later_step_policy_matches"]
    assert any(row["canonical_key"] == "work_authorization_us" for row in result["page_diagnostics"]["later_step_policy_matches"])
    assert any(row["canonical_key"] == "work_authorization_us" for row in result["page_diagnostics"]["later_step_answers_applied"])
    radio_groups = {row["field_name"]: row for row in result["page_diagnostics"]["later_step_radio_group_diagnostics"]}
    assert radio_groups["work_authorization_us"]["selected_option"] == "Yes"
    assert radio_groups["work_authorization_us"]["selection_verified"] is True
    statuses = {row["field_name"]: row for row in result["page_diagnostics"]["later_step_required_field_statuses"]}
    assert statuses["work_authorization_us"]["satisfied"] is True
    resolution_rows = result["page_diagnostics"]["later_step_canonical_key_resolution"]
    assert any(row["resolved_field_name"] == "work_authorization_us" for row in resolution_rows)
    assert not any(row["resolved_field_name"] == "state_or_province" for row in resolution_rows)
    strategy_rows = {row["field_name"]: row for row in result["page_diagnostics"]["later_step_radio_selection_strategy"]}
    assert strategy_rows["work_authorization_us"]["verification_method"] == "checked_state"
    assert result["page_diagnostics"]["later_step_continue_gate_reason"] == "all_required_later_step_fields_satisfied"


def test_backend_linkedin_later_step_blocks_continue_until_required_radio_verified(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    _configure_linkedin_easy_apply_payload(payload, tmp_path)

    class LinkedInLaterStepRadioUnverifiedClient(FakeBrowserClient):
        def __init__(self) -> None:
            super().__init__(
                page_title="Apply to Acme AI",
                current_url="https://www.linkedin.com/jobs/view/4354740729/",
                snapshots=[],
            )
            self.stage = "contact"

        def snapshot(self) -> str:
            if self.stage == "contact":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Contact info"',
                        '[first-name] textbox "First name*": Zachary',
                        '[last-name] textbox "Last name*": Kralec',
                        '[contact-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "resume":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Resume"',
                        'generic "Selected"',
                        '[selected-resume] radio "Deselect resume Zachary Kralec Resume.pdf" checked',
                        '[resume-next] button "Continue to next step"',
                    ]
                )
            return "\n".join(
                [
                    '[dialog-root] dialog "Apply to Acme AI" [active]',
                    '[step-heading] heading "Voluntary self identification"',
                    'group "Are you authorized to work in the US without a sponsor visa? * Required"',
                    '[auth-yes] radio "Yes"',
                    '[auth-no] radio "No"',
                    '[screening-next] button "Continue to next step"',
                ]
            )

        def click(self, ref: str) -> None:
            super().click(ref)
            if ref == "contact-next":
                self.stage = "resume"
            elif ref == "resume-next":
                self.stage = "screening"

    client = LinkedInLaterStepRadioUnverifiedClient()
    result = run_backend(payload, client=client)

    assert result["source_status"] == "manual_review_required"
    assert "auth-yes" in client.click_calls
    assert "screening-next" not in client.click_calls
    assert result["page_diagnostics"]["later_step_continue_gate_reason"] == "blocking_required_later_step_fields"
    statuses = {row["field_name"]: row for row in result["page_diagnostics"]["later_step_required_field_statuses"]}
    assert statuses["work_authorization_us"]["satisfied"] is False
    assert statuses["work_authorization_us"]["selection_attempted"] is True


def test_backend_linkedin_later_step_verified_radio_selection_allows_continue(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    _configure_linkedin_easy_apply_payload(payload, tmp_path)

    class LinkedInLaterStepRadioContinueClient(FakeBrowserClient):
        def __init__(self) -> None:
            super().__init__(
                page_title="Apply to Acme AI",
                current_url="https://www.linkedin.com/jobs/view/4354740729/",
                snapshots=[],
            )
            self.stage = "contact"
            self.selected_auth = False

        def snapshot(self) -> str:
            if self.stage == "contact":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Contact info"',
                        '[contact-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "resume":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Resume"',
                        'generic "Selected"',
                        '[selected-resume] radio "Deselect resume Zachary Kralec Resume.pdf" checked',
                        '[resume-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "screening":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Voluntary self identification"',
                        'group "Are you authorized to work in the US without a sponsor visa? * Required"',
                        '[auth-yes] radio "Yes" checked' if self.selected_auth else '[auth-yes] radio "Yes"',
                        '[auth-no] radio "No"',
                        '[screening-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "review":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Review your application"',
                        '[submit-application] button "Submit application"',
                    ]
                )
            return "\n".join(
                [
                    '[dialog-root] dialog "Application submitted"',
                    'heading "Your application was sent"',
                ]
            )

        def click(self, ref: str) -> None:
            super().click(ref)
            if ref == "contact-next":
                self.stage = "resume"
            elif ref == "resume-next":
                self.stage = "screening"
            elif ref == "auth-yes":
                self.selected_auth = True
            elif ref == "screening-next" and self.selected_auth:
                self.stage = "review"
            elif ref == "submit-application":
                self.stage = "submitted"

    client = LinkedInLaterStepRadioContinueClient()
    result = run_backend(payload, client=client)

    assert result["failure_category"] is None
    assert client.click_calls.index("auth-yes") < client.click_calls.index("screening-next")
    assert result["page_diagnostics"]["pre_submit_transition_attempted"] is False
    assert result["page_diagnostics"]["later_step_continue_gate_reason"] == "all_required_later_step_fields_satisfied"


def test_backend_linkedin_later_step_reclassifies_bad_dom_radio_field_name_to_work_authorization(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    _configure_linkedin_easy_apply_payload(payload, tmp_path)

    class LinkedInLaterStepDomMisclassifiedRadioClient(FakeBrowserClient):
        def __init__(self) -> None:
            super().__init__(
                page_title="Apply to Acme AI",
                current_url="https://www.linkedin.com/jobs/view/4354740729/",
                snapshots=[],
            )
            self.stage = "contact"
            self.selected_auth = False

        def snapshot(self) -> str:
            if self.stage == "contact":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Contact info"',
                        '[contact-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "resume":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Resume"',
                        'generic "Selected"',
                        '[selected-resume] radio "Deselect resume Zachary Kralec Resume.pdf" checked',
                        '[resume-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "screening":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Voluntary self identification"',
                        'group "Are you authorized to work in the US without a sponsor visa? * Required"',
                        '[auth-yes] radio "Yes" checked' if self.selected_auth else '[auth-yes] radio "Yes"',
                        '[auth-no] radio "No"',
                        '[screening-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "review":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Review your application"',
                        '[submit-application] button "Submit application"',
                    ]
                )
            return "\n".join(
                [
                    '[dialog-root] dialog "Application submitted"',
                    'heading "Your application was sent"',
                ]
            )

        def click(self, ref: str) -> None:
            super().click(ref)
            if ref == "contact-next":
                self.stage = "resume"
            elif ref == "resume-next":
                self.stage = "screening"
            elif ref == "auth-yes":
                self.selected_auth = True
            elif ref == "screening-next" and self.selected_auth:
                self.stage = "review"
            elif ref == "submit-application":
                self.stage = "submitted"

        def evaluate_json(self, fn_source: str):
            if "__openclaw_linkedin_radio_groups_probe__" in fn_source and self.stage == "screening":
                return {
                    "probeKind": "__openclaw_linkedin_radio_groups_probe__",
                    "groups": [
                        {
                            "field_name": "state_or_province",
                            "group_label": "Are you authorized to work in the US without a sponsor visa? * Required",
                            "required": True,
                            "options": ["Yes", "No"],
                            "selected_option": "Yes" if self.selected_auth else None,
                            "selection_verified": self.selected_auth,
                            "chosen_option": "Yes" if self.selected_auth else None,
                            "refs_involved": ["auth-yes", "auth-no"],
                        }
                    ],
                }
            if "__openclaw_linkedin_radio_group_select__" in fn_source and self.stage == "screening":
                self.selected_auth = True
                return {
                    "probeKind": "__openclaw_linkedin_radio_group_select__",
                    "found": True,
                    "field_name": "work_authorization_us",
                    "group_label": "Are you authorized to work in the US without a sponsor visa? * Required",
                    "selection_attempted": True,
                    "selection_verified": True,
                    "chosen_option": "Yes",
                    "selected_option": "Yes",
                    "used_input_click": True,
                    "used_label_click": False,
                    "verification_method": "checked_state",
                    "refs_involved": ["auth-yes"],
                }
            return super().evaluate_json(fn_source)

    client = LinkedInLaterStepDomMisclassifiedRadioClient()
    result = run_backend(payload, client=client)

    assert result["failure_category"] is None
    assert client.click_calls.index("screening-next") > client.click_calls.index("resume-next")
    radio_groups = {row["field_name"]: row for row in result["page_diagnostics"]["later_step_radio_group_diagnostics"]}
    assert "state_or_province" not in radio_groups
    assert radio_groups["work_authorization_us"]["selected_option"] == "Yes"
    assert radio_groups["work_authorization_us"]["selection_verified"] is True
    resolution_rows = result["page_diagnostics"]["later_step_canonical_key_resolution"]
    assert any(
        row["resolved_field_name"] == "work_authorization_us"
        and row["resolution_reason"] == "keyword_match_work_authorization"
        for row in resolution_rows
    )
    strategy_rows = {row["field_name"]: row for row in result["page_diagnostics"]["later_step_radio_selection_strategy"]}
    assert strategy_rows["work_authorization_us"]["used_input_click"] is True
    assert strategy_rows["work_authorization_us"]["used_label_click"] is False
    assert result["page_diagnostics"]["later_step_continue_gate_reason"] == "all_required_later_step_fields_satisfied"


def test_backend_linkedin_later_step_blocks_unclassified_required_radio_group(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    _configure_linkedin_easy_apply_payload(payload, tmp_path)

    class LinkedInLaterStepUnclassifiedRadioClient(FakeBrowserClient):
        def __init__(self) -> None:
            super().__init__(
                page_title="Apply to Acme AI",
                current_url="https://www.linkedin.com/jobs/view/4354740729/",
                snapshots=[],
            )
            self.stage = "contact"

        def snapshot(self) -> str:
            if self.stage == "contact":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Contact info"',
                        '[contact-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "resume":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Resume"',
                        'generic "Selected"',
                        '[selected-resume] radio "Deselect resume Zachary Kralec Resume.pdf" checked',
                        '[resume-next] button "Continue to next step"',
                    ]
                )
            return "\n".join(
                [
                    '[dialog-root] dialog "Apply to Acme AI" [active]',
                    '[step-heading] heading "Voluntary self identification"',
                    'group "Do you prefer to work remote or on-site? * Required"',
                    '[pref-remote] radio "Remote"',
                    '[pref-onsite] radio "On-site"',
                    '[screening-next] button "Continue to next step"',
                ]
            )

        def click(self, ref: str) -> None:
            super().click(ref)
            if ref == "contact-next":
                self.stage = "resume"
            elif ref == "resume-next":
                self.stage = "screening"

    client = LinkedInLaterStepUnclassifiedRadioClient()
    result = run_backend(payload, client=client)

    assert result["source_status"] == "manual_review_required"
    assert result["blocking_reason"] == "unclassified_required_radio_group"
    assert "screening-next" not in client.click_calls
    radio_groups = result["page_diagnostics"]["later_step_radio_group_diagnostics"]
    assert any(row["field_name"] == "unclassified_radio_group" for row in radio_groups)
    assert result["page_diagnostics"]["later_step_continue_gate_reason"] == "unclassified_required_radio_group"


def test_backend_linkedin_later_step_blocks_when_stale_select_ref_cannot_reresolve(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    pdf_path = tmp_path / "resume.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%Test\n")
    payload["application_target"]["application_url"] = "https://www.linkedin.com/jobs/view/4354740729/apply/?openSDUIApplyFlow=true"
    payload["application_target"]["source_url"] = "https://www.linkedin.com/jobs/view/4354740729/"
    payload["resume_variant"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_file_name"] = "resume.pdf"
    payload["artifacts"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_variant_text"] = "Zachary Kralec\nzkralec@icloud.com\n240-555-0101\n"
    payload["contact_profile"] = {
        "city": "Saint Mary's City",
        "state_or_province": "MD",
        "postal_code": "20686",
        "country": "United States",
        "primary_phone_number": "240-555-0101",
        "phone_type": "mobile",
    }

    class LinkedInUnresolvedStaleSelectClient(FakeBrowserClient):
        def __init__(self) -> None:
            super().__init__(
                page_title="Apply to Acme AI",
                current_url="https://www.linkedin.com/jobs/view/4354740729/",
                snapshots=[],
            )
            self.stage = "contact"
            self.stale_triggered = False

        def snapshot(self) -> str:
            if self.stage == "contact":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Contact info"',
                        '[first-name] textbox "First name*": Zachary',
                        '[last-name] textbox "Last name*": Kralec',
                        '[email-address] combobox "Email address *": zkralec@icloud.com selected',
                        '[city-input] textbox "City*": Saint Mary\'s City',
                        '[state-input] textbox "State or Province*": MD',
                        '[zip-input] textbox "Zip/Postal Code*": 20686',
                        '[country-select] combobox "Country *": United States selected',
                        '[phone-input] textbox "Primary Phone Number*": 240-555-0101',
                        '[phone-type-mobile] radio "Mobile" checked',
                        '[contact-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "resume":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Resume"',
                        'generic "Selected"',
                        '[resume-file] heading "Zachary Kralec Resume 04_01_26.pdf"',
                        '[selected-resume] radio "Deselect resume Zachary Kralec Resume 04_01_26.pdf" checked',
                        '[resume-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "screening" and not self.stale_triggered:
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Additional questions"',
                        '[clearance-old] combobox "Do you currently hold an active government issued security clearance? *"',
                        '[screening-next] button "Continue to next step"',
                    ]
                )
            return "\n".join(
                [
                    '[dialog-root] dialog "Apply to Acme AI" [active]',
                    '[step-heading] heading "Additional questions"',
                    '[screening-next] button "Continue to next step"',
                ]
            )

        def click(self, ref: str) -> None:
            super().click(ref)
            if ref == "contact-next":
                self.stage = "resume"
            elif ref == "resume-next":
                self.stage = "screening"

        def select(self, ref: str, value: str) -> None:
            super().select(ref, value)
            if ref == "clearance-old" and not self.stale_triggered:
                self.stale_triggered = True
                raise BrowserCommandError(
                    failure_category="navigation_failed",
                    blocking_reason="OpenClaw browser command failed: select clearance-old No",
                    errors=["openclaw_browser_command_failed:1"],
                    stage="select_field",
                    error_kind="navigation_failure",
                    command_debug={
                        "stderr": 'Element "clearance-old" not found or not visible. Run a new snapshot to see current page elements.',
                        "stdout": "",
                        "args": ["select", "clearance-old", value],
                    },
                )

    client = LinkedInUnresolvedStaleSelectClient()
    result = run_backend(payload, client=client)

    assert result["failure_category"] == "manual_review_required"
    assert result["source_status"] == "manual_review_required"
    assert "re-resolve" in result["blocking_reason"].lower()
    action_diag = result["page_diagnostics"]["later_step_action_diagnostics"][0]
    assert action_diag["action_attempted"] == "select"
    assert action_diag["stale_ref_detected"] is True
    assert action_diag["retry_attempted"] is True
    assert action_diag["retry_succeeded"] is False
    assert action_diag["original_ref"] == "clearance-old"


def test_backend_linkedin_repeated_later_step_handoffs_before_timeout(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    pdf_path = tmp_path / "resume.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%Test\n")
    payload["application_target"]["application_url"] = "https://www.linkedin.com/jobs/view/4354740729/apply/?openSDUIApplyFlow=true"
    payload["application_target"]["source_url"] = "https://www.linkedin.com/jobs/view/4354740729/"
    payload["resume_variant"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_file_name"] = "resume.pdf"
    payload["artifacts"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_variant_text"] = "Zachary Kralec\nzkralec@icloud.com\n240-555-0101\n"
    payload["contact_profile"] = {
        "city": "Saint Mary's City",
        "state_or_province": "MD",
        "postal_code": "20686",
        "country": "United States",
        "primary_phone_number": "240-555-0101",
        "phone_type": "mobile",
    }

    class LinkedInNoProgressClient(FakeBrowserClient):
        def __init__(self) -> None:
            super().__init__(
                page_title="Apply to Acme AI",
                current_url="https://www.linkedin.com/jobs/view/4354740729/",
                snapshots=[],
            )
            self.stage = "contact"

        def snapshot(self) -> str:
            if self.stage == "contact":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Contact info"',
                        '[first-name] textbox "First name*": Zachary',
                        '[last-name] textbox "Last name*": Kralec',
                        '[email-address] combobox "Email address *": zkralec@icloud.com selected',
                        '[city-input] textbox "City*": Saint Mary\'s City',
                        '[state-input] textbox "State or Province*": MD',
                        '[zip-input] textbox "Zip/Postal Code*": 20686',
                        '[country-select] combobox "Country *": United States selected',
                        '[phone-input] textbox "Primary Phone Number*": 240-555-0101',
                        '[phone-type-mobile] radio "Mobile" checked',
                        '[contact-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "resume":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Resume"',
                        'generic "Selected"',
                        '[resume-file] heading "Zachary Kralec Resume 04_01_26.pdf"',
                        '[selected-resume] radio "Deselect resume Zachary Kralec Resume 04_01_26.pdf" checked',
                        '[resume-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "review":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Review your application"',
                        '[submit-application] button "Submit application"',
                    ]
                )
            return "\n".join(
                [
                    '[dialog-root] dialog "Apply to Acme AI" [active]',
                    '[step-heading] heading "Additional questions"',
                    '[auth] combobox "Are you authorized to work in the US without a sponsor visa? *"',
                    '[screening-next] button "Continue to next step"',
                ]
            )

        def click(self, ref: str) -> None:
            super().click(ref)
            if ref == "contact-next":
                self.stage = "resume"
            elif ref == "resume-next":
                self.stage = "screening"

    client = LinkedInNoProgressClient()
    result = run_backend(payload, client=client)

    assert result["source_status"] == "manual_review_required"
    assert result["failure_category"] == "manual_review_required"
    assert result["awaiting_review"] is True
    assert result["page_diagnostics"]["repeated_state_detected"] is True
    assert result["page_diagnostics"]["repeated_state_reason"] == "active_step_signature_unchanged_after_next_click"
    assert result["page_diagnostics"]["step_advance_blocking_reason"] == "active_step_signature_unchanged_after_next_click"
    assert result["page_diagnostics"]["later_step_iteration_count"] >= 1
    assert result["page_diagnostics"]["last_step_signature"]
    assert result["page_diagnostics"]["last_visible_labels"] == ["Are you authorized to work in the US without a sponsor visa? *"]
    assert result["page_diagnostics"]["last_action_attempted"] == "click"
    assert result["source_status"] != "timed_out"


def test_backend_linkedin_timeout_preserves_partial_later_step_diagnostics(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    pdf_path = tmp_path / "resume.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%Test\n")
    payload["application_target"]["application_url"] = "https://www.linkedin.com/jobs/view/4354740729/apply/?openSDUIApplyFlow=true"
    payload["application_target"]["source_url"] = "https://www.linkedin.com/jobs/view/4354740729/"
    payload["resume_variant"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_file_name"] = "resume.pdf"
    payload["artifacts"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_variant_text"] = "Zachary Kralec\nzkralec@icloud.com\n240-555-0101\n"
    payload["contact_profile"] = {
        "city": "Saint Mary's City",
        "state_or_province": "MD",
        "postal_code": "20686",
        "country": "United States",
        "primary_phone_number": "240-555-0101",
        "phone_type": "mobile",
    }

    class LinkedInTimedOutLaterStepClient(FakeBrowserClient):
        def __init__(self) -> None:
            super().__init__(
                page_title="Apply to Acme AI",
                current_url="https://www.linkedin.com/jobs/view/4354740729/",
                snapshots=[],
            )
            self.stage = "contact"

        def snapshot(self) -> str:
            if self.stage == "contact":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Contact info"',
                        '[first-name] textbox "First name*": Zachary',
                        '[last-name] textbox "Last name*": Kralec',
                        '[email-address] combobox "Email address *": zkralec@icloud.com selected',
                        '[city-input] textbox "City*": Saint Mary\'s City',
                        '[state-input] textbox "State or Province*": MD',
                        '[zip-input] textbox "Zip/Postal Code*": 20686',
                        '[country-select] combobox "Country *": United States selected',
                        '[phone-input] textbox "Primary Phone Number*": 240-555-0101',
                        '[phone-type-mobile] radio "Mobile" checked',
                        '[contact-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "resume":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Resume"',
                        'generic "Selected"',
                        '[resume-file] heading "Zachary Kralec Resume 04_01_26.pdf"',
                        '[selected-resume] radio "Deselect resume Zachary Kralec Resume 04_01_26.pdf" checked',
                        '[resume-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "review":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Review your application"',
                        '[submit-application] button "Submit application"',
                    ]
                )
            return "\n".join(
                [
                    '[dialog-root] dialog "Apply to Acme AI" [active]',
                    '[step-heading] heading "Additional questions"',
                    '[auth] combobox "Are you authorized to work in the US without a sponsor visa? *"',
                    '[screening-next] button "Continue to next step"',
                ]
            )

        def click(self, ref: str) -> None:
            super().click(ref)
            if ref == "contact-next":
                self.stage = "resume"
            elif ref == "resume-next":
                self.stage = "screening"
            elif ref == "screening-next":
                self.stage = "review"

        def select(self, ref: str, value: str) -> None:
            if ref == "auth":
                raise BrowserCommandError(
                    failure_category="timed_out",
                    blocking_reason="The browser runner hit its time budget before reaching a safe review checkpoint.",
                    errors=["openclaw_browser_command_timeout"],
                    safe_to_retry=True,
                    stage="select_field",
                    error_kind="command_timeout",
                    command_debug={"stderr": "", "stdout": "", "args": ["select", ref, value]},
                )
            super().select(ref, value)

    client = LinkedInTimedOutLaterStepClient()
    result = run_backend(payload, client=client)

    assert result["source_status"] == "timed_out"
    assert result["failure_category"] == "timed_out"
    assert result["page_diagnostics"]["later_step_iteration_count"] >= 1
    assert result["page_diagnostics"]["last_action_attempted"] == "select"
    assert result["page_diagnostics"]["last_field_targeted"] == "work_authorized_us"
    assert result["page_diagnostics"]["last_step_signature"]
    assert result["page_diagnostics"]["last_visible_labels"] == ["Are you authorized to work in the US without a sponsor visa? *"]


def test_backend_linkedin_review_like_step_stops_without_extra_probing(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    pdf_path = tmp_path / "resume.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%Test\n")
    payload["application_target"]["application_url"] = "https://www.linkedin.com/jobs/view/4354740729/apply/?openSDUIApplyFlow=true"
    payload["application_target"]["source_url"] = "https://www.linkedin.com/jobs/view/4354740729/"
    payload["resume_variant"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_file_name"] = "resume.pdf"
    payload["artifacts"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_variant_text"] = "Zachary Kralec\nzkralec@icloud.com\n240-555-0101\n"
    payload["contact_profile"] = {
        "city": "Saint Mary's City",
        "state_or_province": "MD",
        "postal_code": "20686",
        "country": "United States",
        "primary_phone_number": "240-555-0101",
        "phone_type": "mobile",
    }

    class LinkedInReviewLikeClient(FakeBrowserClient):
        def __init__(self) -> None:
            super().__init__(
                page_title="Apply to Acme AI",
                current_url="https://www.linkedin.com/jobs/view/4354740729/",
                snapshots=[],
            )
            self.stage = "contact"

        def snapshot(self) -> str:
            if self.stage == "contact":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Contact info"',
                        '[first-name] textbox "First name*": Zachary',
                        '[last-name] textbox "Last name*": Kralec',
                        '[email-address] combobox "Email address *": zkralec@icloud.com selected',
                        '[city-input] textbox "City*": Saint Mary\'s City',
                        '[state-input] textbox "State or Province*": MD',
                        '[zip-input] textbox "Zip/Postal Code*": 20686',
                        '[country-select] combobox "Country *": United States selected',
                        '[phone-input] textbox "Primary Phone Number*": 240-555-0101',
                        '[phone-type-mobile] radio "Mobile" checked',
                        '[contact-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "resume":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Resume"',
                        'generic "Selected"',
                        '[resume-file] heading "Zachary Kralec Resume 04_01_26.pdf"',
                        '[selected-resume] radio "Deselect resume Zachary Kralec Resume 04_01_26.pdf" checked',
                        '[resume-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "review_like":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Final review"',
                        '[submit-application] button "Submit application"',
                    ]
                )
            return "\n".join(
                [
                    '[dialog-root] dialog "Apply to Acme AI" [active]',
                    '[step-heading] heading "Application submitted"',
                    'text "Thank you for applying"',
                ]
            )

        def click(self, ref: str) -> None:
            super().click(ref)
            if ref == "contact-next":
                self.stage = "resume"
            elif ref == "resume-next":
                self.stage = "review_like"
            elif ref == "submit-application":
                self.stage = "submitted"

    client = LinkedInReviewLikeClient()
    result = run_backend(payload, client=client)

    assert result["failure_category"] is None
    assert result["draft_status"] == "draft_ready"
    assert result["awaiting_review"] is False
    assert result["submitted"] is True
    assert client.click_calls == ["contact-next", "resume-next", "submit-application"]
    assert result["page_diagnostics"]["review_like_step_detected"] is True
    assert result["page_diagnostics"]["auto_submit_succeeded"] is True
    assert result["page_diagnostics"]["should_auto_submit"] is True


def test_backend_linkedin_later_step_uses_truthful_personal_fallback_when_required_and_no_neutral_exists(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    pdf_path = tmp_path / "resume.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%Test\n")
    payload["application_target"]["application_url"] = "https://www.linkedin.com/jobs/view/4354740729/apply/?openSDUIApplyFlow=true"
    payload["application_target"]["source_url"] = "https://www.linkedin.com/jobs/view/4354740729/"
    payload["resume_variant"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_file_name"] = "resume.pdf"
    payload["artifacts"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_variant_text"] = (
        "Zachary Kralec\n"
        "zkralec@icloud.com\n"
        "240-555-0101\n"
    )
    payload["contact_profile"] = {
        "city": "Saint Mary's City",
        "state_or_province": "MD",
        "postal_code": "20686",
        "country": "United States",
        "primary_phone_number": "240-555-0101",
        "phone_type": "mobile",
    }

    class LinkedInUnsafeSelfIdClient(FakeBrowserClient):
        def __init__(self) -> None:
            super().__init__(
                page_title="Apply to Acme AI",
                current_url="https://www.linkedin.com/jobs/view/4354740729/",
                snapshots=[],
            )
            self.stage = "contact"
            self.veteran_selected = False

        def snapshot(self) -> str:
            if self.stage == "contact":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Contact info"',
                        '[first-name] textbox "First name*": Zachary',
                        '[last-name] textbox "Last name*": Kralec',
                        '[email-address] combobox "Email address *": zkralec@icloud.com selected',
                        '[city-input] textbox "City*": Saint Mary\'s City',
                        '[state-input] textbox "State or Province*": MD',
                        '[zip-input] textbox "Zip/Postal Code*": 20686',
                        '[country-select] combobox "Country *": United States selected',
                        '[phone-input] textbox "Primary Phone Number*": 240-555-0101',
                        '[phone-type-mobile] radio "Mobile" checked',
                        '[contact-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "resume":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Resume"',
                        'generic "Selected"',
                        '[resume-file] heading "Zachary Kralec Resume 04_01_26.pdf"',
                        '[selected-resume] radio "Deselect resume Zachary Kralec Resume 04_01_26.pdf" checked',
                        '[resume-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "review":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Review your application"',
                        '[submit-application] button "Submit application"',
                    ]
                )
            if self.stage == "submitted":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Application submitted"',
                        'text "Thank you for applying"',
                    ]
                )
            return "\n".join(
                [
                    '[dialog-root] dialog "Apply to Acme AI" [active]',
                    '[step-heading] heading "Additional questions"',
                    '[auth] combobox "Are you authorized to work in the US without a sponsor visa? *"',
                    '[veteran-yes] radio "Protected veteran *"',
                    '[veteran-no] radio "Not a protected veteran *" checked' if self.veteran_selected else '[veteran-no] radio "Not a protected veteran *"',
                    '[screening-next] button "Continue to next step"',
                ]
            )

        def click(self, ref: str) -> None:
            super().click(ref)
            if ref == "contact-next":
                self.stage = "resume"
            elif ref == "resume-next":
                self.stage = "screening"
            elif ref == "veteran-no":
                self.veteran_selected = True
            elif ref == "screening-next" and self.veteran_selected:
                self.stage = "review"
            elif ref == "submit-application":
                self.stage = "submitted"

    client = LinkedInUnsafeSelfIdClient()
    result = run_backend(payload, client=client)

    assert result["draft_status"] == "draft_ready"
    assert result["source_status"] == "success"
    assert result["failure_category"] is None
    assert result["awaiting_review"] is True
    assert result["submitted"] is False
    assert ("auth", "Yes") in client.select_calls
    assert "veteran-no" in client.click_calls
    assert client.click_calls == ["contact-next", "resume-next", "veteran-no"]
    fallback_rows = result["form_diagnostics"]["later_step_personal_answer_fallbacks_used"]
    assert fallback_rows[0]["canonical_key"] == "veteran_status"
    assert fallback_rows[0]["value"] == "Not a veteran"
    assert "unsafe_personal_fallback_answers_present" in result["page_diagnostics"]["submit_confidence_reasons"]


def test_backend_linkedin_later_step_stops_at_review_when_personal_fallback_is_uncertain(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    pdf_path = tmp_path / "resume.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%Test\n")
    payload["application_target"]["application_url"] = "https://www.linkedin.com/jobs/view/4354740729/apply/?openSDUIApplyFlow=true"
    payload["application_target"]["source_url"] = "https://www.linkedin.com/jobs/view/4354740729/"
    payload["resume_variant"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_file_name"] = "resume.pdf"
    payload["artifacts"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_variant_text"] = (
        "Zachary Kralec\n"
        "zkralec@icloud.com\n"
        "240-555-0101\n"
    )
    payload["contact_profile"] = {
        "city": "Saint Mary's City",
        "state_or_province": "MD",
        "postal_code": "20686",
        "country": "United States",
        "primary_phone_number": "240-555-0101",
        "phone_type": "mobile",
    }

    class LinkedInUncertainPersonalAnswerClient(FakeBrowserClient):
        def __init__(self) -> None:
            super().__init__(
                page_title="Apply to Acme AI",
                current_url="https://www.linkedin.com/jobs/view/4354740729/",
                snapshots=[],
            )
            self.stage = "contact"

        def snapshot(self) -> str:
            if self.stage == "contact":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Contact info"',
                        '[first-name] textbox "First name*": Zachary',
                        '[last-name] textbox "Last name*": Kralec',
                        '[email-address] combobox "Email address *": zkralec@icloud.com selected',
                        '[city-input] textbox "City*": Saint Mary\'s City',
                        '[state-input] textbox "State or Province*": MD',
                        '[zip-input] textbox "Zip/Postal Code*": 20686',
                        '[country-select] combobox "Country *": United States selected',
                        '[phone-input] textbox "Primary Phone Number*": 240-555-0101',
                        '[phone-type-mobile] radio "Mobile" checked',
                        '[contact-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "resume":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to Acme AI" [active]',
                        '[step-heading] heading "Resume"',
                        'generic "Selected"',
                        '[resume-file] heading "Zachary Kralec Resume 04_01_26.pdf"',
                        '[selected-resume] radio "Deselect resume Zachary Kralec Resume 04_01_26.pdf" checked',
                        '[resume-next] button "Continue to next step"',
                    ]
                )
            return "\n".join(
                [
                    '[dialog-root] dialog "Apply to Acme AI" [active]',
                    '[step-heading] heading "Additional questions"',
                    '[orientation-a] radio "Sexual orientation: Option A *"',
                    '[orientation-b] radio "Sexual orientation: Option B *"',
                    '[screening-next] button "Continue to next step"',
                ]
            )

        def click(self, ref: str) -> None:
            super().click(ref)
            if ref == "contact-next":
                self.stage = "resume"
            elif ref == "resume-next":
                self.stage = "screening"

    client = LinkedInUncertainPersonalAnswerClient()
    result = run_backend(payload, client=client)

    assert result["draft_status"] == "partial_draft"
    assert result["source_status"] == "manual_review_required"
    assert result["failure_category"] == "manual_review_required"
    assert result["awaiting_review"] is True
    missing_reasons = {row["reason"] for row in result["form_diagnostics"]["missing_required_fields"]}
    assert "required_personal_answer_fallback_unmatched" in missing_reasons or "ambiguous_required_self_id_field" in missing_reasons
    assert result["page_diagnostics"]["should_auto_submit"] is False


def test_backend_linkedin_job_page_without_modal_is_not_misclassified_as_login_required(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["auth"] = {"session_available": False}
    payload["application_target"]["application_url"] = "https://www.linkedin.com/jobs/view/4354740729/apply/?openSDUIApplyFlow=true"
    payload["application_target"]["source_url"] = "https://www.linkedin.com/jobs/view/4354740729/"

    class StaticJobPageClient(FakeBrowserClient):
        def snapshot(self) -> str:
            return "\n".join(
                [
                    '[nav-network] link "My Network"',
                    '[nav-messaging] link "Messaging"',
                    '[nav-notifications] link "Notifications"',
                    '[easy-apply] button "Easy Apply"',
                    '[show-more] button "Show more"',
                ]
            )

    client = StaticJobPageClient(
        page_title="The Amatriot Group hiring Software Developer",
        current_url="https://www.linkedin.com/jobs/view/4354740729/",
        snapshots=[],
    )

    result = run_backend(payload, client=client)

    assert result["failure_category"] == "manual_review_required"
    assert result["blocking_reason"] == "LinkedIn opened the job page, but the Easy Apply dialog did not mount."
    assert "easy_apply_modal_not_mounted" in result["errors"]
    assert result["page_diagnostics"]["linkedin_state"] == "job_page_easy_apply_visible"
    assert result["page_diagnostics"]["apply_modal_not_mounted"] is True
    assert result["page_diagnostics"]["linkedin_nav_visible"] is True
    assert result["page_diagnostics"]["easy_apply_dialog_exists"] is False
    assert result["page_diagnostics"]["upload_input_exists"] is False
    assert result["page_diagnostics"]["login_or_checkpoint_markers_present"] is False


def test_backend_linkedin_contact_step_detects_candidates_without_upload_ref(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["application_target"]["application_url"] = "https://www.linkedin.com/jobs/view/4354740729/apply/?openSDUIApplyFlow=true"
    payload["application_target"]["source_url"] = "https://www.linkedin.com/jobs/view/4354740729/"
    pdf_path = tmp_path / "resume.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%Test\n")
    payload["resume_variant"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_file_name"] = "resume.pdf"
    payload["artifacts"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_variant_text"] = (
        "Zachary Kralec\n"
        "zkralec@icloud.com\n"
        "240-555-0101\n"
    )
    payload["contact_profile"] = {
        "city": "Saint Mary's City",
        "state_or_province": "MD",
        "postal_code": "20686",
        "country": "United States",
        "primary_phone_number": "240-555-0101",
        "phone_type": "mobile",
    }
    client = FakeBrowserClient(
        page_title="Apply to The Amatriot Group",
        current_url="https://www.linkedin.com/jobs/view/4354740729/",
        snapshots=[
            "\n".join(
                [
                    '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                    '[step-heading] heading "Contact info"',
                    '[first-name] textbox "First name*": Zachary',
                    '[last-name] textbox "Last name*": Kralec',
                    '[email-address] combobox "Email address *": zkralec@icloud.com selected',
                    '[city-input] textbox "City*"',
                    '[state-input] textbox "State or Province*"',
                    '[zip-input] textbox "Zip/Postal Code*"',
                    '[country-select] combobox "Country *"',
                    '[phone-input] textbox "Primary Phone Number*"',
                    '[phone-type-group] group "Type * Required"',
                    '[phone-type-mobile] radio "Mobile"',
                    '[phone-type-home] radio "Home"',
                ]
            ),
            "\n".join(
                [
                    '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                    '[step-heading] heading "Contact info"',
                    '[first-name] textbox "First name*": Zachary',
                    '[last-name] textbox "Last name*": Kralec',
                    '[email-address] combobox "Email address *": zkralec@icloud.com selected',
                    '[city-input] textbox "City*": Saint Mary\'s City',
                    '[state-input] textbox "State or Province*": MD',
                    '[zip-input] textbox "Zip/Postal Code*": 20686',
                    '[country-select] combobox "Country *": United States selected',
                    '[phone-input] textbox "Primary Phone Number*": 240-555-0101',
                    '[phone-type-mobile] radio "Mobile" checked',
                ]
            ),
        ],
    )

    result = run_backend(payload, client=client)

    assert result["failure_category"] is None
    assert result["draft_status"] == "draft_ready"
    assert "linkedin_contact_step_not_advanced" in result["warnings"]
    assert result["page_diagnostics"]["easy_apply_dialog_exists"] is True
    assert result["page_diagnostics"]["linkedin_state"] == "easy_apply_contact_info_step"
    assert result["page_diagnostics"]["upload_input_exists"] is False
    assert result["form_diagnostics"]["detected_ref_count"] >= 8
    assert result["form_diagnostics"]["fill_candidate_count"] > 0
    assert "City*" in result["form_diagnostics"]["detected_labels"]
    assert "Primary Phone Number*" in result["form_diagnostics"]["detected_labels"]
    assert client.upload_calls == []
    required_statuses = {row["field_name"]: row for row in result["form_diagnostics"]["required_field_statuses"]}
    assert required_statuses["country"]["satisfied"] is True
    assert required_statuses["phone_type"]["satisfied"] is True
    radio_groups = {row["field_name"]: row for row in result["form_diagnostics"]["radio_group_diagnostics"]}
    assert radio_groups["phone_type"]["group_label"] == "Type * Required"
    assert radio_groups["phone_type"]["selected_option"] == "Mobile"
    assert radio_groups["phone_type"]["selection_verified"] is True
    assert any(field["ref"] == "city-input" for batch in client.fill_calls for field in batch)
    skipped_reasons = {row["reason"] for row in result["form_diagnostics"]["skipped_fields"]}
    assert "no_matching_phone_type_value" not in skipped_reasons


def test_backend_linkedin_multi_step_progresses_to_resume_before_upload(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["application_target"]["application_url"] = "https://www.linkedin.com/jobs/view/4354740729/apply/?openSDUIApplyFlow=true"
    payload["application_target"]["source_url"] = "https://www.linkedin.com/jobs/view/4354740729/"
    pdf_path = tmp_path / "resume.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%Test\n")
    payload["resume_variant"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_file_name"] = "resume.pdf"
    payload["artifacts"]["resume_upload_path"] = str(pdf_path)
    payload["contact_profile"] = {
        "city": "Saint Mary's City",
        "state_or_province": "MD",
        "postal_code": "20686",
        "country": "United States",
        "primary_phone_number": "240-555-0101",
        "phone_type": "mobile",
    }

    class LinkedInProgressionClient(FakeBrowserClient):
        def __init__(self) -> None:
            super().__init__(
                page_title="The Amatriot Group hiring Software Developer",
                current_url="https://www.linkedin.com/jobs/view/4354740729/",
                snapshots=[],
            )
            self.stage = "job_page"
            self.upload_stages: list[str] = []
            self.contact_values: dict[str, object] = {
                "city": "",
                "state": "",
                "zip": "",
                "country": "",
                "phone": "",
            }
            self.phone_type_selected = False

        def snapshot(self) -> str:
            if self.stage == "job_page":
                return "\n".join(
                    [
                        '[nav-network] link "My Network"',
                        '[nav-messaging] link "Messaging"',
                        '[nav-notifications] link "Notifications"',
                        '[easy-apply-trigger] button "Easy Apply"',
                    ]
                )
            if self.stage == "contact":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                        '[step-heading] heading "Contact info"',
                        f'[city-input] textbox "City*"{": " + str(self.contact_values["city"]) if self.contact_values["city"] else ""}',
                        f'[state-input] textbox "State or Province*"{": " + str(self.contact_values["state"]) if self.contact_values["state"] else ""}',
                        f'[zip-input] textbox "Zip/Postal Code*"{": " + str(self.contact_values["zip"]) if self.contact_values["zip"] else ""}',
                        f'[country-select] combobox "Country *"{": " + str(self.contact_values["country"]) + " selected" if self.contact_values["country"] else ""}',
                        f'[phone-input] textbox "Primary Phone Number*"{": " + str(self.contact_values["phone"]) if self.contact_values["phone"] else ""}',
                        f'[phone-type-mobile] radio "Mobile"{" checked" if self.phone_type_selected else ""}',
                        '[next-contact] button "Next"',
                    ]
                )
            if self.stage == "contact_country_open":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                        '[step-heading] heading "Contact info"',
                        f'[city-input] textbox "City*": {self.contact_values["city"]}',
                        f'[state-input] textbox "State or Province*": {self.contact_values["state"]}',
                        f'[zip-input] textbox "Zip/Postal Code*": {self.contact_values["zip"]}',
                        '[country-select] combobox "Country *"',
                        '- option "UNITED STATES"',
                        f'[phone-input] textbox "Primary Phone Number*": {self.contact_values["phone"]}',
                        f'[phone-type-mobile] radio "Mobile"{" checked" if self.phone_type_selected else ""}',
                        '[next-contact] button "Next"',
                    ]
                )
            if self.stage == "resume":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                        '[step-heading] heading "Resume"',
                        '[resume-upload] input "Upload resume"',
                    ]
                )
            return "\n".join(
                [
                    '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                    '[step-heading] heading "Review your application"',
                    '[review-note] note "Review your application before submitting"',
                ]
            )

        def click(self, ref: str) -> None:
            super().click(ref)
            if ref == "easy-apply-trigger":
                self.stage = "contact"
                self.page_title = "Apply to The Amatriot Group"
            elif ref == "phone-type-mobile":
                self.phone_type_selected = True
                self.stage = "contact"
            elif ref == "next-contact":
                self.stage = "resume"

        def fill(self, fields: list[dict]) -> None:
            super().fill(fields)
            ref_map = {
                "city-input": "city",
                "state-input": "state",
                "zip-input": "zip",
                "phone-input": "phone",
            }
            for row in fields:
                key = ref_map.get(str(row["ref"]))
                if key:
                    self.contact_values[key] = row["value"]
            self.stage = "contact"

        def select(self, ref: str, value: str) -> None:
            super().select(ref, value)
            if ref == "country-select":
                assert value == "UNITED STATES"
                self.contact_values["country"] = "United States"
                self.stage = "contact"

        def evaluate_json(self, fn_source: str):
            if "__openclaw_native_select_probe__" in fn_source:
                return {
                    "probeKind": "__openclaw_native_select_probe__",
                    "isNativeSelect": True,
                    "detectedFieldType": "select",
                    "optionCount": 2,
                    "matchedOptionValue": "UNITED STATES",
                    "matchedOptionLabel": "UNITED STATES",
                    "fieldLabelText": "Country",
                }
            return super().evaluate_json(fn_source)

        def upload(self, staged_path: Path, *, input_ref: str | None = None) -> None:
            self.upload_stages.append(self.stage)
            super().upload(staged_path, input_ref=input_ref)
            self.stage = "later"

    client = LinkedInProgressionClient()
    result = run_backend(payload, client=client)

    assert result["failure_category"] is None
    assert result["draft_status"] == "draft_ready"
    assert client.click_calls == ["easy-apply-trigger", "phone-type-mobile", "next-contact"]
    assert ("country-select", "UNITED STATES") in client.select_calls
    assert client.upload_stages == ["resume"]
    assert client.upload_calls == [("resume-upload", "run-1.pdf")]
    assert result["page_diagnostics"]["linkedin_state"] == "easy_apply_review_step"
    assert [row["action"] for row in result["debug_json"]["linkedin_progression"]] == [
        "click_easy_apply",
        "fill_contact_info",
        "click_next",
        "upload_resume",
    ]


def test_backend_linkedin_contact_step_uses_explicit_continue_ref_when_only_optional_fields_remain(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["application_target"]["application_url"] = "https://www.linkedin.com/jobs/view/4354740729/apply/?openSDUIApplyFlow=true"
    payload["application_target"]["source_url"] = "https://www.linkedin.com/jobs/view/4354740729/"
    pdf_path = tmp_path / "resume.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%Test\n")
    payload["resume_variant"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_file_name"] = "resume.pdf"
    payload["artifacts"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_variant_text"] = (
        "Zachary Kralec\n"
        "zkralec@icloud.com\n"
        "240-555-0101\n"
    )
    payload["contact_profile"] = {
        "city": "Saint Mary's City",
        "state_or_province": "MD",
        "postal_code": "20686",
        "country": "United States",
        "primary_phone_number": "240-555-0101",
        "phone_type": "mobile",
    }

    class LinkedInOptionalFieldsClient(FakeBrowserClient):
        def __init__(self) -> None:
            super().__init__(
                page_title="The Amatriot Group hiring Software Developer",
                current_url="https://www.linkedin.com/jobs/view/4354740729/",
                snapshots=[],
            )
            self.stage = "job_page"
            self.contact_values: dict[str, object] = {
                "country": "",
            }
            self.phone_type_selected = False

        def snapshot(self) -> str:
            if self.stage == "job_page":
                return "\n".join(
                    [
                        '[nav-network] link "My Network"',
                        '[nav-messaging] link "Messaging"',
                        '[nav-notifications] link "Notifications"',
                        '[easy-apply-trigger] button "Easy Apply"',
                    ]
                )
            if self.stage == "contact":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                        '[step-heading] heading "Contact info"',
                        '[first-name] textbox "First name*": Zachary',
                        '[last-name] textbox "Last name*": Kralec',
                        '[email-address] combobox "Email address *": zkralec@icloud.com selected',
                        '[city-input] textbox "City*"',
                        '[state-input] textbox "State or Province*"',
                        '[zip-input] textbox "Zip/Postal Code*"',
                        f'[country-select] combobox "Country *"{": " + str(self.contact_values["country"]) + " selected" if self.contact_values["country"] else ""}',
                        '[phone-input] textbox "Primary Phone Number*"',
                        f'[phone-type-mobile] radio "Mobile"{" checked" if self.phone_type_selected else ""}',
                        '[phone-type-home] radio "Home"',
                        '[secondary-phone] textbox "Secondary Phone Number"',
                        '[secondary-type-mobile] radio "Mobile"',
                        '[secondary-type-home] radio "Home"',
                        '[continue-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "contact_country_open":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                        '[step-heading] heading "Contact info"',
                        '[first-name] textbox "First name*": Zachary',
                        '[last-name] textbox "Last name*": Kralec',
                        '[email-address] combobox "Email address *": zkralec@icloud.com selected',
                        '[city-input] textbox "City*": Saint Mary\'s City',
                        '[state-input] textbox "State or Province*": MD',
                        '[zip-input] textbox "Zip/Postal Code*": 20686',
                        '[country-select] combobox "Country *"',
                        '[country-option-us] option "United States"',
                        '[phone-input] textbox "Primary Phone Number*": 240-555-0101',
                        '[phone-type-mobile] radio "Mobile" checked',
                        '[continue-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "resume":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                        '[step-heading] heading "Resume"',
                        '[resume-upload] input "Upload resume"',
                    ]
                )
            return "\n".join(
                [
                    '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                    '[step-heading] heading "Review your application"',
                    '[review-note] note "Review your application before submitting"',
                ]
            )

        def click(self, ref: str) -> None:
            super().click(ref)
            if ref == "easy-apply-trigger":
                self.stage = "contact"
                self.page_title = "Apply to The Amatriot Group"
            elif ref == "phone-type-mobile":
                self.phone_type_selected = True
                self.stage = "contact"
            elif ref == "continue-next":
                self.stage = "resume"

        def select(self, ref: str, value: str) -> None:
            super().select(ref, value)
            if ref == "country-select":
                assert value == "UNITED STATES"
                self.contact_values["country"] = "United States"
                self.stage = "contact"

        def evaluate_json(self, fn_source: str):
            if "__openclaw_native_select_probe__" in fn_source:
                return {
                    "probeKind": "__openclaw_native_select_probe__",
                    "isNativeSelect": True,
                    "detectedFieldType": "select",
                    "optionCount": 2,
                    "matchedOptionValue": "UNITED STATES",
                    "matchedOptionLabel": "UNITED STATES",
                    "fieldLabelText": "Country",
                }
            return super().evaluate_json(fn_source)

        def upload(self, staged_path: Path, *, input_ref: str | None = None) -> None:
            super().upload(staged_path, input_ref=input_ref)
            self.stage = "later"

    client = LinkedInOptionalFieldsClient()
    result = run_backend(payload, client=client)

    assert result["failure_category"] is None
    assert result["draft_status"] == "draft_ready"
    assert client.click_calls == ["easy-apply-trigger", "phone-type-mobile", "continue-next"]
    assert ("country-select", "UNITED STATES") in client.select_calls
    assert result["page_diagnostics"]["next_button_ref"] == "continue-next"
    assert result["page_diagnostics"]["next_button_label"] == 'button "Continue to next step"'
    assert result["page_diagnostics"]["next_button_clicked"] is True
    assert result["page_diagnostics"]["next_button_not_clicked_reason"] is None
    assert result["page_diagnostics"]["blocking_skipped_fields"] == []
    assert result["debug_json"]["linkedin_progression"][2]["chosen_ref"] == "continue-next"


def test_backend_linkedin_contact_step_refreshes_snapshot_before_reusing_changed_refs(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["application_target"]["application_url"] = "https://www.linkedin.com/jobs/view/4354740729/apply/?openSDUIApplyFlow=true"
    payload["application_target"]["source_url"] = "https://www.linkedin.com/jobs/view/4354740729/"
    pdf_path = tmp_path / "resume.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%Test\n")
    payload["resume_variant"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_file_name"] = "resume.pdf"
    payload["artifacts"]["resume_upload_path"] = str(pdf_path)

    class LinkedInRerenderClient(FakeBrowserClient):
        def __init__(self) -> None:
            super().__init__(
                page_title="The Amatriot Group hiring Software Developer",
                current_url="https://www.linkedin.com/jobs/view/4354740729/",
                snapshots=[],
            )
            self.stage = "job_page"

        def snapshot(self) -> str:
            if self.stage == "job_page":
                return "\n".join(
                    [
                        '[nav-network] link "My Network"',
                        '[nav-messaging] link "Messaging"',
                        '[nav-notifications] link "Notifications"',
                        '[easy-apply-trigger] button "Easy Apply"',
                    ]
                )
            if self.stage == "contact_initial":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                        '[step-heading] heading "Contact info"',
                        '[first-name] textbox "First name*": Zachary',
                        '[last-name] textbox "Last name*": Kralec',
                        '[email-select] combobox "Email address *"',
                        '[city-input] textbox "City*": Monkton',
                        '[state-input] textbox "State or Province*": MD',
                        '[zip-input] textbox "Zip/Postal Code*": 21111',
                        '[country-old] combobox "Country *"',
                        '[phone-input] textbox "Primary Phone Number*": 4104566443',
                        '[phone-type-mobile] radio "Mobile" checked',
                        '[next-contact] button "Next"',
                    ]
                )
            if self.stage == "contact_after_email":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                        '[step-heading] heading "Contact info"',
                        '[first-name] textbox "First name*": Zachary',
                        '[last-name] textbox "Last name*": Kralec',
                        '[email-select] combobox "Email address *": zkralec@icloud.com selected',
                        '[city-input] textbox "City*": Monkton',
                        '[state-input] textbox "State or Province*": MD',
                        '[zip-input] textbox "Zip/Postal Code*": 21111',
                        '[country-new] combobox "Country *"',
                        '[phone-input] textbox "Primary Phone Number*": 4104566443',
                        '[phone-type-mobile] radio "Mobile" checked',
                        '[next-contact] button "Next"',
                    ]
                )
            if self.stage == "contact_after_country":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                        '[step-heading] heading "Contact info"',
                        '[first-name] textbox "First name*": Zachary',
                        '[last-name] textbox "Last name*": Kralec',
                        '[email-select] combobox "Email address *": zkralec@icloud.com selected',
                        '[city-input] textbox "City*": Monkton',
                        '[state-input] textbox "State or Province*": MD',
                        '[zip-input] textbox "Zip/Postal Code*": 21111',
                        '[country-new] combobox "Country *": United States selected',
                        '[phone-input] textbox "Primary Phone Number*": 4104566443',
                        '[phone-type-mobile] radio "Mobile" checked',
                        '[next-contact] button "Next"',
                    ]
                )
            if self.stage == "resume":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                        '[step-heading] heading "Resume"',
                        '[resume-upload] input "Upload resume"',
                    ]
                )
            return "\n".join(
                [
                    '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                    '[step-heading] heading "Review your application"',
                ]
            )

        def click(self, ref: str) -> None:
            super().click(ref)
            if ref == "easy-apply-trigger":
                self.stage = "contact_initial"
                self.page_title = "Apply to The Amatriot Group"
            elif ref == "next-contact":
                self.stage = "resume"

        def select(self, ref: str, value: str) -> None:
            super().select(ref, value)
            if ref == "email-select":
                self.stage = "contact_after_email"
            elif ref == "country-old":
                raise AssertionError("stale country ref should not be reused")
            elif ref == "country-new":
                assert value == "UNITED STATES"
                self.stage = "contact_after_country"

        def evaluate_json(self, fn_source: str):
            if "__openclaw_native_select_probe__" in fn_source:
                return {
                    "probeKind": "__openclaw_native_select_probe__",
                    "isNativeSelect": True,
                    "detectedFieldType": "select",
                    "optionCount": 2,
                    "matchedOptionValue": "UNITED STATES",
                    "matchedOptionLabel": "UNITED STATES",
                    "fieldLabelText": "Country",
                }
            return super().evaluate_json(fn_source)

        def upload(self, staged_path: Path, *, input_ref: str | None = None) -> None:
            super().upload(staged_path, input_ref=input_ref)
            self.stage = "review"

    client = LinkedInRerenderClient()
    result = run_backend(payload, client=client)

    assert result["failure_category"] is None
    assert client.select_calls == [("email-select", "zkralec@icloud.com"), ("country-new", "UNITED STATES")]
    assert client.click_calls[0] == "easy-apply-trigger"
    assert client.click_calls[-1] == "next-contact"
    assert result["page_diagnostics"]["contact_snapshot_refreshed"] is True
    assert result["page_diagnostics"]["contact_snapshot_refresh_count"] >= 2
    refreshes = result["page_diagnostics"]["contact_snapshot_refreshes"]
    assert refreshes[0]["trigger_operation"] == "select"
    assert refreshes[0]["executed_ref"] == "email-select"
    assert "country-new" in refreshes[0]["re_resolved_refs"]
    interactions = result["page_diagnostics"]["contact_field_interactions"]
    assert interactions[-1]["interaction_type"] == "select"
    assert interactions[-1]["detected_field_type"] == "select"
    assert interactions[-1]["select_value_normalized"] == "UNITED STATES"
    assert interactions[-1]["select_success"] is True


def test_backend_linkedin_contact_step_reports_blocking_required_fields_before_next_click(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["application_target"]["application_url"] = "https://www.linkedin.com/jobs/view/4354740729/apply/?openSDUIApplyFlow=true"
    payload["application_target"]["source_url"] = "https://www.linkedin.com/jobs/view/4354740729/"
    pdf_path = tmp_path / "resume.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%Test\n")
    payload["resume_variant"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_file_name"] = "resume.pdf"
    payload["artifacts"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_variant_text"] = (
        "Zachary Kralec\n"
        "zkralec@icloud.com\n"
    )
    payload["default_answer_profile"] = {
        "city": "",
        "state": "",
        "state_abbrev": "",
        "state_or_province": "",
        "postal_code": "",
        "zip": "",
        "country": "",
        "phone": "",
        "primary_phone_number": "",
        "phone_type": "",
    }
    client = FakeBrowserClient(
        page_title="Apply to The Amatriot Group",
        current_url="https://www.linkedin.com/jobs/view/4354740729/",
        snapshots=[
            "\n".join(
                [
                    '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                    '[step-heading] heading "Contact info"',
                    '[first-name] textbox "First name*": Zachary',
                    '[last-name] textbox "Last name*": Kralec',
                    '[email-address] combobox "Email address *": zkralec@icloud.com selected',
                    '[city-input] textbox "City*"',
                    '[state-input] textbox "State or Province*"',
                    '[zip-input] textbox "Zip/Postal Code*"',
                    '[country-select] combobox "Country *"',
                    '[phone-input] textbox "Primary Phone Number*"',
                    '[phone-type-mobile] radio "Mobile"',
                    '[phone-type-home] radio "Home"',
                    '[secondary-phone] textbox "Secondary Phone Number"',
                    '[continue-next] button "Continue to next step"',
                ]
            ),
        ],
    )

    result = run_backend(payload, client=client)

    assert result["draft_status"] == "draft_ready"
    assert result["page_diagnostics"]["next_button_ref"] == "continue-next"
    assert result["page_diagnostics"]["next_button_clicked"] is False
    assert result["page_diagnostics"]["next_button_not_clicked_reason"] == "blocking_required_contact_fields"
    blocking_labels = {row["label"] for row in result["page_diagnostics"]["blocking_skipped_fields"]}
    assert {"City*", "State or Province*", "Zip/Postal Code*", "Country *", "Primary Phone Number*", "Mobile"} <= blocking_labels
    assert client.click_calls == []


def test_backend_linkedin_contact_step_requires_verified_phone_type_radio_before_continue(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["application_target"]["application_url"] = "https://www.linkedin.com/jobs/view/4354740729/apply/?openSDUIApplyFlow=true"
    payload["application_target"]["source_url"] = "https://www.linkedin.com/jobs/view/4354740729/"
    payload["resume_variant"]["resume_variant_text"] = (
        "Zachary Kralec\n"
        "zkralec@icloud.com\n"
        "240-555-0101\n"
    )
    payload["contact_profile"] = {
        "city": "Saint Mary's City",
        "state_or_province": "MD",
        "postal_code": "20686",
        "country": "United States",
        "primary_phone_number": "240-555-0101",
        "phone_type": "mobile",
    }

    class LinkedInRadioNotVerifiedClient(FakeBrowserClient):
        def __init__(self) -> None:
            super().__init__(
                page_title="The Amatriot Group hiring Software Developer",
                current_url="https://www.linkedin.com/jobs/view/4354740729/",
                snapshots=[],
            )
            self.stage = "job_page"

        def snapshot(self) -> str:
            if self.stage == "job_page":
                return "\n".join(
                    [
                        '[nav-network] link "My Network"',
                        '[nav-messaging] link "Messaging"',
                        '[nav-notifications] link "Notifications"',
                        '[easy-apply-trigger] button "Easy Apply"',
                    ]
                )
            return "\n".join(
                [
                    '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                    '[step-heading] heading "Contact info"',
                    '[first-name] textbox "First name*": Zachary',
                    '[last-name] textbox "Last name*": Kralec',
                    '[email-address] combobox "Email address *": zkralec@icloud.com selected',
                    '[city-input] textbox "City*": Saint Mary\'s City',
                    '[state-input] textbox "State or Province*": MD',
                    '[zip-input] textbox "Zip/Postal Code*": 20686',
                    '[country-select] combobox "Country *": United States selected',
                    '[phone-input] textbox "Primary Phone Number*": 240-555-0101',
                    '[phone-type-group] group "Type * Required"',
                    '[phone-type-mobile] radio "Mobile"',
                    '[phone-type-home] radio "Home"',
                    '[continue-next] button "Continue to next step"',
                ]
            )

        def click(self, ref: str) -> None:
            super().click(ref)
            if ref == "easy-apply-trigger":
                self.stage = "contact"

    client = LinkedInRadioNotVerifiedClient()
    result = run_backend(payload, client=client)

    assert result["draft_status"] == "draft_ready"
    assert result["page_diagnostics"]["linkedin_state"] == "easy_apply_contact_info_step"
    assert result["page_diagnostics"]["next_button_clicked"] is False
    assert result["page_diagnostics"]["next_button_not_clicked_reason"] == "blocking_required_contact_fields"
    radio_groups = {row["field_name"]: row for row in result["page_diagnostics"]["radio_group_diagnostics"]}
    assert radio_groups["phone_type"]["group_label"] == "Type * Required"
    assert radio_groups["phone_type"]["option_labels"] == ["Mobile", "Home"]
    assert radio_groups["phone_type"]["selected_option"] is None
    assert radio_groups["phone_type"]["selection_attempted"] is True
    assert radio_groups["phone_type"]["selection_verified"] is False
    assert client.click_calls == ["easy-apply-trigger", "phone-type-mobile"]
    blocking_rows = [row for row in result["page_diagnostics"]["blocking_skipped_fields"] if row["field_name"] == "phone_type"]
    assert blocking_rows
    assert blocking_rows[0]["reason"] == "radio_selection_attempted_but_not_verified"


def test_backend_linkedin_contact_step_uses_dom_radio_probe_when_snapshot_radio_refs_are_missing(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["application_target"]["application_url"] = "https://www.linkedin.com/jobs/view/4354740729/apply/?openSDUIApplyFlow=true"
    payload["application_target"]["source_url"] = "https://www.linkedin.com/jobs/view/4354740729/"
    pdf_path = tmp_path / "resume.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%Test\n")
    payload["resume_variant"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_file_name"] = "resume.pdf"
    payload["artifacts"]["resume_upload_path"] = str(pdf_path)
    payload["contact_profile"] = {
        "city": "Saint Mary's City",
        "state_or_province": "MD",
        "postal_code": "20686",
        "country": "United States",
        "primary_phone_number": "240-555-0101",
        "phone_type": "mobile",
    }

    class LinkedInDomRadioProbeClient(FakeBrowserClient):
        def __init__(self) -> None:
            super().__init__(
                page_title="The Amatriot Group hiring Software Developer",
                current_url="https://www.linkedin.com/jobs/view/4354740729/",
                snapshots=[],
            )
            self.stage = "job_page"
            self.phone_type_selected = False

        def snapshot(self) -> str:
            if self.stage == "job_page":
                return "\n".join(
                    [
                        '[nav-network] link "My Network"',
                        '[nav-messaging] link "Messaging"',
                        '[nav-notifications] link "Notifications"',
                        '[easy-apply-trigger] button "Easy Apply"',
                    ]
                )
            if self.stage == "contact":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                        '[step-heading] heading "Contact info"',
                        '[first-name] textbox "First name*": Zachary',
                        '[last-name] textbox "Last name*": Kralec',
                        '[email-address] combobox "Email address *": zkralec@icloud.com selected',
                        '[city-input] textbox "City*": Saint Mary\'s City',
                        '[state-input] textbox "State or Province*": MD',
                        '[zip-input] textbox "Zip/Postal Code*": 20686',
                        '[country-select] combobox "Country *": United States selected',
                        '[phone-input] textbox "Primary Phone Number*": 240-555-0101',
                        'group "Type * Required"',
                        f'radio "Mobile"{" checked" if self.phone_type_selected else " [active]"}',
                        'radio "Home"',
                        '[continue-next] button "Continue to next step"',
                    ]
                )
            if self.stage == "resume":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                        '[step-heading] heading "Resume"',
                        '[resume-upload] input "Upload resume"',
                    ]
                )
            return "\n".join(
                [
                    '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                    '[step-heading] heading "Review your application"',
                ]
            )

        def click(self, ref: str) -> None:
            super().click(ref)
            if ref == "easy-apply-trigger":
                self.stage = "contact"
            elif ref == "continue-next":
                self.stage = "resume"

        def evaluate_json(self, fn_source: str):
            if "__openclaw_native_select_probe__" in fn_source:
                return {
                    "probeKind": "__openclaw_native_select_probe__",
                    "isNativeSelect": True,
                    "detectedFieldType": "select",
                    "optionCount": 2,
                    "matchedOptionValue": "UNITED STATES",
                    "matchedOptionLabel": "UNITED STATES",
                    "fieldLabelText": "Country",
                }
            if "__openclaw_linkedin_radio_groups_probe__" in fn_source:
                return {
                    "probeKind": "__openclaw_linkedin_radio_groups_probe__",
                    "groups": [
                        {
                            "field_name": "phone_type",
                            "group_label": "Type * Required",
                            "required": True,
                            "options": ["Mobile", "Home"],
                            "selected_option": "Mobile" if self.phone_type_selected else None,
                            "selection_verified": self.phone_type_selected,
                            "chosen_option": "Mobile" if self.phone_type_selected else None,
                            "refs_involved": ["radio-mobile-id", "radio-home-id"],
                        }
                    ],
                }
            if "__openclaw_linkedin_radio_group_select__" in fn_source:
                self.phone_type_selected = True
                return {
                    "probeKind": "__openclaw_linkedin_radio_group_select__",
                    "found": True,
                    "field_name": "phone_type",
                    "group_label": "Type * Required",
                    "selection_attempted": True,
                    "selection_verified": True,
                    "chosen_option": "Mobile",
                    "selected_option": "Mobile",
                    "refs_involved": ["radio-mobile-id"],
                }
            return super().evaluate_json(fn_source)

        def upload(self, staged_path: Path, *, input_ref: str | None = None) -> None:
            super().upload(staged_path, input_ref=input_ref)
            self.stage = "review"

    client = LinkedInDomRadioProbeClient()
    result = run_backend(payload, client=client)

    assert result["failure_category"] is None
    assert result["draft_status"] == "draft_ready"
    assert client.click_calls == ["easy-apply-trigger", "continue-next"]
    radio_groups = {row["field_name"]: row for row in result["page_diagnostics"]["radio_group_diagnostics"]}
    assert radio_groups["phone_type"]["group_label"] == "Type * Required"
    assert radio_groups["phone_type"]["options"] == ["Mobile", "Home"]
    assert radio_groups["phone_type"]["selected_option"] == "Mobile"
    assert radio_groups["phone_type"]["selection_attempted"] is True
    assert radio_groups["phone_type"]["selection_verified"] is True
    assert radio_groups["phone_type"]["chosen_option"] == "Mobile"
    assert radio_groups["phone_type"]["refs_involved"] == ["radio-mobile-id", "radio-home-id"]


def test_backend_attach_mode_probes_existing_browser_without_start(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_APPLY_BROWSER_ATTACH_MODE", "true")
    monkeypatch.setenv("OPENCLAW_APPLY_SKIP_BROWSER_START", "true")
    monkeypatch.setenv("OPENCLAW_APPLY_ALLOW_BROWSER_START", "false")
    client = FakeBrowserClient()

    result = run_backend(_payload(tmp_path), client=client)

    assert result["draft_status"] == "draft_ready"
    assert client.start_calls == 0
    assert client.status_calls == 1
    assert client.tabs_calls == 1
    assert result["debug_json"]["browser_runtime"]["attach_probe_succeeded"] is True


def test_backend_skip_start_path_uses_probe_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_APPLY_BROWSER_ATTACH_MODE", "false")
    monkeypatch.setenv("OPENCLAW_APPLY_SKIP_BROWSER_START", "true")
    monkeypatch.setenv("OPENCLAW_APPLY_ALLOW_BROWSER_START", "false")
    client = FakeBrowserClient()

    result = run_backend(_payload(tmp_path), client=client)

    assert result["draft_status"] == "draft_ready"
    assert client.start_calls == 0
    assert client.status_calls == 1
    assert client.tabs_calls == 1


def test_backend_command_failure_diagnostics_include_stdout_stderr_and_stage(tmp_path: Path, monkeypatch) -> None:
    responses = [
        SimpleNamespace(returncode=0, stdout="OK", stderr=""),
        SimpleNamespace(returncode=1, stdout="", stderr="gateway connect failed"),
    ]

    def fake_run(command, stdout, stderr, text, timeout, check):  # type: ignore[no-untyped-def]
        return responses.pop(0)

    monkeypatch.setattr("integrations.openclaw_apply_browser_backend.subprocess.run", fake_run)
    monkeypatch.setenv("OPENCLAW_BROWSER_BASE_COMMAND", "/opt/openclaw/npm-global/bin/openclaw browser")
    monkeypatch.setenv("OPENCLAW_APPLY_BROWSER_ATTACH_MODE", "true")
    monkeypatch.setenv("OPENCLAW_APPLY_SKIP_BROWSER_START", "true")
    monkeypatch.setenv("OPENCLAW_APPLY_ALLOW_BROWSER_START", "false")

    result = run_backend(
        _payload(tmp_path),
        client=OpenClawBrowserClient(
            command="/opt/openclaw/npm-global/bin/openclaw browser",
            timeout_ms=5000,
            logger=logging.getLogger("test_openclaw_apply_browser_backend"),
        ),
    )

    assert result["failure_category"] == "manual_review_required"
    assert result["debug_json"]["browser_runtime"]["last_error_kind"] == "gateway_connectivity_failure"
    commands = result["debug_json"]["openclaw_commands"]
    assert commands[0]["stage"] == "probe_status"
    assert commands[1]["stage"] == "probe_tabs"
    assert commands[1]["exit_code"] == 1
    assert commands[1]["stderr"] == "gateway connect failed"
    assert commands[1]["failure_kind"] == "gateway_connectivity_failure"


def test_backend_container_safe_urls_use_host_docker_internal(monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_APPLY_RUNNING_IN_DOCKER", "true")
    monkeypatch.delenv("OPENCLAW_APPLY_RUN_ON_HOST", raising=False)
    monkeypatch.setenv("OPENCLAW_APPLY_GATEWAY_URL", "ws://127.0.0.1:18789")
    monkeypatch.setenv("OPENCLAW_APPLY_CDP_URL", "http://localhost:9222")
    monkeypatch.setenv("OPENCLAW_APPLY_HOST_GATEWAY_ALIAS", "host.docker.internal")
    config = _resolve_runtime_config({})

    assert config.gateway_url == "ws://host.docker.internal:18789"
    assert config.cdp_url == "http://host.docker.internal:9222"


def test_backend_host_run_mode_preserves_host_loopback_urls(monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_APPLY_RUNNING_IN_DOCKER", "true")
    monkeypatch.setenv("OPENCLAW_APPLY_RUN_ON_HOST", "true")
    monkeypatch.setenv("OPENCLAW_APPLY_GATEWAY_URL", "ws://127.0.0.1:18789")
    monkeypatch.setenv("OPENCLAW_APPLY_CDP_URL", "http://127.0.0.1:18800")
    config = _resolve_runtime_config({})

    assert config.run_on_host is True
    assert config.gateway_url == "ws://127.0.0.1:18789"
    assert config.cdp_url == "http://127.0.0.1:18800"


def test_browser_command_normalization_places_browser_scoped_flags_after_browser(monkeypatch) -> None:
    monkeypatch.delenv("OPENCLAW_APPLY_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("OPENCLAW_BROWSER_GATEWAY_TOKEN", raising=False)
    command = _normalize_browser_base_command(
        "openclaw --url ws://host.docker.internal:18789 browser --browser-profile openclaw",
        gateway_url=None,
        gateway_token=None,
    )

    assert command == "openclaw browser --url ws://host.docker.internal:18789 --browser-profile openclaw"


def test_browser_command_generation_for_status_tabs_open_and_screenshot(monkeypatch, tmp_path: Path) -> None:
    captured_commands: list[list[str]] = []
    media_path = tmp_path / "source.png"
    media_path.write_bytes(b"PNG")

    def fake_run(command, stdout, stderr, text, timeout, check):  # type: ignore[no-untyped-def]
        captured_commands.append(list(command))
        if command[-1] == "status":
            return SimpleNamespace(returncode=0, stdout="OK", stderr="")
        if command[-1] == "tabs":
            return SimpleNamespace(returncode=0, stdout="[]", stderr="")
        if command[-2] == "open":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "screenshot" in command:
            return SimpleNamespace(returncode=0, stdout=f"MEDIA:{media_path}", stderr="")
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr("integrations.openclaw_apply_browser_backend.subprocess.run", fake_run)
    client = OpenClawBrowserClient(
        command="openclaw browser --url ws://host.docker.internal:18789 --browser-profile openclaw",
        timeout_ms=5000,
        logger=logging.getLogger("test_openclaw_apply_browser_backend"),
    )

    client.status()
    client.tabs()
    client.open("https://jobs.example/apply/1")
    screenshot_path = tmp_path / "final.png"
    client.screenshot(screenshot_path)

    assert captured_commands == [
        ["openclaw", "browser", "--url", "ws://host.docker.internal:18789", "--browser-profile", "openclaw", "status"],
        ["openclaw", "browser", "--url", "ws://host.docker.internal:18789", "--browser-profile", "openclaw", "tabs"],
        ["openclaw", "browser", "--url", "ws://host.docker.internal:18789", "--browser-profile", "openclaw", "open", "https://jobs.example/apply/1"],
        ["openclaw", "browser", "--url", "ws://host.docker.internal:18789", "--browser-profile", "openclaw", "screenshot", "--full-page"],
    ]


def test_regression_bad_top_level_url_is_normalized_and_subcommand_scoped_url_succeeds(monkeypatch) -> None:
    monkeypatch.delenv("OPENCLAW_APPLY_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("OPENCLAW_BROWSER_GATEWAY_TOKEN", raising=False)
    monkeypatch.setenv(
        "OPENCLAW_BROWSER_BASE_COMMAND",
        "openclaw --url ws://host.docker.internal:18789 browser --browser-profile openclaw",
    )
    config = _resolve_runtime_config({})

    assert config.command != "openclaw --url ws://host.docker.internal:18789 browser --browser-profile openclaw"
    assert config.command == "openclaw browser --url ws://host.docker.internal:18789 --browser-profile openclaw"

    def fake_run(command, stdout, stderr, text, timeout, check):  # type: ignore[no-untyped-def]
        if len(command) > 1 and command[1] == "--url":
            return SimpleNamespace(returncode=1, stdout="", stderr="error: unknown option '--url'")
        return SimpleNamespace(returncode=0, stdout="OK", stderr="")

    monkeypatch.setattr("integrations.openclaw_apply_browser_backend.subprocess.run", fake_run)
    client = OpenClawBrowserClient(
        command=config.command,
        timeout_ms=5000,
        logger=logging.getLogger("test_openclaw_apply_browser_backend"),
    )

    assert client.status() == "OK"


def test_backend_contact_fields_use_default_answer_profile_values_on_linkedin(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["application_target"]["application_url"] = "https://www.linkedin.com/jobs/view/4354740729/apply/?openSDUIApplyFlow=true"
    payload["application_target"]["source_url"] = "https://www.linkedin.com/jobs/view/4354740729/"
    pdf_path = tmp_path / "resume.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%Test\n")
    payload["resume_variant"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_file_name"] = "resume.pdf"
    payload["artifacts"]["resume_upload_path"] = str(pdf_path)

    class LinkedInDefaultContactClient(FakeBrowserClient):
        def __init__(self) -> None:
            super().__init__(
                page_title="The Amatriot Group hiring Software Developer",
                current_url="https://www.linkedin.com/jobs/view/4354740729/",
                snapshots=[],
            )
            self.stage = "job_page"
            self.contact_values: dict[str, object] = {
                "city": "",
                "state": "",
                "zip": "",
                "country": "",
                "phone": "",
            }
            self.phone_type_selected = False

        def snapshot(self) -> str:
            if self.stage == "job_page":
                return "\n".join(
                    [
                        '[nav-network] link "My Network"',
                        '[nav-messaging] link "Messaging"',
                        '[nav-notifications] link "Notifications"',
                        '[easy-apply-trigger] button "Easy Apply"',
                    ]
                )
            if self.stage == "contact":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                        '[step-heading] heading "Contact info"',
                        f'[city-input] textbox "City*"{": " + str(self.contact_values["city"]) if self.contact_values["city"] else ""}',
                        f'[state-input] textbox "State or Province*"{": " + str(self.contact_values["state"]) if self.contact_values["state"] else ""}',
                        f'[zip-input] textbox "Zip/Postal Code*"{": " + str(self.contact_values["zip"]) if self.contact_values["zip"] else ""}',
                        f'[country-select] combobox "Country *"{": " + str(self.contact_values["country"]) + " selected" if self.contact_values["country"] else ""}',
                        f'[phone-input] textbox "Primary Phone Number*"{": " + str(self.contact_values["phone"]) if self.contact_values["phone"] else ""}',
                        f'[phone-type-mobile] radio "Mobile"{" checked" if self.phone_type_selected else ""}',
                        '[next-contact] button "Next"',
                    ]
                )
            if self.stage == "contact_country_open":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                        '[step-heading] heading "Contact info"',
                        f'[city-input] textbox "City*": {self.contact_values["city"]}',
                        f'[state-input] textbox "State or Province*": {self.contact_values["state"]}',
                        f'[zip-input] textbox "Zip/Postal Code*": {self.contact_values["zip"]}',
                        '[country-select] combobox "Country *"',
                        '- option "UNITED STATES"',
                        f'[phone-input] textbox "Primary Phone Number*": {self.contact_values["phone"]}',
                        f'[phone-type-mobile] radio "Mobile"{" checked" if self.phone_type_selected else ""}',
                        '[next-contact] button "Next"',
                    ]
                )
            if self.stage == "resume":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                        '[step-heading] heading "Resume"',
                        '[resume-upload] input "Upload resume"',
                    ]
                )
            return "\n".join(
                [
                    '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                    '[step-heading] heading "Review your application"',
                ]
            )

        def click(self, ref: str) -> None:
            super().click(ref)
            if ref == "easy-apply-trigger":
                self.stage = "contact"
                self.page_title = "Apply to The Amatriot Group"
            elif ref == "phone-type-mobile":
                self.phone_type_selected = True
                self.stage = "contact"
            elif ref == "next-contact":
                self.stage = "resume"

        def fill(self, fields: list[dict]) -> None:
            super().fill(fields)
            ref_map = {
                "city-input": "city",
                "state-input": "state",
                "zip-input": "zip",
                "phone-input": "phone",
            }
            for row in fields:
                key = ref_map.get(str(row["ref"]))
                if key:
                    self.contact_values[key] = row["value"]
            self.stage = "contact"

        def select(self, ref: str, value: str) -> None:
            super().select(ref, value)
            if ref == "country-select":
                assert value == "UNITED STATES"
                self.contact_values["country"] = "United States"
                self.stage = "contact"

        def evaluate_json(self, fn_source: str):
            if "__openclaw_native_select_probe__" in fn_source:
                return {
                    "probeKind": "__openclaw_native_select_probe__",
                    "isNativeSelect": True,
                    "detectedFieldType": "select",
                    "optionCount": 2,
                    "matchedOptionValue": "UNITED STATES",
                    "matchedOptionLabel": "UNITED STATES",
                    "fieldLabelText": "Country",
                }
            return super().evaluate_json(fn_source)

        def upload(self, staged_path: Path, *, input_ref: str | None = None) -> None:
            super().upload(staged_path, input_ref=input_ref)
            self.stage = "review"

    client = LinkedInDefaultContactClient()
    result = run_backend(payload, client=client)
    filled = _flatten_fill_calls(client)

    assert result["failure_category"] is None
    assert filled["city-input"] == "Monkton"
    assert filled["state-input"] == "MD"
    assert filled["zip-input"] == "21111"
    assert filled["phone-input"] == "4104566443"
    assert client.click_calls == ["easy-apply-trigger", "phone-type-mobile", "next-contact"]
    assert not any(field["ref"] == "country-select" for batch in client.fill_calls for field in batch)
    assert ("country-select", "UNITED STATES") in client.select_calls
    country_interaction = next(
        row
        for row in result["page_diagnostics"]["contact_field_interactions"]
        if row["field_name"] == "country" and row["interaction_type"] == "select"
    )
    assert country_interaction["detected_field_type"] == "select"
    assert country_interaction["select_value_attempted"] == "United States"
    assert country_interaction["select_value_normalized"] == "UNITED STATES"
    assert country_interaction["select_success"] is True
    assert "click_next" in [row["action"] for row in result["debug_json"]["linkedin_progression"]]


def test_backend_linkedin_country_native_select_uses_normalized_select_value(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["application_target"]["application_url"] = "https://www.linkedin.com/jobs/view/4354740729/apply/?openSDUIApplyFlow=true"
    payload["application_target"]["source_url"] = "https://www.linkedin.com/jobs/view/4354740729/"
    pdf_path = tmp_path / "resume.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%Test\n")
    payload["resume_variant"]["resume_upload_path"] = str(pdf_path)
    payload["resume_variant"]["resume_file_name"] = "resume.pdf"
    payload["artifacts"]["resume_upload_path"] = str(pdf_path)

    class LinkedInNativeSelectClient(FakeBrowserClient):
        def __init__(self) -> None:
            super().__init__(
                page_title="The Amatriot Group hiring Software Developer",
                current_url="https://www.linkedin.com/jobs/view/4354740729/",
                snapshots=[],
            )
            self.stage = "job_page"
            self.native_select_probe_calls = 0
            self.contact_values: dict[str, object] = {
                "city": "",
                "state": "",
                "zip": "",
                "country": "",
                "phone": "",
            }
            self.phone_type_selected = False

        def snapshot(self) -> str:
            if self.stage == "job_page":
                return "\n".join(
                    [
                        '[nav-network] link "My Network"',
                        '[nav-messaging] link "Messaging"',
                        '[nav-notifications] link "Notifications"',
                        '[easy-apply-trigger] button "Easy Apply"',
                    ]
                )
            if self.stage == "contact":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                        '[step-heading] heading "Contact info"',
                        f'[city-input] textbox "City*"{": " + str(self.contact_values["city"]) if self.contact_values["city"] else ""}',
                        f'[state-input] textbox "State or Province*"{": " + str(self.contact_values["state"]) if self.contact_values["state"] else ""}',
                        f'[zip-input] textbox "Zip/Postal Code*"{": " + str(self.contact_values["zip"]) if self.contact_values["zip"] else ""}',
                        f'[country-select] combobox "Country *"{": " + str(self.contact_values["country"]) + " selected" if self.contact_values["country"] else ""}',
                        f'[phone-input] textbox "Primary Phone Number*"{": " + str(self.contact_values["phone"]) if self.contact_values["phone"] else ""}',
                        f'[phone-type-mobile] radio "Mobile"{" checked" if self.phone_type_selected else ""}',
                        '[next-contact] button "Next"',
                    ]
                )
            if self.stage == "resume":
                return "\n".join(
                    [
                        '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                        '[step-heading] heading "Resume"',
                        '[resume-upload] input "Upload resume"',
                    ]
                )
            return "\n".join(
                [
                    '[dialog-root] dialog "Apply to The Amatriot Group" [active]',
                    '[step-heading] heading "Review your application"',
                ]
            )

        def click(self, ref: str) -> None:
            super().click(ref)
            if ref == "easy-apply-trigger":
                self.stage = "contact"
                self.page_title = "Apply to The Amatriot Group"
            elif ref == "phone-type-mobile":
                self.phone_type_selected = True
                self.stage = "contact"
            elif ref == "next-contact":
                self.stage = "resume"

        def fill(self, fields: list[dict]) -> None:
            super().fill(fields)
            ref_map = {
                "city-input": "city",
                "state-input": "state",
                "zip-input": "zip",
                "phone-input": "phone",
            }
            for row in fields:
                key = ref_map.get(str(row["ref"]))
                if key:
                    self.contact_values[key] = row["value"]
            self.stage = "contact"

        def select(self, ref: str, value: str) -> None:
            super().select(ref, value)
            if ref == "country-select":
                assert value == "UNITED STATES"
                self.contact_values["country"] = "United States"
                self.stage = "contact"

        def evaluate_json(self, fn_source: str):
            if "__openclaw_native_select_probe__" in fn_source:
                self.native_select_probe_calls += 1
                return {
                    "probeKind": "__openclaw_native_select_probe__",
                    "isNativeSelect": True,
                    "detectedFieldType": "select",
                    "optionCount": 2,
                    "matchedOptionValue": "UNITED STATES",
                    "matchedOptionLabel": "UNITED STATES",
                    "fieldLabelText": "Country",
                }
            return super().evaluate_json(fn_source)

        def upload(self, staged_path: Path, *, input_ref: str | None = None) -> None:
            super().upload(staged_path, input_ref=input_ref)
            self.stage = "review"

    client = LinkedInNativeSelectClient()
    result = run_backend(payload, client=client)

    assert result["failure_category"] is None
    assert client.native_select_probe_calls >= 1
    assert client.click_calls == ["easy-apply-trigger", "phone-type-mobile", "next-contact"]
    assert client.select_calls[-1] == ("country-select", "UNITED STATES")
    assert not any(field["ref"] == "country-select" for batch in client.fill_calls for field in batch)
    country_interaction = next(
        row
        for row in result["page_diagnostics"]["contact_field_interactions"]
        if row["field_name"] == "country" and row["interaction_type"] == "select"
    )
    assert country_interaction["detected_field_type"] == "select"
    assert country_interaction["select_value_attempted"] == "United States"
    assert country_interaction["select_value_normalized"] == "UNITED STATES"
    assert country_interaction["select_success"] is True
    assert result["page_diagnostics"]["contact_any_false_positive_prevented"] is False


def test_backend_maps_work_authorization_and_sponsorship_questions(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    form_snapshot = "\n".join(
        [
            '[auth] combobox "Are you legally authorized to work in the United States? *"',
            '[sponsor] combobox "Will you now or in the future require sponsorship? *"',
            '[review] heading "Review your application"',
        ]
    )
    client = _post_upload_client(form_snapshot)

    result = run_backend(payload, client=client)

    assert result["failure_category"] is None
    assert ("auth", "Yes") in client.select_calls
    assert ("sponsor", "No") in client.select_calls
    mappings = {row["canonical_key"]: row for row in result["form_diagnostics"]["answer_mappings"] if row["canonical_key"]}
    assert mappings["work_authorized_us"]["source"] == "default_profile"
    assert mappings["sponsorship_required"]["source"] == "default_profile"


def test_backend_maps_recruiter_and_company_history_defaults(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    form_snapshot = "\n".join(
        [
            '[worked-here] combobox "Have you ever worked here before? *"',
            '[interviewed-before] combobox "Have you interviewed here before? *"',
            '[hear-about-us] combobox "How did you hear about us? *"',
            '[review] heading "Review your application"',
        ]
    )
    client = _post_upload_client(form_snapshot)

    result = run_backend(payload, client=client)

    assert result["failure_category"] is None
    assert ("worked-here", "No") in client.select_calls
    assert ("interviewed-before", "No") in client.select_calls
    assert ("hear-about-us", "LinkedIn") in client.select_calls
    required_filled = set(result["form_diagnostics"]["required_fields_filled"])
    assert {"worked_here_before", "interviewed_here_before", "hear_about_us"} <= required_filled


def test_backend_maps_salary_and_start_date_defaults(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    form_snapshot = "\n".join(
        [
            '[salary] textbox "Desired salary *"',
            '[start-date] textbox "Available start date *"',
            '[review] heading "Review your application"',
        ]
    )
    client = _post_upload_client(form_snapshot)

    result = run_backend(payload, client=client)
    filled = _flatten_fill_calls(client)

    assert result["failure_category"] is None
    assert filled["salary"] == "100000"
    assert filled["start-date"] == "05/18/2026"
    assert result["page_diagnostics"]["should_auto_submit"] is False
    assert result["page_diagnostics"]["submit_step_detected"] is False
    assert result["page_diagnostics"]["submit_button_present"] is False


def test_backend_optional_self_id_fields_are_filled_when_clearly_mapped(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    form_snapshot = "\n".join(
        [
            '[veteran] combobox "Veteran status"',
            '[gender] combobox "Gender"',
            '[disability] combobox "Disability status"',
            '[ethnicity] combobox "Ethnicity/Race"',
            '[review] heading "Review your application"',
        ]
    )
    client = _post_upload_client(form_snapshot)

    result = run_backend(payload, client=client)

    assert result["failure_category"] is None
    assert ("veteran", "Not a veteran") in client.select_calls
    assert ("gender", "Male") in client.select_calls
    assert ("disability", "No disability") in client.select_calls
    assert ("ethnicity", "White (not Hispanic)") in client.select_calls
    assert result["form_diagnostics"]["self_id_handling_mode"] == "direct_default"


def test_backend_required_self_id_fields_are_completed_when_mapped(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    form_snapshot = "\n".join(
        [
            '[veteran] combobox "Veteran status *"',
            '[gender] combobox "Gender *"',
            '[review] heading "Review your application"',
        ]
    )
    client = _post_upload_client(form_snapshot)

    result = run_backend(payload, client=client)

    assert result["failure_category"] is None
    assert result["form_diagnostics"]["missing_required_fields"] == []
    assert ("veteran", "Not a veteran") in client.select_calls
    assert ("gender", "Male") in client.select_calls


def test_backend_uses_llm_generated_motivation_answer_when_available(tmp_path: Path, monkeypatch) -> None:
    payload = _payload(tmp_path)
    payload["application_answers"] = []
    form_snapshot = "\n".join(
        [
            '[motivation] textarea "Why are you interested in this role? *"',
            '[review] heading "Review your application"',
        ]
    )
    client = _post_upload_client(form_snapshot)

    monkeypatch.setattr(
        "integrations.openclaw_apply_browser_backend.motivation_answer",
        lambda **_: {
            "answer": "I am excited about the chance to apply AI automation to a real customer-facing product.",
            "source": "llm_generated",
            "confidence": 0.91,
            "reason": "llm_generated",
        },
    )

    result = run_backend(payload, client=client)
    filled = _flatten_fill_calls(client)

    assert result["failure_category"] is None
    assert filled["motivation"] == "I am excited about the chance to apply AI automation to a real customer-facing product."
    motivation_mapping = next(
        row for row in result["form_diagnostics"]["answer_mappings"] if row["canonical_key"] == "reason_for_interest"
    )
    assert motivation_mapping["source"] == "llm_generated"
    assert result["page_diagnostics"]["confidence_score_used"] == 0.91


def test_backend_sets_auto_submit_eligibility_only_when_confidence_threshold_is_met(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    form_snapshot = "\n".join(
        [
            '[auth] combobox "Are you legally authorized to work in the United States? *"',
            '[salary] textbox "Desired salary *"',
            '[review] heading "Review your application"',
        ]
    )
    client = _post_upload_client(form_snapshot)

    result = run_backend(payload, client=client)

    assert result["failure_category"] is None
    assert result["page_diagnostics"]["should_auto_submit"] is False
    assert result["page_diagnostics"]["submit_step_detected"] is False
    assert result["page_diagnostics"]["submit_decision_reason"] in {
        "submit_step_not_detected",
        "no_safe_advance_action_visible",
    }
    assert result["page_diagnostics"]["confidence_score_used"] == 0.95


def test_backend_stops_for_review_when_required_question_is_ambiguous(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    form_snapshot = "\n".join(
        [
            '[auth] combobox "Are you legally authorized to work in the United States? *"',
            '[mystery-self-id] combobox "Voluntary self-identification category *"',
            '[review] heading "Review your application"',
        ]
    )
    client = _post_upload_client(form_snapshot)

    result = run_backend(payload, client=client)

    assert result["draft_status"] == "partial_draft"
    assert result["source_status"] == "manual_review_required"
    assert result["failure_category"] == "manual_review_required"
    assert result["awaiting_review"] is True
    assert ("auth", "Yes") in client.select_calls
    assert result["form_diagnostics"]["missing_required_fields"][0]["reason"] == "ambiguous_required_self_id_field"
