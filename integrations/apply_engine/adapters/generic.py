"""
Generic fallback adapter.

Used when no specific adapter matches the URL.
Attempts best-effort form filling on any job application page.

Strategy:
  1. Find an "Apply" button if not already on a form
  2. Detect and fill all visible form fields
  3. Look for multi-step navigation (Next buttons)
  4. Stop when review/submit page detected
  5. Return partial result with whatever was filled

This won't work reliably on complex ATS platforms, but handles
simple employer career sites and custom application pages.
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
from ..observability import get_logger
from ..session import ApplySession
from .base import AdapterResult, SiteAdapter

_log = get_logger("generic")

MAX_STEPS = 15

_APPLY_BUTTON_SELECTORS = [
    "a:has-text('Apply Now')",
    "a:has-text('Apply for this Job')",
    "a:has-text('Apply for Job')",
    "button:has-text('Apply Now')",
    "button:has-text('Apply')",
    "a:has-text('Apply')",
    "[data-apply-button]",
    ".apply-button",
    "#apply-button",
    "#applyBtn",
]


class GenericAdapter(SiteAdapter):
    name = "generic"
    priority = 0  # Lowest priority — only used as fallback

    @classmethod
    def detect(cls, url: str, page_title: str = "", page_content: str = "") -> bool:
        return True  # Always matches — this is the fallback

    async def run(
        self,
        session: ApplySession,
        answer_engine: AnswerEngine,
        job_metadata: dict[str, Any] | None = None,
    ) -> AdapterResult:
        result = AdapterResult(adapter_name=self.name, site_name="Generic")
        all_fills: list[FieldFillResult] = []

        try:
            await session.screenshot("01-landing")
            _log.info(f"generic adapter started | url={await session.current_url()}")

            # Try to navigate to application form
            await self._try_navigate_to_form(session)
            await session.wait_stable(1200)
            await session.screenshot("02-on-form")

            step_index = 0
            while step_index < MAX_STEPS:
                step_index += 1

                step_type = await detect_step(session.page)

                if step_type == StepType.REVIEW or await is_review_page(session.page):
                    _log.info(f"review page reached | step={step_index}")
                    await session.screenshot(f"{step_index:02d}-review")
                    result.review_reached = True
                    result.status = "draft_ready"
                    result.step_count = step_index
                    result.fields_filled = all_fills
                    return result

                # Fill fields
                fields = await detect_fields(session.page)
                for form_field in fields:
                    label = form_field.label or form_field.aria_label or form_field.name_attr
                    if not label:
                        continue

                    question = FormQuestion(
                        label=label,
                        field_type=form_field.field_type,
                        options=form_field.options,
                        required=form_field.required,
                        placeholder=form_field.placeholder,
                        name_attr=form_field.name_attr,
                        id_attr=form_field.id_attr,
                        context_text=form_field.context_text,
                        site="generic",
                    )

                    if form_field.field_type == "file":
                        resume_path = answer_engine._profile.resume_path
                        fill_result = await fill_field(session.page, form_field, resume_path, resume_path=resume_path)
                    else:
                        answer = answer_engine.answer(question)
                        if not answer.found:
                            all_fills.append(FieldFillResult(
                                field_label=label, field_type=form_field.field_type,
                                status="skipped", value_preview="no answer",
                                required=form_field.required,
                            ))
                            continue
                        fill_result = await fill_field(session.page, form_field, answer.value)

                    all_fills.append(fill_result)

                await session.screenshot(f"{step_index:02d}-filled")

                # Check if we can advance
                next_btn = await find_next_button(session.page)
                submit_btn = await find_submit_button(session.page)

                if submit_btn and not next_btn:
                    # We're on the final form page / review
                    _log.info("submit button found — treating as review page")
                    await session.screenshot(f"{step_index:02d}-pre-submit")
                    result.review_reached = True
                    result.status = "draft_ready"
                    result.step_count = step_index
                    result.fields_filled = all_fills
                    result.notes.append("Submit button detected. Human review required before submitting.")
                    return result

                if next_btn:
                    await next_btn.click()
                    await asyncio.sleep(1.0)
                    errors = await get_validation_errors(session.page)
                    if errors:
                        _log.warning(f"validation errors | step={step_index} errors={errors[:3]}")
                        await session.screenshot(f"{step_index:02d}-validation-errors")
                        result.status = "partial"
                        result.failure_reason = f"Validation errors on step {step_index}: {'; '.join(errors[:3])}"
                        result.step_count = step_index
                        result.fields_filled = all_fills
                        return result
                else:
                    # No next button and no submit button — stuck
                    _log.warning(f"no next or submit button found | step={step_index}")
                    result.status = "partial"
                    result.failure_reason = f"Could not navigate past step {step_index} — no navigation buttons found."
                    result.step_count = step_index
                    result.fields_filled = all_fills
                    return result

            result.status = "partial"
            result.failure_reason = f"Did not reach review within {MAX_STEPS} steps."
            result.step_count = step_index
            result.fields_filled = all_fills
            return result

        except Exception as exc:
            _log.error(f"generic adapter error | error={exc}", exc_info=True)
            await session.screenshot_failure("adapter-exception")
            result.status = "failed"
            result.failure_reason = str(exc)
            result.fields_filled = all_fills
            return result
        finally:
            result.screenshots = session.all_screenshots

    async def _try_navigate_to_form(self, session: ApplySession) -> None:
        """Try to click an Apply button if visible."""
        apply_btn = await session.try_selectors(_APPLY_BUTTON_SELECTORS)
        if apply_btn:
            _log.debug("clicking apply button")
            await apply_btn.click()
            await asyncio.sleep(1.5)
