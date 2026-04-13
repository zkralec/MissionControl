"""
Greenhouse adapter.

Greenhouse (greenhouse.io / boards.greenhouse.io) is one of the most common ATS platforms.
It has a relatively stable DOM structure.

Greenhouse application flow:
  1. Job listing at boards.greenhouse.io/{company}/jobs/{id}
  2. "Apply for this job" button → application form (single page, no modal)
  3. Form sections: Contact, Work Experience, Education, Voluntary Demographics
  4. Submit button at the bottom
  5. We stop BEFORE submit

Greenhouse URLs:
  - https://boards.greenhouse.io/company/jobs/12345
  - https://job-boards.greenhouse.io/company/jobs/12345
  - https://boards.eu.greenhouse.io/company/jobs/12345
  - Custom domain (company.jobs → greenhouse iframe)
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from ..answer_engine import AnswerEngine, FormQuestion
from ..forms.classifier import detect_fields
from ..forms.detector import find_submit_button, is_review_page
from ..forms.handlers import FieldFillResult, fill_field
from ..observability import get_logger
from ..session import ApplySession
from .base import AdapterResult, SiteAdapter

_log = get_logger("greenhouse")

_GREENHOUSE_URL_PATTERNS = [
    r"boards\.greenhouse\.io",
    r"job-boards\.greenhouse\.io",
    r"boards\.eu\.greenhouse\.io",
]

_APPLY_BUTTON_SELECTORS = [
    "#apply_button",
    "a:has-text('Apply for this Job')",
    "a:has-text('Apply Now')",
    "button:has-text('Apply')",
    ".job-application-btn",
]

_FORM_SELECTOR = "#application_form, form#application, form.application-form, form"


class GreenhouseAdapter(SiteAdapter):
    name = "greenhouse"
    priority = 8

    @classmethod
    def detect(cls, url: str, page_title: str = "", page_content: str = "") -> bool:
        url_lower = url.lower()
        return any(re.search(p, url_lower) for p in _GREENHOUSE_URL_PATTERNS)

    async def run(
        self,
        session: ApplySession,
        answer_engine: AnswerEngine,
        job_metadata: dict[str, Any] | None = None,
    ) -> AdapterResult:
        result = AdapterResult(adapter_name=self.name, site_name="Greenhouse")
        all_fills: list[FieldFillResult] = []

        try:
            await session.screenshot("01-greenhouse-listing")
            current_url = await session.current_url()
            _log.info(f"greenhouse adapter started | url={current_url}")

            # Navigate to application form (click Apply button if needed)
            on_form = await self._ensure_on_form(session)
            if not on_form:
                result.status = "failed"
                result.failure_reason = "Could not locate or navigate to the Greenhouse application form."
                await session.screenshot_failure("no-form-found")
                return result

            await session.wait_stable(1000)
            await session.screenshot("02-greenhouse-form")

            # Greenhouse forms are typically single-page — fill all fields
            fields = await detect_fields(session.page)
            _log.info(f"form fields found | count={len(fields)}")

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
                    site="greenhouse",
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
                    fill_result = await fill_field(
                        session.page, form_field, answer.value,
                        resume_path=answer_engine._profile.resume_path,
                    )

                all_fills.append(fill_result)

            await session.screenshot("03-greenhouse-filled")

            # Greenhouse is single-page: after filling, we're at the review/submit stage
            result.review_reached = True
            result.status = "draft_ready"
            result.step_count = 1
            result.fields_filled = all_fills
            result.notes.append("Greenhouse single-page form filled. Ready for human review before submit.")
            return result

        except Exception as exc:
            _log.error(f"greenhouse adapter error | error={exc}", exc_info=True)
            await session.screenshot_failure("adapter-exception")
            result.status = "failed"
            result.failure_reason = f"{type(exc).__name__}: {exc}"
            result.fields_filled = all_fills
            return result
        finally:
            result.screenshots = session.all_screenshots

    async def _ensure_on_form(self, session: ApplySession) -> bool:
        """Navigate to the application form if not already there."""
        # Check if form already visible
        form = await session.page.query_selector(_FORM_SELECTOR)
        if form:
            return True

        # Try clicking an Apply button
        apply_btn = await session.try_selectors(_APPLY_BUTTON_SELECTORS)
        if apply_btn:
            await apply_btn.click()
            await asyncio.sleep(1.5)
            form = await session.page.query_selector(_FORM_SELECTOR)
            return form is not None

        return False
