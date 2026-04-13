"""
Avature adapter.

Avature (*.avature.net) is an enterprise ATS used by companies such as Epic Systems.

Typical Avature application flow:
  1. RegisterMethod  — method-choice / login / account-creation page
  2. Resume upload or LinkedIn-profile import
  3. Contact / profile information
  4. Screening / questionnaire steps
  5. Review / confirmation page

Avature uses JS-driven navigation controls that often are NOT standard
<button type="submit"> elements — they may be <a> tags, <span role="button">,
<div onclick=...>, or <input type="button"> with custom CSS classes.  The
generic adapter's find_next_button() misses all of these, which is why it
falls through with "no navigation buttons found."

Detection:  *.avature.net in URL
Priority:   7  (below LinkedIn / Greenhouse / Lever at 8, above Generic at 0)
"""
from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from ..answer_engine import AnswerEngine, FormQuestion
from ..forms.classifier import detect_fields
from ..forms.detector import StepType, detect_step, get_validation_errors, is_review_page
from ..forms.handlers import FieldFillResult, fill_field
from ..observability import get_logger
from ..session import ApplySession
from .base import AdapterResult, SiteAdapter

_log = get_logger("avature")

MAX_STEPS = 15

_AVATURE_URL_PATTERNS = [
    r"avature\.net",
]

# Navigation text labels in priority order.
# Checked against the visible text of every candidate element.
_NAV_TEXTS = [
    "Save and Continue",
    "Continue",
    "Next",
    "Apply Now",
    "Apply",
    "Register",
    "Submit Application",
    "Submit",
    "Upload Resume",
    "Upload",
    "Proceed",
    "Save",
    "Confirm",
]

# Selector templates tried (in order) for each nav text above.
# {text} is substituted with the nav label.
_NAV_SELECTOR_TEMPLATES = [
    "button:has-text('{text}')",
    "a:has-text('{text}')",
    "[role='button']:has-text('{text}')",
    "input[type='submit'][value*='{text}']",
    "input[type='button'][value*='{text}']",
    "span[onclick]:has-text('{text}')",
    "div[onclick]:has-text('{text}')",
    "[tabindex='0']:has-text('{text}')",
]

# Avature-specific step classification.
# Each tuple is (step_name, [text/URL substrings that identify it]).
# Checked in order; first match wins.
#
# NOTE: "profile_dashboard" must be checked BEFORE "profile" so that
# /Careers/Profile (the post-login landing page) is not mistaken for a
# form-filling step.
_AVATURE_STEP_PATTERNS: list[tuple[str, list[str]]] = [
    ("review", [
        "review your application", "review application", "review & submit",
        "review and submit", "confirm your application", "application summary",
        "preview your application",
    ]),
    ("register", [
        "registermethod", "how would you like to apply",
        "sign in to", "sign in with", "create an account", "create account",
        "log in to apply", "/login", "/signin", "/sign-in",
        "already have an account", "existing user",
    ]),
    # ApplyConfirmation — the interstitial page that appears after login with an
    # "Apply Now" button.  Must be checked before profile_dashboard.
    ("apply_confirmation", [
        "/careers/applyconfirmation",
    ]),
    # Post-login profile/dashboard landing — NOT a form step, needs redirect recovery.
    ("profile_dashboard", [
        "/careers/profile",
        "/careers/dashboard",
        "/careers/home",
    ]),
    ("resume", [
        "upload resume", "upload your resume", "attach resume",
        "import from linkedin", "upload cv",
    ]),
    ("profile", [
        "personal information", "contact information", "your profile",
        "basic information",
    ]),
    ("screening", [
        "additional questions", "screening questions", "questionnaire",
        "application questions",
    ]),
]


