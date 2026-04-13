"""
Step and page state detection.

Answers: what kind of step is this? Is this the review page?
Where is the Next/Submit button?

Design: multiple heuristics, no LLM. Returns confidence scores so callers
can decide whether to trust the classification.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any

from ..observability import get_logger

_log = get_logger("detector")


class StepType(str, Enum):
    CONTACT = "contact"
    RESUME = "resume"
    SCREENING = "screening"
    EXPERIENCE = "experience"
    EDUCATION = "education"
    DEMOGRAPHIC = "demographic"
    REVIEW = "review"
    UNKNOWN = "unknown"


# Text patterns mapped to step types  (lowercase, order matters)
_STEP_PATTERNS: list[tuple[StepType, list[str]]] = [
    (StepType.REVIEW, [
        "review your application",
        "review application",
        "review & submit",
        "review and submit",
        "confirm your application",
        "summary",
        "preview your application",
        "application summary",
    ]),
    (StepType.RESUME, [
        "resume",
        "cv",
        "upload your resume",
        "attach resume",
        "resume upload",
        "upload cv",
        "upload resume",
    ]),
    (StepType.CONTACT, [
        "contact info",
        "contact information",
        "personal information",
        "personal details",
        "basic information",
        "your information",
    ]),
    (StepType.EXPERIENCE, [
        "work experience",
        "employment history",
        "work history",
        "professional experience",
        "experience",
    ]),
    (StepType.EDUCATION, [
        "education",
        "academic background",
        "educational background",
    ]),
    (StepType.DEMOGRAPHIC, [
        "voluntary disclosure",
        "diversity",
        "self-identification",
        "equal employment",
        "eeo",
        "demographic",
        "veteran status",
    ]),
    (StepType.SCREENING, [
        "additional questions",
        "screening questions",
        "application questions",
        "work authorization",
    ]),
]


# Button selectors for next/continue
_NEXT_BUTTON_SELECTORS = [
    "button[aria-label='Continue to next step']",
    "button[aria-label='Review your application']",
    "button[data-easy-apply-next-button]",
    "button:has-text('Next')",
    "button:has-text('Continue')",
    "button:has-text('Save and continue')",
    "button:has-text('Next step')",
    "[data-control-name='continue_unify']",
    ".jobs-easy-apply-footer button.artdeco-button--primary",
    "footer button.artdeco-button--primary",
    "button.artdeco-button--primary[type='button']",
]

_SUBMIT_BUTTON_SELECTORS = [
    "button[aria-label='Submit application']",
    "button:has-text('Submit application')",
    "button:has-text('Submit')",
    "button[type='submit']",
    "input[type='submit']",
]

_BACK_BUTTON_SELECTORS = [
    "button[aria-label='Go back to the previous step']",
    "button[aria-label='Back']",
    "button:has-text('Back')",
    "button:has-text('Previous')",
]


async def detect_step(page: Any) -> StepType:
    """
    Classify the current application step by inspecting visible page text.
    Returns StepType.UNKNOWN if no pattern matches.
    """
    try:
        # Check headings first (most reliable)
        heading_text = await _get_headings_text(page)
        step = _classify_text(heading_text)
        if step != StepType.UNKNOWN:
            _log.debug(f"step detected from heading | step={step} text={heading_text[:80]}")
            return step

        # Fall back to broader page text
        body_text = await _get_visible_text(page, max_chars=2000)
        step = _classify_text(body_text)
        _log.debug(f"step detected from body text | step={step}")
        return step
    except Exception as exc:
        _log.debug(f"step detection error | error={exc}")
        return StepType.UNKNOWN


async def is_review_page(page: Any) -> bool:
    """Quick check: is the current page the application review/summary step?"""
    step = await detect_step(page)
    if step == StepType.REVIEW:
        return True

    # Also check for submit button presence as secondary signal
    submit_btn = await find_submit_button(page)
    return submit_btn is not None


async def find_next_button(page: Any) -> Any | None:
    """Find the Next/Continue button. Returns the Playwright element or None."""
    for selector in _NEXT_BUTTON_SELECTORS:
        try:
            el = await page.query_selector(selector)
            if el and await el.is_visible() and await el.is_enabled():
                return el
        except Exception:
            continue
    return None


async def find_submit_button(page: Any) -> Any | None:
    """Find the Submit button. Returns the Playwright element or None."""
    for selector in _SUBMIT_BUTTON_SELECTORS:
        try:
            el = await page.query_selector(selector)
            if el and await el.is_visible() and await el.is_enabled():
                return el
        except Exception:
            continue
    return None


async def find_back_button(page: Any) -> Any | None:
    """Find the Back/Previous button."""
    for selector in _BACK_BUTTON_SELECTORS:
        try:
            el = await page.query_selector(selector)
            if el and await el.is_visible():
                return el
        except Exception:
            continue
    return None


async def get_validation_errors(page: Any) -> list[str]:
    """
    Extract any visible validation error messages.
    Useful for detecting why a step advance failed.
    """
    errors: list[str] = []
    error_selectors = [
        ".artdeco-inline-feedback--error",
        "[role='alert']",
        ".error-message",
        ".field-error",
        "[aria-invalid='true']",
        ".invalid-feedback",
        ".form-error",
    ]
    for selector in error_selectors:
        try:
            els = await page.query_selector_all(selector)
            for el in els:
                text = await el.inner_text()
                if text.strip():
                    errors.append(text.strip())
        except Exception:
            continue
    return list(dict.fromkeys(errors))  # deduplicate while preserving order


async def has_required_unfilled(page: Any) -> bool:
    """Detect if there are required fields that appear empty/unvalidated."""
    try:
        # Look for aria-required elements that have no value
        result = await page.evaluate("""
            () => {
                const required = document.querySelectorAll(
                    '[required], [aria-required="true"]'
                );
                for (const el of required) {
                    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
                        if (!el.value.trim()) return true;
                    }
                    if (el.tagName === 'SELECT') {
                        if (!el.value || el.value === '' || el.selectedIndex <= 0) return true;
                    }
                }
                return false;
            }
        """)
        return bool(result)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _classify_text(text: str) -> StepType:
    text_lower = text.lower()
    for step_type, patterns in _STEP_PATTERNS:
        for pattern in patterns:
            if pattern in text_lower:
                return step_type
    return StepType.UNKNOWN


async def _get_headings_text(page: Any) -> str:
    try:
        return await page.evaluate("""
            () => {
                const headings = document.querySelectorAll('h1, h2, h3, [role="heading"]');
                return Array.from(headings).map(h => h.innerText).join(' ');
            }
        """)
    except Exception:
        return ""


async def _get_visible_text(page: Any, max_chars: int = 2000) -> str:
    try:
        text = await page.evaluate("() => document.body.innerText")
        return text[:max_chars] if text else ""
    except Exception:
        return ""
