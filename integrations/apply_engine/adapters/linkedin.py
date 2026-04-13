"""
LinkedIn Easy Apply adapter.

Handles the LinkedIn Easy Apply modal form flow.

Flow:
  1. Navigate to job listing URL
  2. Find and click "Easy Apply" button
  3. Handle multi-step modal: Contact Info → Resume → Screening → Review
  4. Fill each visible field using answer engine
  5. Click "Next" to advance steps
  6. STOP when review page is detected (do NOT click Submit)
  7. Take screenshots at each transition

LinkedIn quirks:
  - Selectors change frequently → use multiple fallback selectors
  - Dynamic fields appear based on previous answers
  - Some fields are custom widgets (not native HTML)
  - File upload accepts PDF/DOCX/DOC only
  - Auth required for Easy Apply → detect and surface clearly
  - Modal may have multiple sub-forms per "step"
"""
from __future__ import annotations

import asyncio
from typing import Any

from ..answer_engine import AnswerEngine, FormQuestion
from ..forms.classifier import detect_fields
from ..forms.detector import (
    StepType,
    detect_step,
    find_next_button,
    find_submit_button,
    get_validation_errors,
    is_review_page,
)
from ..forms.handlers import FieldFillResult, fill_field
from ..observability import StepRecord, get_logger
from ..session import ApplySession
from .base import AdapterResult, SiteAdapter
from datetime import datetime, timezone

_log = get_logger("linkedin")

MAX_STEPS = 20          # safety cap to avoid infinite loops
MAX_RETRY_PER_STEP = 2  # retry advancing a step this many times if errors found


