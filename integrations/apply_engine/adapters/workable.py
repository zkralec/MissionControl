"""
Workable adapter.

Workable (apply.workable.com) is a common ATS platform. Application forms vary
by employer but typically follow a single-page or short multi-step flow.

Detection: hostname contains workable.com — checked against the hostname only so
that utm_source=workable or similar tracking params on other portals do not match.

Priority: 8 — same as Greenhouse and Lever, ahead of Generic (0) and Avature (7).

Current implementation: delegates to the Generic adapter's form-filling logic.
A dedicated Workable implementation can replace this stub when portal-specific
selectors are needed.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urlparse

from ..answer_engine import AnswerEngine, FormQuestion
from ..forms.classifier import detect_fields
from ..forms.detector import (
    StepType,
    detect_step,
    get_validation_errors,
)
from ..forms.handlers import FieldFillResult, fill_field
from ..observability import get_logger
from ..session import ApplySession
from .base import AdapterResult, SiteAdapter

_log = get_logger("workable")

MAX_STEPS = 15

_APPLY_BUTTON_SELECTORS = [
    "a:has-text('Apply Now')",
    "button:has-text('Apply Now')",
    "a:has-text('Apply')",
    "button:has-text('Apply')",
    "[data-ui='apply-button']",
    ".apply-button",
]

# Workable-specific navigation button selectors.
# Listed most-specific first so Workable's own elements win over generic fallbacks.
_WORKABLE_NEXT_SELECTORS = [
    "[data-ui='next-btn']",
    "button[data-ui*='next']",
    "button:has-text('Next step')",
    "button:has-text('Next')",
    "button:has-text('Continue')",
    "button:has-text('Save and continue')",
]

_WORKABLE_SUBMIT_SELECTORS = [
    "[data-ui='submit-btn']",
    "button[data-ui*='submit']",
    "button:has-text('Submit Application')",
    "button:has-text('Submit application')",
    "button:has-text('Submit')",
    "button[type='submit']",
    "input[type='submit']",
]

# Selectors that indicate a cookie consent modal is blocking interaction.
_COOKIE_BACKDROP_SELECTORS = [
    "[data-ui='backdrop']",
    "[data-ui='cookie-consent']",
    "div[role='dialog'][aria-label*='Cookie' i]",
    "div[role='dialog'][aria-label*='cookie' i]",
    "#cookie-consent",
    ".cookie-consent",
    ".cookie-banner",
    ".cookie-modal",
    "[id*='cookie'][role='dialog']",
    "[class*='cookie'][role='dialog']",
]

# Buttons inside (or near) the cookie modal to accept/dismiss it.
_COOKIE_DISMISS_SELECTORS = [
    # Specific Workable cookie consent buttons first
    "[data-ui='cookie-consent'] button:has-text('Accept')",
    "[data-ui='cookie-consent'] button:has-text('Agree')",
    "[data-ui='cookie-consent'] button:has-text('OK')",
    "[data-ui='cookie-consent'] button:has-text('Close')",
    "[data-ui='cookie-consent'] button",
    # Generic accept/agree buttons
    "button:has-text('Accept all')",
    "button:has-text('Accept All')",
    "button:has-text('Accept cookies')",
    "button:has-text('Accept Cookies')",
    "button:has-text('Accept')",
    "button:has-text('Agree')",
    "button:has-text('I agree')",
    "button:has-text('I Agree')",
    "button:has-text('OK')",
    "button:has-text('Got it')",
    "button:has-text('Got It')",
    # Close / dismiss
    "div[role='dialog'][aria-label*='Cookie' i] button[aria-label*='Close' i]",
    "div[role='dialog'][aria-label*='Cookie' i] button[aria-label*='Dismiss' i]",
    "div[role='dialog'][aria-label*='Cookie' i] button",
    # Backdrop itself (some portals dismiss on backdrop click)
    "[data-ui='backdrop']",
]


class WorkableAdapter(SiteAdapter):
    name = "workable"
    priority = 8

    @classmethod
    def detect(cls, url: str, page_title: str = "", page_content: str = "") -> bool:
        # Hostname-only check — query params like utm_source=workable.com must not match.
        try:
            hostname = urlparse(url).hostname or ""
            return "workable.com" in hostname.lower()
        except Exception:
            return False

    async def run(
        self,
        session: ApplySession,
        answer_engine: AnswerEngine,
        job_metadata: dict[str, Any] | None = None,
    ) -> AdapterResult:
        result = AdapterResult(adapter_name=self.name, site_name="Workable")
        all_fills: list[FieldFillResult] = []

        try:
            await session.screenshot("01-workable-landing")
            _log.info(f"workable adapter started | url={await session.current_url()}")

            # Click an Apply button if not already on the form.
            # Returns None on success, or an error string if blocked.
            nav_error = await self._try_navigate_to_form(session)
            if nav_error:
                _log.error(f"workable navigation blocked | reason={nav_error}")
                await session.screenshot_failure("workable-nav-blocked")
                result.status = "failed"
                result.failure_reason = nav_error
                return result
            await session.wait_stable(1200)
            await session.screenshot("02-workable-form")

            step_index = 0
            while step_index < MAX_STEPS:
                step_index += 1
                step_type = await detect_step(session.page)
                address_widget_filled = False

                # STOP CONDITION: explicit review-page heading in the DOM.
                # Do NOT use is_review_page() here — it falls back to submit-button
                # presence, which fires falsely on Workable's single-page form
                # (the Submit button sits on the same page as all the fields).
                # The submit-button stop is handled correctly below, after filling.
                if step_type == StepType.REVIEW:
                    _log.info(f"review page heading detected | step={step_index}")
                    await session.screenshot(f"{step_index:02d}-workable-review")
                    result.review_reached = True
                    result.status = "draft_ready"
                    result.step_count = step_index
                    result.fields_filled = all_fills
                    result.notes.append(
                        "Workable review page reached. Human review required before submitting."
                    )
                    return result

                fields = await detect_fields(session.page)
                _log.info(f"fields detected | step={step_index} count={len(fields)}")

                for form_field in fields:
                    label = form_field.label or form_field.aria_label or form_field.name_attr
                    if not label:
                        continue

                    if form_field.field_type == "file":
                        resume_path = answer_engine._profile.resume_path
                        fill_result = await fill_field(
                            session.page, form_field, resume_path, resume_path=resume_path
                        )
                    else:
                        question = FormQuestion(
                            label=label,
                            field_type=form_field.field_type,
                            options=form_field.options,
                            required=form_field.required,
                            placeholder=form_field.placeholder,
                            name_attr=form_field.name_attr,
                            id_attr=form_field.id_attr,
                            context_text=form_field.context_text,
                            site="workable",
                        )
                        answer = answer_engine.answer(question)
                        if not answer.found:
                            all_fills.append(FieldFillResult(
                                field_label=label, field_type=form_field.field_type,
                                status="skipped", value_preview="no answer",
                            ))
                            continue
                        if (
                            address_widget_filled
                            and answer.canonical_key in {
                                "city", "state_or_province", "postal_code", "country", "current_location"
                            }
                            and not (form_field.label or form_field.aria_label).strip()
                        ):
                            all_fills.append(FieldFillResult(
                                field_label=label,
                                field_type=form_field.field_type,
                                status="skipped",
                                value_preview="covered by address autocomplete",
                                required=form_field.required,
                            ))
                            continue
                        if self._is_workable_address_field(form_field, answer.canonical_key):
                            fill_result = await self._fill_workable_address(
                                session,
                                answer_engine,
                                form_field,
                            )
                            if fill_result.success:
                                address_widget_filled = True
                            all_fills.append(fill_result)
                            continue
                        fill_result = await fill_field(session.page, form_field, answer.value)

                    all_fills.append(fill_result)

                await session.screenshot(f"{step_index:02d}-workable-filled")

                # Use Workable-specific selectors instead of the generic ones
                # (generic selectors are mostly LinkedIn-specific).
                submit_btn = await self._find_workable_submit(session)
                next_btn = await self._find_workable_next(session)

                if submit_btn and not next_btn:
                    _log.info("submit button found after filling — stopping for human review")
                    await session.screenshot(f"{step_index:02d}-workable-pre-submit")
                    result.review_reached = True
                    result.status = "draft_ready"
                    result.step_count = step_index
                    result.fields_filled = all_fills
                    result.notes.append(
                        "Workable submit button detected. Human review required before submitting."
                    )
                    return result

                if next_btn:
                    await next_btn.click()
                    await asyncio.sleep(1.0)
                    errors = await get_validation_errors(session.page)
                    if errors:
                        _log.warning(f"validation errors | step={step_index} errors={errors[:3]}")
                        await session.screenshot(f"{step_index:02d}-workable-validation-errors")
                        result.status = "partial"
                        result.failure_reason = (
                            f"Validation errors on step {step_index}: {'; '.join(errors[:3])}"
                        )
                        result.step_count = step_index
                        result.fields_filled = all_fills
                        return result
                else:
                    _log.warning(f"no next or submit button | step={step_index}")
                    result.status = "partial"
                    result.failure_reason = (
                        f"Could not navigate past step {step_index} — no navigation buttons found."
                    )
                    result.step_count = step_index
                    result.fields_filled = all_fills
                    return result

            result.status = "partial"
            result.failure_reason = f"Did not reach review within {MAX_STEPS} steps."
            result.step_count = step_index
            result.fields_filled = all_fills
            return result

        except Exception as exc:
            _log.error(f"workable adapter error | error={exc}", exc_info=True)
            await session.screenshot_failure("workable-exception")
            result.status = "failed"
            result.failure_reason = f"{type(exc).__name__}: {exc}"
            result.fields_filled = all_fills
            return result
        finally:
            result.screenshots = session.all_screenshots

    async def _try_navigate_to_form(self, session: ApplySession) -> str | None:
        """
        Click the Apply button to navigate into the application form.

        Returns None on success (or when no Apply button exists — already on form).
        Returns an error string if a cookie consent modal could not be dismissed.
        """
        apply_btn = await session.try_selectors(_APPLY_BUTTON_SELECTORS)
        if not apply_btn:
            return None  # already on the form, nothing to click

        # Dismiss any cookie consent modal before trying to click Apply.
        cookie_error = await self._dismiss_cookie_consent(session)
        if cookie_error:
            return cookie_error

        _log.debug("clicking apply button")
        await apply_btn.click()
        await asyncio.sleep(1.5)
        return None

    async def _dismiss_cookie_consent(self, session: ApplySession) -> str | None:
        """
        Detect and dismiss a cookie consent modal/backdrop if present.

        Returns None when nothing was blocking or dismissal succeeded.
        Returns an error string when a modal was found but could not be cleared.
        """
        # Check if any known cookie/backdrop element is visible.
        backdrop = await session.try_selectors(_COOKIE_BACKDROP_SELECTORS)
        if not backdrop:
            return None  # nothing blocking — fast path

        _log.info("cookie consent modal/backdrop detected — attempting dismissal")
        await session.screenshot("cookie-consent-detected")

        dismissed = False
        for sel in _COOKIE_DISMISS_SELECTORS:
            try:
                el = await session.page.query_selector(sel)
                if el and await el.is_visible():
                    label = (await el.inner_text()).strip() or sel
                    _log.debug(f"dismissing cookie consent | action=click selector={sel!r} label={label!r}")
                    await el.click()
                    dismissed = True
                    break
            except Exception as exc:
                _log.debug(f"cookie dismiss attempt failed | selector={sel!r} error={exc}")
                continue

        if not dismissed:
            _log.warning("no cookie dismiss button found — cannot clear modal")
            return "cookie-consent-blocked: modal present but no dismiss button matched"

        _log.debug("dismissal clicked — waiting for backdrop to disappear")

        # Wait up to 4 s for the backdrop to vanish.
        backdrop_gone = False
        for _ in range(8):
            await asyncio.sleep(0.5)
            still_present = await session.try_selectors(_COOKIE_BACKDROP_SELECTORS)
            if not still_present:
                backdrop_gone = True
                break

        if backdrop_gone:
            _log.info("cookie consent backdrop dismissed — proceeding to Apply click")
            await session.screenshot("cookie-consent-dismissed")
            return None
        else:
            _log.warning("backdrop still present after dismissal — Apply click will be blocked")
            return "cookie-consent-blocked: backdrop did not disappear after dismiss click"

    async def _find_workable_next(self, session: ApplySession) -> Any | None:
        """Find the next/continue button using Workable-specific selectors first."""
        for selector in _WORKABLE_NEXT_SELECTORS:
            try:
                el = await session.page.query_selector(selector)
                if el and await el.is_visible() and await el.is_enabled():
                    return el
            except Exception:
                continue
        return None

    async def _find_workable_submit(self, session: ApplySession) -> Any | None:
        """Find the submit button using Workable-specific selectors first."""
        for selector in _WORKABLE_SUBMIT_SELECTORS:
            try:
                el = await session.page.query_selector(selector)
                if el and await el.is_visible() and await el.is_enabled():
                    return el
            except Exception:
                continue
        return None

    def _is_workable_address_field(self, form_field: Any, canonical_key: str | None) -> bool:
        label = (form_field.label or form_field.aria_label or "").strip().lower()
        return canonical_key == "address_line_1" or label == "address"

    async def _fill_workable_address(
        self,
        session: ApplySession,
        answer_engine: AnswerEngine,
        form_field: Any,
    ) -> FieldFillResult:
        address = self._compose_workable_address(answer_engine)
        if not address:
            return FieldFillResult(
                field_label=form_field.label or "Address",
                field_type=form_field.field_type,
                status="skipped",
                value_preview="no address configured",
                required=form_field.required,
            )

        target = await self._resolve_workable_text_input(form_field.locator)
        if not target:
            return FieldFillResult(
                field_label=form_field.label or "Address",
                field_type=form_field.field_type,
                status="failed",
                value_preview=address,
                required=form_field.required,
                error="no address input target found",
            )

        try:
            await target.fill(address)
            await asyncio.sleep(0.4)
            selected = await self._select_workable_address_suggestion(session, address)
            await asyncio.sleep(0.4)
            current_value = await self._read_locator_value(target)
            if current_value and self._address_matches(current_value, address):
                _log.info(
                    "workable address filled | "
                    f"selected_suggestion={selected} value={current_value}"
                )
                return FieldFillResult(
                    field_label=form_field.label or "Address",
                    field_type=form_field.field_type,
                    status="filled",
                    value_preview=address[:40] + ("..." if len(address) > 40 else ""),
                    required=form_field.required,
                    selector_used="workable-address-autocomplete",
                )
            return FieldFillResult(
                field_label=form_field.label or "Address",
                field_type=form_field.field_type,
                status="failed",
                value_preview=address,
                required=form_field.required,
                error="address autocomplete did not retain expected value",
                selector_used="workable-address-autocomplete",
            )
        except Exception as exc:
            return FieldFillResult(
                field_label=form_field.label or "Address",
                field_type=form_field.field_type,
                status="failed",
                value_preview=address,
                required=form_field.required,
                error=str(exc),
                selector_used="workable-address-autocomplete",
            )

    def _compose_workable_address(self, answer_engine: AnswerEngine) -> str:
        profile = answer_engine._profile
        line1 = profile.get_str("address_line_1") or ""
        city = profile.get_str("city") or ""
        state = profile.get_str("state") or profile.get_str("state_or_province") or ""
        postal = profile.get_str("postal_code") or ""

        parts = [line1]
        locality = " ".join(part for part in (city, state) if part)
        if locality:
            parts.append(locality)
        if postal:
            parts.append(postal)
        return ", ".join(part for part in parts if part).strip()

    async def _resolve_workable_text_input(self, locator: Any) -> Any | None:
        try:
            tag = await locator.evaluate("el => (el.tagName || '').toLowerCase()")
            if tag in {"input", "textarea"}:
                return locator
        except Exception:
            pass

        for selector in (
            "input:not([type='hidden']):not([type='file'])",
            "textarea",
            "[data-role='illustrated-input'] input",
            "[data-role='illustrated-input'] textarea",
        ):
            try:
                inner = await locator.query_selector(selector)
                if inner:
                    _log.info(f"workable inner selector chosen | selector={selector}")
                    return inner
            except Exception:
                continue
        return None

    async def _select_workable_address_suggestion(self, session: ApplySession, address: str) -> bool:
        option_selectors = (
            "[role='listbox'] [role='option']",
            "[role='option']",
            "[data-ui*='option']",
            "[data-ui*='suggestion']",
            "li[role='option']",
            "ul li",
        )
        target_fragments = [
            fragment for fragment in (
                address.lower(),
                (address.split(",")[0] or "").strip().lower(),
                "monkton",
            )
            if fragment
        ]
        for selector in option_selectors:
            try:
                options = await session.page.query_selector_all(selector)
            except Exception:
                continue
            for option in options[:10]:
                try:
                    if not await option.is_visible():
                        continue
                    text = re.sub(r"\s+", " ", (await option.inner_text()).strip()).lower()
                    if any(fragment in text for fragment in target_fragments):
                        await option.click()
                        _log.info(f"workable address autocomplete selected | selector={selector} text={text}")
                        return True
                except Exception:
                    continue
        try:
            await session.page.keyboard.press("ArrowDown")
            await session.page.keyboard.press("Enter")
            _log.info("workable address autocomplete selected via keyboard")
            return True
        except Exception:
            return False

    async def _read_locator_value(self, locator: Any) -> str:
        try:
            return await locator.input_value()
        except Exception:
            try:
                return await locator.evaluate("el => typeof el.value === 'string' ? el.value : ''")
            except Exception:
                return ""

    def _address_matches(self, actual: str, expected: str) -> bool:
        actual_compact = "".join(ch.lower() for ch in actual if ch.isalnum())
        expected_compact = "".join(ch.lower() for ch in expected if ch.isalnum())
        line1 = expected.split(",")[0].strip().lower()
        return (
            actual_compact == expected_compact
            or (expected_compact and expected_compact in actual_compact)
            or (line1 and line1.replace(" ", "") in actual_compact)
        )