class AvatureAdapter(SiteAdapter):
    name = "avature"
    priority = 7

    @classmethod
    def detect(cls, url: str, page_title: str = "", page_content: str = "") -> bool:
        url_lower = url.lower()
        return any(re.search(p, url_lower) for p in _AVATURE_URL_PATTERNS)

    async def run(
        self,
        session: ApplySession,
        answer_engine: AnswerEngine,
        job_metadata: dict[str, Any] | None = None,
    ) -> AdapterResult:
        result = AdapterResult(adapter_name=self.name, site_name="Avature")
        all_fills: list[FieldFillResult] = []

        try:
            await session.screenshot("01-avature-landing")
            # Capture the original application URL before any login redirect changes it.
            target_url = await session.current_url()
            _log.info(f"avature adapter started | target_url={target_url}")

            step_index = 0
            while step_index < MAX_STEPS:
                step_index += 1

                url = await session.current_url()
                page_text = await _get_page_text(session.page)
                avature_step = _classify_avature_step(url, page_text)
                generic_step = await detect_step(session.page)

                _log.info(
                    f"avature step | index={step_index} "
                    f"avature_step={avature_step} generic_step={generic_step} "
                    f"url={url[:100]}"
                )
                await session.screenshot(f"{step_index:02d}-avature-{avature_step}")

                # --- Reached review/confirmation — stop here (draft mode) ---
                if (
                    avature_step == "review"
                    or generic_step == StepType.REVIEW
                    or await is_review_page(session.page)
                ):
                    _log.info(f"review page reached | step={step_index}")
                    result.review_reached = True
                    result.status = "draft_ready"
                    result.step_count = step_index
                    result.fields_filled = all_fills
                    result.notes.append(
                        "Avature review/confirmation page reached. "
                        "Human review required before submitting."
                    )
                    return result

                # --- Login / registration wall ---
                if avature_step == "register":
                    _log.info(f"register/login page detected | step={step_index}")

                    email = (job_metadata or {}).get("avature_email")
                    password = (job_metadata or {}).get("avature_password")

                    if email and password:
                        logged_in = await _handle_login(session, email, password)
                        if logged_in:
                            result.notes.append(
                                f"Step {step_index}: Logged into Avature as {email}."
                            )
                            await session.wait_stable(2000)
                            continue   # re-enter loop to classify the post-login page
                        else:
                            _log.warning("login attempt failed — falling through to nav search")
                            result.notes.append(
                                f"Step {step_index}: Login attempt for {email} did not succeed. "
                                "Attempting nav button as fallback."
                            )
                    else:
                        _log.info(
                            "no credentials supplied — skipping login attempt. "
                            "Set AVATURE_EMAIL and AVATURE_PASSWORD in .env to enable."
                        )
                        result.notes.append(
                            f"Step {step_index}: Login/registration page detected but no "
                            "credentials configured (AVATURE_EMAIL / AVATURE_PASSWORD)."
                        )
                    # No credentials or login failed — fall through to nav button search.

                # --- ApplyConfirmation interstitial — click "Apply Now" to enter the flow ---
                elif avature_step == "apply_confirmation":
                    _log.info(
                        f"ApplyConfirmation page detected | step={step_index} url={url[:100]}"
                    )
                    await session.screenshot(f"{step_index:02d}-avature-apply-confirmation")

                    apply_btn = await _find_apply_now_button(session.page)
                    if apply_btn is None:
                        candidates = await _scan_clickable_elements(session.page)
                        _log.warning(
                            f"ApplyConfirmation: no 'Apply Now' button found | "
                            f"candidate_elements={len(candidates)}"
                        )
                        for c in candidates:
                            _log.warning(
                                f"  clickable | tag={c['tag']} text={repr(c['text'][:60])} "
                                f"role={repr(c['role'])} href={repr(c['href'][:50])}"
                            )
                        await session.save_html(f"{step_index:02d}-avature-apply-confirmation-no-btn")
                        result.status = "partial"
                        result.failure_reason = (
                            f"Step {step_index}: Reached ApplyConfirmation but could not find "
                            "an 'Apply Now' button. Check logs and HTML snapshot."
                        )
                        result.step_count = step_index
                        result.fields_filled = all_fills
                        return result

                    _log.info("clicking Apply Now on ApplyConfirmation page")
                    await apply_btn.click()
                    await session.wait_stable(2000)
                    await session.screenshot(f"{step_index:02d}-avature-after-apply-now")
                    result.notes.append(
                        f"Step {step_index}: Clicked 'Apply Now' on ApplyConfirmation page."
                    )
                    continue  # re-classify the page we land on

                # --- Post-login profile/dashboard redirect recovery ---
                elif avature_step == "profile_dashboard":
                    _log.info(
                        f"post-login profile/dashboard detected | step={step_index} "
                        f"current_url={url[:100]} original_target={target_url[:100]}"
                    )

                    # Derive the ApplyConfirmation URL from the original job link.
                    # e.g. /Careers/RegisterMethod?folderId=742&source=LinkedIn
                    #   -> /Careers/ApplyConfirmation?folderId=742&source=LinkedIn
                    confirmation_url = _derive_apply_confirmation_url(target_url)
                    _log.info(
                        f"derived ApplyConfirmation url | url={confirmation_url or '(none)'}"
                    )

                    if confirmation_url:
                        result.notes.append(
                            f"Step {step_index}: Redirected to Profile after login. "
                            f"Navigating to derived ApplyConfirmation URL."
                        )
                        await session.navigate(confirmation_url)
                        await session.wait_stable(2000)
                        await session.screenshot(f"{step_index:02d}-avature-post-login-recovery")

                        recovered_url = await session.current_url()
                        recovered_step = _classify_avature_step(
                            recovered_url, await _get_page_text(session.page)
                        )
                        _log.info(
                            f"post-login recovery result | "
                            f"url={recovered_url[:100]} step={recovered_step}"
                        )

                        if recovered_step == "profile_dashboard":
                            # Portal still redirects back to Profile — profile completion required.
                            await session.save_html(f"{step_index:02d}-avature-profile-required")
                            result.status = "profile_required"
                            result.failure_reason = (
                                "Avature portal redirected back to Profile after navigating to "
                                f"ApplyConfirmation ({confirmation_url}). Profile completion may "
                                "be required before applying. Complete your profile and re-run."
                            )
                            result.step_count = step_index
                            result.fields_filled = all_fills
                            return result

                        # Successfully recovered — re-classify the new page.
                        continue
                    else:
                        # Can't derive a confirmation URL; fall through to nav button search
                        # so we still log candidate elements if that also fails.
                        _log.warning(
                            "could not derive ApplyConfirmation URL from target — "
                            f"target_url={target_url}"
                        )
                        result.notes.append(
                            f"Step {step_index}: On Profile page; could not derive "
                            "ApplyConfirmation URL. Trying nav button fallback."
                        )

                # --- Fill visible form fields (non-register, non-dashboard steps) ---
                else:
                    fields = await detect_fields(session.page)
                    _log.info(f"fields detected | step={step_index} count={len(fields)}")

                    for form_field in fields:
                        label = (
                            form_field.label
                            or form_field.aria_label
                            or form_field.name_attr
                        )
                        if not label:
                            continue

                        if form_field.field_type == "file":
                            resume_path = answer_engine._profile.resume_path
                            fill_result = await fill_field(
                                session.page, form_field, resume_path,
                                resume_path=resume_path,
                            )
                            all_fills.append(fill_result)
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
                                site="avature",
                            )
                            answer = answer_engine.answer(question)
                            if not answer.found:
                                all_fills.append(FieldFillResult(
                                    field_label=label,
                                    field_type=form_field.field_type,
                                    status="skipped",
                                    value_preview="no answer",
                                    required=form_field.required,
                                ))
                                continue
                            fill_result = await fill_field(
                                session.page, form_field, answer.value,
                            )
                            all_fills.append(fill_result)

                    await session.screenshot(f"{step_index:02d}-avature-{avature_step}-filled")

                # --- Find navigation button ---
                nav_btn = await _find_nav_button(session.page)

                if nav_btn is None:
                    # Diagnostics: dump every visible clickable element to logs and HTML
                    candidates = await _scan_clickable_elements(session.page)
                    _log.warning(
                        f"no nav button found | step={step_index} step_type={avature_step} "
                        f"candidate_elements={len(candidates)}"
                    )
                    for c in candidates:
                        _log.warning(
                            f"  clickable | tag={c['tag']} "
                            f"text={repr(c['text'][:60])} "
                            f"role={repr(c['role'])} "
                            f"type={repr(c['type'])} "
                            f"onclick={c['onclick']} "
                            f"tabindex={repr(c['tabindex'])} "
                            f"class={repr(c['className'][:50])} "
                            f"href={repr(c['href'][:50])}"
                        )
                    await session.screenshot(f"{step_index:02d}-avature-no-nav")
                    await session.save_html(f"{step_index:02d}-avature-no-nav")

                    result.status = "partial"
                    result.failure_reason = (
                        f"Step {step_index} ({avature_step}): No navigation control found. "
                        f"Scanned {len(candidates)} candidate element(s) — "
                        "check WARNING logs and the saved HTML snapshot for details."
                    )
                    result.step_count = step_index
                    result.fields_filled = all_fills
                    return result

                _log.info(f"clicking nav button | step={step_index}")
                await nav_btn.click()
                await asyncio.sleep(1.5)

                # Check for validation errors after clicking
                errors = await get_validation_errors(session.page)
                if errors:
                    _log.warning(
                        f"validation errors | step={step_index} errors={errors[:3]}"
                    )
                    await session.screenshot(f"{step_index:02d}-avature-validation-errors")
                    result.status = "partial"
                    result.failure_reason = (
                        f"Validation errors on step {step_index}: "
                        f"{'; '.join(errors[:3])}"
                    )
                    result.step_count = step_index
                    result.fields_filled = all_fills
                    return result

                await session.wait_stable(500)

            # Exhausted step limit
            result.status = "partial"
            result.failure_reason = f"Did not reach review within {MAX_STEPS} steps."
            result.step_count = step_index
            result.fields_filled = all_fills
            return result

        except Exception as exc:
            _log.error(f"avature adapter error | error={exc}", exc_info=True)
            await session.screenshot_failure("avature-exception")
            result.status = "failed"
            result.failure_reason = f"{type(exc).__name__}: {exc}"
            result.fields_filled = all_fills
            return result
        finally:
            result.screenshots = session.all_screenshots


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _derive_apply_confirmation_url(original_url: str) -> str | None:
    """
    Derive the ApplyConfirmation URL from the original Avature job URL.

    Avature's post-login interstitial lives at the same base path with the
    page name replaced by 'ApplyConfirmation' and the same query params:

        /Careers/RegisterMethod?folderId=742&source=LinkedIn
     -> /Careers/ApplyConfirmation?folderId=742&source=LinkedIn

    Returns None if folderId cannot be found in the query string.
    """
    try:
        parsed = urlparse(original_url)
        params = parse_qs(parsed.query, keep_blank_values=True)

        # folderId is required — without it Avature won't know which job.
        folder_id_list = params.get("folderId") or params.get("folderid") or params.get("FolderId")
        if not folder_id_list:
            return None
        folder_id = folder_id_list[0]

        # Replace the last path segment with ApplyConfirmation.
        path_parts = parsed.path.rstrip("/").rsplit("/", 1)
        if len(path_parts) == 2:
            new_path = path_parts[0] + "/ApplyConfirmation"
        else:
            new_path = "/ApplyConfirmation"

        # Rebuild query string, preserving source if present.
        new_qs: dict[str, str] = {"folderId": folder_id}
        for key in ("source", "Source"):
            if key in params:
                new_qs["source"] = params[key][0]
                break

        return urlunparse((
            parsed.scheme,
            parsed.netloc,
            new_path,
            "",
            urlencode(new_qs),
            "",
        ))
    except Exception as exc:
        _log.debug(f"_derive_apply_confirmation_url error | url={original_url} error={exc}")
        return None