class LinkedInAdapter(SiteAdapter):
    name = "linkedin"
    priority = 10

    # ------------------------------------------------------------------
    # Easy Apply modal selectors (ordered by specificity)
    # Multiple fallbacks because LinkedIn changes these frequently
    # ------------------------------------------------------------------

    _EASY_APPLY_BUTTON_SELECTORS = [
        ".jobs-apply-button[aria-label*='Easy Apply']",
        "button.jobs-apply-button",
        "button[aria-label*='Easy Apply']",
        ".jobs-s-apply button",
        "a.jobs-apply-button",
        "button:has-text('Easy Apply')",
    ]

    _MODAL_SELECTORS = [
        ".jobs-easy-apply-modal",
        "[data-test-modal]",
        ".jobs-easy-apply-content",
        "div[role='dialog']",
        ".artdeco-modal",
    ]

    _MODAL_HEADER_SELECTORS = [
        ".jobs-easy-apply-modal__header h3",
        ".jobs-easy-apply-header h2",
        "[data-test-modal-header]",
        "div[role='dialog'] h1",
        "div[role='dialog'] h2",
        "div[role='dialog'] h3",
        ".artdeco-modal__header h2",
    ]

    _DISMISS_MODAL_SELECTORS = [
        "button[aria-label='Dismiss']",
        "button[aria-label='Close']",
        ".artdeco-modal__dismiss",
    ]

    @classmethod
    def detect(cls, url: str, page_title: str = "", page_content: str = "") -> bool:
        # Check the hostname only — utm_source=linkedin.com in query params must not match.
        from urllib.parse import urlparse
        try:
            hostname = urlparse(url).hostname or ""
            return "linkedin.com" in hostname.lower()
        except Exception:
            return False

    async def run(
        self,
        session: ApplySession,
        answer_engine: AnswerEngine,
        job_metadata: dict[str, Any] | None = None,
    ) -> AdapterResult:
        result = AdapterResult(adapter_name=self.name, site_name="LinkedIn")
        all_fills: list[FieldFillResult] = []

        try:
            # Screenshot the job listing page
            await session.screenshot("01-job-listing")

            # Check if we're on a job listing or if it redirected
            current_url = await session.current_url()
            _log.info(f"starting linkedin apply | url={current_url}")

            # Try to find and click the Easy Apply button
            apply_btn = await session.try_selectors(self._EASY_APPLY_BUTTON_SELECTORS)
            if not apply_btn:
                _log.warning("easy apply button not found")
                await session.screenshot_failure("no-easy-apply-button")
                result.status = "failed"
                result.failure_reason = "Easy Apply button not found. Job may not support Easy Apply, or requires login."
                return result

            # Check if login is required
            if await self._is_login_required(session):
                _log.warning("login required")
                await session.screenshot_failure("login-required")
                result.status = "auth_required"
                result.failure_reason = "LinkedIn login required. Set up auth state first."
                return result

            # Click the Easy Apply button
            await apply_btn.click()
            await asyncio.sleep(1.2)
            await session.screenshot("02-modal-opened")

            # Verify the modal appeared
            modal = await session.try_selectors(self._MODAL_SELECTORS)
            if not modal:
                _log.warning("modal did not appear after clicking Easy Apply")
                await session.screenshot_failure("modal-not-opened")
                result.status = "failed"
                result.failure_reason = "Easy Apply modal did not open."
                return result

            _log.info("modal opened, starting step navigation")

            # Navigate through steps
            step_index = 0
            while step_index < MAX_STEPS:
                step_index += 1
                await session.wait_stable(600)

                # Detect current step type
                step_type = await detect_step(session.page)
                _log.info(f"processing step | index={step_index} step_type={step_type}")

                step_record = StepRecord(
                    index=step_index,
                    step_type=step_type.value,
                    started_at=datetime.now(timezone.utc).isoformat(),
                )

                # STOP condition: review page reached
                if step_type == StepType.REVIEW or await is_review_page(session.page):
                    _log.info(f"review page reached | step_index={step_index}")
                    screenshot_path = await session.screenshot(f"{step_index:02d}-review-page")
                    step_record.screenshot_path = screenshot_path
                    result.screenshots = session.all_screenshots
                    result.review_reached = True
                    result.status = "draft_ready"
                    result.step_count = step_index
                    result.fields_filled = all_fills
                    result.notes.append(f"Stopped at review page (step {step_index}). Do not submit without human review.")
                    return result

                # Fill visible fields on this step
                fills = await self._fill_step_fields(session, answer_engine, step_type, step_index)
                all_fills.extend(fills)
                step_record.fields_filled = sum(1 for f in fills if f.success)
                step_record.fields_failed = sum(1 for f in fills if not f.success and f.status != "skipped")

                screenshot_path = await session.screenshot(f"{step_index:02d}-{step_type.value}-filled")
                step_record.screenshot_path = screenshot_path
                step_record.completed_at = datetime.now(timezone.utc).isoformat()

                # Try to advance to next step
                advanced = await self._advance_step(session, step_index)
                if not advanced:
                    _log.warning(f"could not advance step | index={step_index}")
                    await session.screenshot_failure(f"step-{step_index}-stuck")
                    result.status = "partial"
                    result.failure_reason = f"Could not advance past step {step_index} ({step_type.value})."
                    result.step_count = step_index
                    result.fields_filled = all_fills
                    return result

            # Exceeded max steps without finding review
            _log.warning("max steps exceeded without reaching review")
            result.status = "partial"
            result.failure_reason = f"Did not reach review page within {MAX_STEPS} steps."
            result.step_count = step_index
            result.fields_filled = all_fills
            return result

        except Exception as exc:
            _log.error(f"linkedin adapter error | error={exc}", exc_info=True)
            await session.screenshot_failure("adapter-exception")
            result.status = "failed"
            result.failure_reason = f"{type(exc).__name__}: {exc}"
            result.fields_filled = all_fills
            return result
        finally:
            result.screenshots = session.all_screenshots

    # ------------------------------------------------------------------
    # Step field filling
    # ------------------------------------------------------------------

    async def _fill_step_fields(
        self,
        session: ApplySession,
        answer_engine: AnswerEngine,
        step_type: StepType,
        step_index: int,
    ) -> list[FieldFillResult]:
        """Detect and fill all fields on the current step."""
        fills: list[FieldFillResult] = []

        # Scope detection to the modal container
        modal = await session.try_selectors(self._MODAL_SELECTORS)
        target_page = session.page  # fallback to full page

        fields = await detect_fields(target_page)
        _log.debug(f"fields found on step | count={len(fields)} step={step_type.value}")

        for form_field in fields:
            # Skip file inputs unless this is the resume step
            if form_field.field_type == "file" and step_type != StepType.RESUME:
                continue

            # Build question for answer engine
            question = FormQuestion(
                label=form_field.label or form_field.aria_label or form_field.name_attr,
                field_type=form_field.field_type,
                options=form_field.options,
                required=form_field.required,
                placeholder=form_field.placeholder,
                name_attr=form_field.name_attr,
                id_attr=form_field.id_attr,
                context_text=form_field.context_text,
                site="linkedin",
            )

            # Handle file uploads specially
            if form_field.field_type == "file":
                resume_path = answer_engine._profile.resume_path
                fill_result = await fill_field(target_page, form_field, resume_path, resume_path=resume_path)
            else:
                answer = answer_engine.answer(question)
                if not answer.found:
                    _log.debug(f"no answer for field | label={question.label} step={step_type.value}")
                    fills.append(FieldFillResult(
                        field_label=form_field.label,
                        field_type=form_field.field_type,
                        status="skipped",
                        value_preview="no answer configured",
                        required=form_field.required,
                    ))
                    continue

                _log.debug(
                    f"answering field | label={question.label} source={answer.source} "
                    f"confidence={answer.confidence:.2f}"
                )
                fill_result = await fill_field(
                    target_page, form_field, answer.value,
                    resume_path=answer_engine._profile.resume_path,
                )

            fills.append(fill_result)
            if fill_result.success:
                _log.debug(f"filled | label={form_field.label} preview={fill_result.value_preview}")
            else:
                _log.warning(f"fill failed | label={form_field.label} error={fill_result.error}")

        return fills

    # ------------------------------------------------------------------
    # Step navigation
    # ------------------------------------------------------------------

    async def _advance_step(self, session: ApplySession, step_index: int) -> bool:
        """
        Click the Next/Continue button to advance to the next step.
        Returns True if successfully advanced (page changed).
        """
        for attempt in range(MAX_RETRY_PER_STEP):
            next_btn = await find_next_button(session.page)
            if not next_btn:
                # Check if it's actually the submit button (review page)
                submit_btn = await find_submit_button(session.page)
                if submit_btn:
                    _log.debug("submit button found — this is the review step")
                    return True
                _log.warning(f"no next button found | step={step_index} attempt={attempt + 1}")
                return False

            # Click Next
            await next_btn.click()
            await session.wait_stable(800)

            # Check for validation errors
            errors = await get_validation_errors(session.page)
            if errors:
                _log.warning(
                    f"validation errors after next click | step={step_index} "
                    f"errors={errors[:3]} attempt={attempt + 1}"
                )
                if attempt < MAX_RETRY_PER_STEP - 1:
                    await session.screenshot(f"step-{step_index}-validation-error")
                    await asyncio.sleep(1.0)
                    continue
                else:
                    return False

            return True

        return False

    # ------------------------------------------------------------------
    # Auth detection
    # ------------------------------------------------------------------

    async def _is_login_required(self, session: ApplySession) -> bool:
        """Check if the page is showing a login prompt."""
        url = await session.current_url()
        if "login" in url or "signin" in url or "authwall" in url:
            return True
        # Check for sign-in form elements
        try:
            login_form = await session.page.query_selector(
                "#username, input[name='session_key'], .login-form"
            )
            return login_form is not None
        except Exception:
            return False
