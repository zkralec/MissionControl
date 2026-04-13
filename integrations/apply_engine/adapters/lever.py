"""
Lever adapter.

Lever (jobs.lever.co) is a common ATS with a consistent single-page application form.

Lever application flow:
  1. Job listing at jobs.lever.co/{company}/{id}
  2. "Apply" button → application form (same page, scrolls down)
  3. Single form: Contact info, Resume upload, optional questions
  4. "Submit application" button at bottom
  5. We stop BEFORE submit

Lever URLs:
  - https://jobs.lever.co/company/uuid
  - https://jobs.lever.co/company/uuid/apply
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from ..answer_engine import AnswerEngine, FormQuestion
from ..forms.classifier import detect_fields
from ..forms.handlers import FieldFillResult, fill_field
from ..observability import get_logger
from ..session import ApplySession
from .base import AdapterResult, SiteAdapter

_log = get_logger("lever")

_LEVER_URL_PATTERNS = [
    r"jobs\.lever\.co",
    r"lever\.co/.*apply",
]

_APPLY_BUTTON_SELECTORS = [
    "a.template-btn-submit",
    "a:has-text('Apply for this job')",
    "a:has-text('Apply Now')",
    "button:has-text('Apply')",
]

_FORM_SELECTOR = "form.application-form, #application-form, form[action*='apply']"


class LeverAdapter(SiteAdapter):
    name = "lever"
    priority = 8

    @classmethod
    def detect(cls, url: str, page_title: str = "", page_content: str = "") -> bool:
        url_lower = url.lower()
        return any(re.search(p, url_lower) for p in _LEVER_URL_PATTERNS)

    async def run(
        self,
        session: ApplySession,
        answer_engine: AnswerEngine,
        job_metadata: dict[str, Any] | None = None,
    ) -> AdapterResult:
        result = AdapterResult(adapter_name=self.name, site_name="Lever")
        all_fills: list[FieldFillResult] = []

        try:
            await session.screenshot("01-lever-listing")
            _log.info(f"lever adapter started | url={await session.current_url()}")

            # Navigate to apply form
            on_form = await self._ensure_on_form(session)
            if not on_form:
                result.status = "failed"
                result.failure_reason = "Could not locate Lever application form."
                await session.screenshot_failure("no-form-found")
                return result

            await session.wait_stable(1000)
            await session.screenshot("02-lever-form")

            fields = await detect_fields(session.page)
            _log.info(f"fields detected | count={len(fields)}")

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
                    site="lever",
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

            await session.screenshot("03-lever-filled")
            result.review_reached = True
            result.status = "draft_ready"
            result.step_count = 1
            result.fields_filled = all_fills
            return result

        except Exception as exc:
            _log.error(f"lever adapter error | error={exc}", exc_info=True)
            await session.screenshot_failure("adapter-exception")
            result.status = "failed"
            result.failure_reason = str(exc)
            result.fields_filled = all_fills
            return result
        finally:
            result.screenshots = session.all_screenshots

    async def _ensure_on_form(self, session: ApplySession) -> bool:
        # If already on /apply URL or form is visible
        url = await session.current_url()
        if "/apply" in url:
            form = await session.page.query_selector(_FORM_SELECTOR)
            if form:
                return True

        apply_btn = await session.try_selectors(_APPLY_BUTTON_SELECTORS)
        if apply_btn:
            await apply_btn.click()
            await asyncio.sleep(1.5)
            return True

        return False