# Selectors for the "Apply Now" button on the ApplyConfirmation page.
_APPLY_NOW_SELECTORS = [
    "button:has-text('Apply Now')",
    "a:has-text('Apply Now')",
    "[role='button']:has-text('Apply Now')",
    "button:has-text('Apply')",
    "a:has-text('Apply')",
    "input[type='submit'][value*='Apply']",
    "[role='button']:has-text('Apply')",
]


async def _find_apply_now_button(page: Any) -> Any | None:
    """Find the Apply Now button on an ApplyConfirmation page."""
    el = await _find_first(page, _APPLY_NOW_SELECTORS)
    if el:
        text = (await el.inner_text()).strip()[:60]
        _log.debug(f"Apply Now button found via selector | text={repr(text)}")
        return el

    # JS fallback
    try:
        handle = await page.evaluate_handle("""
            () => {
                const WORDS = ['apply now', 'apply'];
                const candidates = document.querySelectorAll(
                    'button, a[href], [role="button"], input[type="submit"]'
                );
                for (const el of candidates) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') continue;
                    const text = (el.innerText || el.value || '').toLowerCase().trim();
                    if (WORDS.some(w => text === w || text.startsWith(w))) return el;
                }
                return null;
            }
        """)
        el = handle.as_element()
        if el:
            text = (await el.inner_text()).strip()[:60]
            _log.debug(f"Apply Now button found via JS | text={repr(text)}")
            return el
    except Exception as exc:
        _log.debug(f"JS Apply Now search error | error={exc}")

    return None


# ---------------------------------------------------------------------------
# Login helpers
# ---------------------------------------------------------------------------

# Password field selectors — checked first to see if we're already on the form.
_PASSWORD_INPUT_SELECTORS = [
    "input[type='password']",
    "input[name*='password' i]",
    "input[id*='password' i]",
    "input[placeholder*='password' i]",
    "input[autocomplete='current-password']",
]

# Email field selectors — note: Avature sometimes uses type='text', not 'email'.
_EMAIL_INPUT_SELECTORS = [
    "input[type='email']",
    "input[name*='email' i]",
    "input[id*='email' i]",
    "input[placeholder*='email' i]",
    "input[autocomplete='email']",
    "input[autocomplete='username']",
    "input[type='text'][name*='user' i]",
    "input[type='text'][id*='user' i]",
]

# Login submit selectors.
_LOGIN_SUBMIT_SELECTORS = [
    "button[type='submit']",
    "input[type='submit']",
    "button:has-text('Sign In')",
    "button:has-text('Sign in')",
    "button:has-text('Log In')",
    "button:has-text('Log in')",
    "button:has-text('Login')",
    "[role='button']:has-text('Sign In')",
    "[role='button']:has-text('Log In')",
]


async def _find_sign_in_trigger(page: Any) -> Any | None:
    """
    Find the element that leads to the email/password form on a method-choice page.
    Uses both CSS selectors and a broad JS fallback so it works across Avature portals
    that vary in button text and markup.
    """
    # CSS selectors — ordered from most to least specific
    css_candidates = [
        "a:has-text('Sign In')",
        "button:has-text('Sign In')",
        "a:has-text('Sign in')",
        "button:has-text('Sign in')",
        "[role='button']:has-text('Sign In')",
        "[role='button']:has-text('Sign in')",
        "a:has-text('Log In')",
        "a:has-text('Log in')",
        "button:has-text('Log In')",
        "button:has-text('Log in')",
        "a:has-text('Already have an account')",
        "button:has-text('Already have an account')",
        "a:has-text('Existing user')",
        "a:has-text('Sign in here')",
        "a:has-text('Login')",
        "button:has-text('Login')",
        "a:has-text('Email')",            # some portals show "Sign in with Email"
    ]
    el = await _find_first(page, css_candidates)
    if el:
        text = (await el.inner_text()).strip()[:60]
        _log.debug(f"sign-in trigger found via CSS | text={repr(text)}")
        return el

    # JS fallback — walks all visible clickable elements and matches on keywords
    try:
        handle = await page.evaluate_handle("""
            () => {
                const WORDS = ['sign in', 'log in', 'login', 'signin',
                               'already have', 'existing user', 'sign-in'];
                const candidates = document.querySelectorAll(
                    'a, button, [role="button"], [onclick], [tabindex="0"]'
                );
                for (const el of candidates) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') continue;
                    const text = (el.innerText || el.textContent || '').toLowerCase().trim();
                    if (WORDS.some(w => text.includes(w))) return el;
                }
                return null;
            }
        """)
        el = handle.as_element()
        if el:
            text = (await el.inner_text()).strip()[:60]
            _log.debug(f"sign-in trigger found via JS | text={repr(text)}")
            return el
    except Exception as exc:
        _log.debug(f"JS sign-in trigger search error | error={exc}")

    return None


async def _wait_for_field(page: Any, selectors: list[str], timeout_s: float = 4.0) -> Any | None:
    """Poll for a field to appear (for pages that animate the login form in)."""
    import time
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        el = await _find_first(page, selectors)
        if el:
            return el
        await asyncio.sleep(0.4)
    return None


async def _fill_input(el: Any, value: str) -> None:
    """
    Fill an ElementHandle input field safely.

    ElementHandle does not support triple_click().  Use fill() as the primary
    method (clears the field and sets the value atomically), with a
    click → select-all → type fallback for inputs that reject fill().
    """
    try:
        await el.fill(value)
        return
    except Exception:
        pass
    # Fallback: focus, select-all, type
    try:
        await el.click()
        await el.press("Control+a")
        await el.press("Backspace")
        await el.type(value, delay=40)
    except Exception as exc:
        _log.warning(f"_fill_input fallback also failed | error={exc}")


async def _handle_login(session: ApplySession, email: str, password: str) -> bool:
    """
    Attempt to log into an Avature portal.

    Flow:
      1. Check if a password field is already visible (already on the login form).
         If not, look for a "Sign In" / "Log in" trigger element and click it,
         then wait up to 4 s for the credential form to appear.
      2. Fill the email field.
      3. Fill the password field (never logged).
      4. Click the submit button.
      5. Return True if the page URL changed or no validation errors appeared.

    Returns True if login appeared to succeed, False otherwise.
    All failures emit a WARNING log explaining which element was missing so the
    caller can diagnose the DOM via the saved screenshot/HTML.
    """
    page = session.page
    _log.info(f"attempting avature login | email={email}")

    # -- Step 1: get to the credential form --
    password_el = await _find_first(page, _PASSWORD_INPUT_SELECTORS)
    if password_el is None:
        # Method-choice page — need to click something to reveal email+password
        trigger = await _find_sign_in_trigger(page)
        if trigger is None:
            # Log everything visible so the developer can add the right selector
            candidates = await _scan_clickable_elements(page)
            _log.warning(
                f"login: no 'Sign In' trigger and no password field found | "
                f"visible_clickables={len(candidates)}"
            )
            for c in candidates:
                _log.warning(
                    f"  clickable | tag={c['tag']} text={repr(c['text'][:60])} "
                    f"role={repr(c['role'])} href={repr(c['href'][:40])}"
                )
            await session.screenshot_failure("avature-login-no-trigger")
            await session.save_html("avature-login-no-trigger")
            return False

        _log.debug("clicking sign-in trigger")
        await trigger.click()
        await session.screenshot("avature-login-after-trigger")

        # Wait for the form to animate / navigate in
        password_el = await _wait_for_field(page, _PASSWORD_INPUT_SELECTORS, timeout_s=4.0)
        if password_el is None:
            # Maybe it's email-first (email → submit → then password page)
            _log.debug("no password field after trigger click — trying email-first flow")

    # -- Step 2: fill email --
    email_el = await _wait_for_field(page, _EMAIL_INPUT_SELECTORS, timeout_s=3.0)
    if email_el is None:
        _log.warning("login: email input not found after trigger click")
        await session.screenshot_failure("avature-login-no-email-field")
        await session.save_html("avature-login-no-email-field")
        return False

    _log.debug("filling email field")
    await _fill_input(email_el, email)

    # -- Step 3: fill password (if visible now; may come after email submit) --
    password_el = await _find_first(page, _PASSWORD_INPUT_SELECTORS)
    if password_el is None:
        # Email-first flow: submit email, then password appears on next page/step
        _log.debug("password not yet visible — submitting email to advance to password page")
        submit_el = await _find_first(page, _LOGIN_SUBMIT_SELECTORS)
        if submit_el:
            await submit_el.click()
            await asyncio.sleep(1.5)
            await session.screenshot("avature-login-after-email-submit")

        password_el = await _wait_for_field(page, _PASSWORD_INPUT_SELECTORS, timeout_s=4.0)
        if password_el is None:
            _log.warning("login: password input not found (tried both single-page and email-first flows)")
            await session.screenshot_failure("avature-login-no-password-field")
            await session.save_html("avature-login-no-password-field")
            return False

    _log.debug("filling password field")
    await _fill_input(password_el, password)
    await session.screenshot("avature-login-credentials-entered")

    # -- Step 4: submit --
    url_before = await session.current_url()
    submit_el = await _find_first(page, _LOGIN_SUBMIT_SELECTORS)
    if submit_el is None:
        _log.warning("login: submit button not found after filling credentials")
        await session.screenshot_failure("avature-login-no-submit")
        await session.save_html("avature-login-no-submit")
        return False

    _log.info("submitting login form")
    await submit_el.click()
    await asyncio.sleep(2.5)
    await session.screenshot("avature-post-login")

    # Step 5 — verify the page changed (URL or visible content shifted)
    url_after = await session.current_url()
    if url_after != url_before:
        _log.info(f"login succeeded | url_after={url_after[:100]}")
        return True

    # URL didn't change — check for error messages
    errors = await get_validation_errors(page)
    if errors:
        _log.warning(f"login form errors | errors={errors[:3]}")
        await session.screenshot_failure("avature-login-error")
        return False

    # URL same but no error — might be an SPA that updates in-place; treat as success
    _log.info("login submitted — URL unchanged but no errors detected, treating as success")
    return True


async def _find_first(page: Any, selectors: list[str]) -> Any | None:
    """Return the first visible, enabled element matching any selector in the list."""
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible() and await el.is_enabled():
                return el
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Navigation helpers
# ---------------------------------------------------------------------------

async def _find_nav_button(page: Any) -> Any | None:
    """
    Find a navigation button on an Avature page.

    Strategy (in order):
    1. Selector-based: try every combination of (_NAV_TEXTS × _NAV_SELECTOR_TEMPLATES),
       filtering to visible + enabled elements only.
    2. JS evaluate_handle fallback: walk all visible clickable elements in DOM order
       and return the first whose text contains a known nav word.

    Returns a Playwright ElementHandle or None.
    """
    for text in _NAV_TEXTS:
        for template in _NAV_SELECTOR_TEMPLATES:
            selector = template.format(text=text)
            try:
                el = await page.query_selector(selector)
                if el and await el.is_visible() and await el.is_enabled():
                    tag = await el.evaluate("el => el.tagName.toLowerCase()")
                    visible_text = (await el.inner_text()).strip()[:60]
                    _log.debug(
                        f"nav button found via selector | "
                        f"selector={selector!r} tag={tag} text={repr(visible_text)}"
                    )
                    return el
            except Exception:
                continue

    # JS fallback — handles elements that Playwright selectors may miss
    # (e.g. custom web-components, shadow-DOM-lite wrappers, onclick divs)
    try:
        handle = await page.evaluate_handle("""
            () => {
                const NAV_WORDS = [
                    'continue', 'next', 'apply', 'register',
                    'submit', 'upload', 'proceed', 'save', 'confirm',
                ];
                const candidates = document.querySelectorAll(
                    'button, a[href], [role="button"], ' +
                    'input[type="submit"], input[type="button"], ' +
                    '[onclick], [tabindex="0"]'
                );
                for (const el of candidates) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') continue;
                    const text = (
                        el.innerText || el.value ||
                        el.getAttribute('aria-label') || ''
                    ).toLowerCase().trim();
                    if (NAV_WORDS.some(w => text.includes(w))) return el;
                }
                return null;
            }
        """)
        element = handle.as_element()
        if element:
            visible_text = (await element.inner_text()).strip()[:60]
            _log.debug(f"nav button found via JS fallback | text={repr(visible_text)}")
            return element
    except Exception as exc:
        _log.debug(f"JS nav fallback error | error={exc}")

    return None


async def _scan_clickable_elements(page: Any) -> list[dict]:
    """
    Return metadata for every visible, potentially-clickable element on the page.
    Called only when _find_nav_button() returns None, for diagnostic logging.
    """
    try:
        return await page.evaluate("""
            () => {
                const results = [];
                const candidates = document.querySelectorAll(
                    'button, input[type="submit"], input[type="button"], ' +
                    'a[href], [role="button"], [onclick], [tabindex]'
                );
                for (const el of candidates) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') continue;
                    results.push({
                        tag:       el.tagName.toLowerCase(),
                        text:      (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().substring(0, 80),
                        role:      el.getAttribute('role') || '',
                        type:      el.getAttribute('type') || '',
                        id:        el.id || '',
                        className: (el.className || '').toString().substring(0, 60),
                        onclick:   el.hasAttribute('onclick'),
                        tabindex:  el.getAttribute('tabindex') || '',
                        href:      (el.getAttribute('href') || '').substring(0, 60),
                    });
                }
                return results.slice(0, 40);
            }
        """)
    except Exception as exc:
        _log.debug(f"_scan_clickable_elements error | error={exc}")
        return []


def _classify_avature_step(url: str, page_text: str) -> str:
    """
    Classify the current Avature page step.

    Returns one of: 'register' | 'resume' | 'profile' | 'screening' | 'review' | 'unknown'.
    Checks URL and visible page text together so URL-encoded step names are caught.
    """
    combined = (url + " " + page_text).lower()
    for step_name, patterns in _AVATURE_STEP_PATTERNS:
        for pattern in patterns:
            if pattern in combined:
                return step_name
    return "unknown"


async def _get_page_text(page: Any, max_chars: int = 3000) -> str:
    try:
        text = await page.evaluate("() => document.body.innerText")
        return (text or "")[:max_chars]
    except Exception:
        return ""
