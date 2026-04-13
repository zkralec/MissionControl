"""
Apply Engine — main runner.

Entry point for a single job application run.

Usage:
    from integrations.apply_engine.runner import run_apply, ApplyConfig

    config = ApplyConfig(
        job_url="https://www.linkedin.com/jobs/view/12345",
        profile_path="/home/user/.config/mission-control/profile.yaml",
        output_dir=Path("/data/apply_engine_runs"),
        headless=True,
    )
    result = asyncio.run(run_apply(config))

The runner:
  1. Loads the applicant profile
  2. Launches a Playwright browser session
  3. Navigates to the job URL
  4. Auto-detects which site adapter to use
  5. Delegates to the adapter
  6. Persists the run summary
  7. Returns a structured ApplyResult
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .adapters.base import AdapterResult, SiteAdapter
from .adapters.avature import AvatureAdapter
from .adapters.generic import GenericAdapter
from .adapters.greenhouse import GreenhouseAdapter
from .adapters.lever import LeverAdapter
from .adapters.linkedin import LinkedInAdapter
from .adapters.workable import WorkableAdapter
from .adapters.workday import WorkdayAdapter
from .answer_engine import AnswerEngine
from .observability import RunSummary, get_logger
from .profile import ApplicantProfile
from .session import ApplySession, SessionConfig

_log = get_logger("runner")

# Adapter registry — ordered by priority (highest first).
# Generic is always last (priority=0).
ADAPTERS: list[type[SiteAdapter]] = [
    LinkedInAdapter,
    GreenhouseAdapter,
    LeverAdapter,
    WorkdayAdapter,
    WorkableAdapter,
    AvatureAdapter,
    GenericAdapter,
]


@dataclass
class ApplyConfig:
    job_url: str
    profile_path: str | Path
    output_dir: Path = field(default_factory=lambda: Path("/data/apply_engine_runs"))
    headless: bool = True
    storage_state_path: str | None = None     # saved LinkedIn/site auth
    browser_profile_dir: str | None = None    # persistent Chrome profile
    enable_llm: bool = True                   # allow OpenAI fallback for long-form answers
    llm_budget: int = 10                      # max OpenAI fallback calls per run
    llm_model: str = "gpt-5-mini"             # OpenAI model for long-form answers
    llm_max_tokens: int = 1500                # max completion tokens for long-form answers (gpt-5* models use reasoning tokens from this budget)
    run_id: str | None = None                 # auto-generated if None
    slow_mo_ms: int = 0                       # non-zero for debugging
    screenshots_subdir: str = "screenshots"
    # Avature portal credentials (read from AVATURE_EMAIL / AVATURE_PASSWORD env vars)
    avature_email: str | None = None
    avature_password: str | None = None
    # Submit the application after reaching the review page.
    # When True, the engine pauses and prompts for confirmation before submitting.
    auto_submit: bool = False


@dataclass
class ApplyResult:
    run_id: str
    job_url: str
    status: str                          # submitted | draft_ready | partial | blocked | auth_required | failed
    review_reached: bool
    adapter_name: str
    site_name: str
    fields_filled_count: int
    fields_failed_count: int
    step_count: int
    screenshots: list[str]
    summary_path: str | None
    failure_reason: str | None
    llm_calls_used: int
    notes: list[str]
    fields_manifest: list[dict[str, Any]]
    submitted: bool = False

    @property
    def success(self) -> bool:
        return self.status in {"draft_ready", "submitted"} and self.review_reached

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "job_url": self.job_url,
            "status": self.status,
            "review_reached": self.review_reached,
            "adapter_name": self.adapter_name,
            "site_name": self.site_name,
            "fields_filled_count": self.fields_filled_count,
            "fields_failed_count": self.fields_failed_count,
            "step_count": self.step_count,
            "screenshots": self.screenshots,
            "summary_path": self.summary_path,
            "failure_reason": self.failure_reason,
            "llm_calls_used": self.llm_calls_used,
            "notes": self.notes,
            "fields_manifest": self.fields_manifest,
            "submitted": self.submitted,
        }


async def run_apply(config: ApplyConfig) -> ApplyResult:
    """
    Run a single job application draft.

    This is the main entry point. It is fully async and safe to run
    in parallel for multiple jobs (each gets its own browser session).
    """
    run_id = config.run_id or f"run-{uuid.uuid4().hex[:12]}"
    started_at = datetime.now(timezone.utc).isoformat()

    # Fill in credentials from env vars if not already set on config
    import os as _os
    if not config.avature_email:
        config.avature_email = _os.environ.get("AVATURE_EMAIL")
    if not config.avature_password:
        config.avature_password = _os.environ.get("AVATURE_PASSWORD")

    _log.info(
        f"session created | run_id={run_id} headless={config.headless} "
        f"avature_email={config.avature_email or '(none)'} "
        f"avature_password_set={bool(config.avature_password)}"
    )

    # Load applicant profile
    try:
        profile = ApplicantProfile.load(config.profile_path)
    except FileNotFoundError as exc:
        _log.error(f"profile not found | path={config.profile_path}")
        return _error_result(run_id, config.job_url, str(exc), "failed")
    except Exception as exc:
        _log.error(f"profile load error | error={exc}")
        return _error_result(run_id, config.job_url, str(exc), "failed")

    # Build session config
    session_config = SessionConfig(
        headless=config.headless,
        storage_state_path=config.storage_state_path,
        browser_profile_dir=config.browser_profile_dir,
        slow_mo_ms=config.slow_mo_ms,
        screenshots_dir=config.output_dir / config.screenshots_subdir,
    )

    # Build answer engine (LLM client is optional)
    llm_client = None
    if config.enable_llm:
        llm_client = _try_load_llm_client()

    answer_engine = AnswerEngine(
        profile=profile,
        llm_client=llm_client,
        llm_model=config.llm_model,
        enable_llm=config.enable_llm and llm_client is not None,
        llm_call_budget=config.llm_budget,
        llm_max_tokens=config.llm_max_tokens,
    )

    # Run with Playwright
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return _error_result(
            run_id, config.job_url,
            "playwright not installed. Run: pip install playwright && playwright install chromium",
            "failed",
        )

    async with async_playwright() as pw:
        session = await ApplySession.create(run_id, session_config, pw)
        async with session:
            # Navigate to the job URL
            nav_ok = await session.navigate(config.job_url)
            if not nav_ok:
                return _error_result(run_id, config.job_url, "Failed to navigate to job URL.", "failed")

            await session.wait_stable(1500)

            # Detect which adapter to use
            url = await session.current_url()
            page_title = await session.page_title()
            adapter = _select_adapter(url, page_title)
            _log.info(f"adapter selected | adapter={adapter.name} url={url}")

            # Build per-adapter metadata (credentials, portal hints, etc.)
            job_metadata: dict[str, Any] = {}
            if config.avature_email:
                job_metadata["avature_email"] = config.avature_email
            if config.avature_password:
                job_metadata["avature_password"] = config.avature_password

            # Run the adapter
            adapter_result: AdapterResult = await adapter.run(
                session, answer_engine, job_metadata=job_metadata or None
            )

            # Auto-submit: if the adapter reached the review page and the user
            # opted in, prompt for confirmation then submit.
            if config.auto_submit and adapter_result.status == "draft_ready":
                adapter_result = await _run_auto_submit(session, adapter_result)

            # Build and save run summary
            summary = _build_summary(
                run_id=run_id,
                job_url=config.job_url,
                started_at=started_at,
                adapter=adapter,
                adapter_result=adapter_result,
                llm_calls=answer_engine.llm_calls_used,
            )

            config.output_dir.mkdir(parents=True, exist_ok=True)

            # Build fields manifest (includes required flag) for both summary + log.
            fields_manifest = [
                {
                    "label": f.field_label,
                    "type": f.field_type,
                    "required": f.required,
                    "status": f.status,
                    "value_preview": f.value_preview,
                    "error": f.error,
                }
                for f in adapter_result.fields_filled
            ]
            summary.fields_manifest = fields_manifest

            summary_path = summary.save(config.output_dir)
            fields_log_path = _write_fields_log(
                run_id=run_id,
                job_url=config.job_url,
                adapter_name=adapter_result.adapter_name,
                status=adapter_result.status,
                fields=adapter_result.fields_filled,
                output_dir=config.output_dir,
            )
            _log.info(
                f"apply run complete | run_id={run_id} status={adapter_result.status} "
                f"review_reached={adapter_result.review_reached} "
                f"fields_filled={adapter_result.filled_count} summary={summary_path} "
                f"fields_log={fields_log_path}"
            )

            return ApplyResult(
                run_id=run_id,
                job_url=config.job_url,
                status=adapter_result.status,
                review_reached=adapter_result.review_reached,
                adapter_name=adapter_result.adapter_name,
                site_name=adapter_result.site_name,
                fields_filled_count=adapter_result.filled_count,
                fields_failed_count=adapter_result.failed_count,
                step_count=adapter_result.step_count,
                screenshots=adapter_result.screenshots,
                summary_path=str(summary_path),
                failure_reason=adapter_result.failure_reason,
                llm_calls_used=answer_engine.llm_calls_used,
                notes=adapter_result.notes,
                submitted=adapter_result.submitted,
                fields_manifest=fields_manifest,
            )


# ---------------------------------------------------------------------------
# Auto-submit
# ---------------------------------------------------------------------------

# Broad submit-button selectors that work across ATS platforms.
# Ordered: most specific first so we don't accidentally click a secondary button.
_SUBMIT_SELECTORS_BROAD = [
    "button[aria-label='Submit application']",
    "button[aria-label='Submit Application']",
    "button:has-text('Submit Application')",
    "button:has-text('Submit application')",
    "button:has-text('Send Application')",
    "button:has-text('Send application')",
    "button:has-text('Submit')",
    "button:has-text('Send')",
    "button[type='submit']",
    "input[type='submit']",
    "[role='button']:has-text('Submit Application')",
    "[role='button']:has-text('Submit')",
    "input[type='button'][value*='Submit' i]",
    "a:has-text('Submit Application')",
    "a:has-text('Submit')",
]


async def _find_submit_button_broad(page: Any) -> Any | None:
    """Find the submit button using a wide selector set + JS fallback."""
    for sel in _SUBMIT_SELECTORS_BROAD:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible() and await el.is_enabled():
                return el
        except Exception:
            continue

    # JS fallback — catches non-standard submit controls
    try:
        handle = await page.evaluate_handle("""
            () => {
                const WORDS = ['submit application', 'send application', 'submit', 'send'];
                const candidates = document.querySelectorAll(
                    'button, input[type="submit"], input[type="button"], ' +
                    '[role="button"], a[href]'
                );
                for (const el of candidates) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') continue;
                    const text = (el.innerText || el.value || '').toLowerCase().trim();
                    if (WORDS.some(w => text === w || text.startsWith(w + ' '))) return el;
                }
                return null;
            }
        """)
        el = handle.as_element()
        if el:
            return el
    except Exception:
        pass

    return None


async def _run_auto_submit(
    session: "ApplySession",
    adapter_result: "AdapterResult",
) -> "AdapterResult":
    """
    Prompt the user to confirm submission, then click the submit button.

    This runs while the browser session is still open on the review page.
    The prompt blocks until the user responds — the browser stays idle.

    Returns the adapter_result unchanged if the user cancels.
    Returns a modified adapter_result with status='submitted' if submitted.
    """
    filled = adapter_result.filled_count
    failed = adapter_result.failed_count

    print()
    print("=" * 62)
    print("  APPLICATION READY TO SUBMIT")
    print("=" * 62)
    print(f"  Site          : {adapter_result.site_name}")
    print(f"  Fields filled : {filled}")
    if failed:
        print(f"  Fields failed : {failed}  ← review before submitting")
    if adapter_result.notes:
        for note in adapter_result.notes:
            print(f"  Note          : {note}")
    print()

    try:
        answer = input(
            "  Type 'yes' or 'submit' to confirm, anything else to cancel: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  Submit cancelled.")
        return adapter_result

    if answer not in ("yes", "y", "submit"):
        print("  Cancelled — application left in draft_ready state.")
        return adapter_result

    await session.screenshot("auto-submit-pre-click")

    submit_btn = await _find_submit_button_broad(session.page)
    if submit_btn is None:
        print("  ERROR: Submit button not found on the review page.")
        _log.error("auto_submit: submit button not found on review page")
        adapter_result.failure_reason = (
            "auto_submit: submit button not found on review page"
        )
        return adapter_result

    _log.info("auto_submit: clicking submit button")
    await submit_btn.click()
    await asyncio.sleep(3.0)
    await session.screenshot("auto-submit-post-click")

    adapter_result.submitted = True
    adapter_result.status = "submitted"
    adapter_result.notes.append("Application submitted (user confirmed via auto-submit prompt).")
    adapter_result.screenshots = session.all_screenshots

    print("  Application submitted.")
    _log.info("auto_submit: application submitted")
    return adapter_result


# ---------------------------------------------------------------------------
# Adapter selection
# ---------------------------------------------------------------------------

def _select_adapter(url: str, page_title: str = "", page_content: str = "") -> SiteAdapter:
    """Select the best adapter for the given URL."""
    # Sort by priority descending, check each in order
    sorted_adapters = sorted(ADAPTERS, key=lambda cls: cls.priority, reverse=True)
    checked: list[str] = []
    for adapter_cls in sorted_adapters:
        matched = adapter_cls.detect(url, page_title, page_content)
        _log.debug(
            f"adapter detection | adapter={adapter_cls.name} priority={adapter_cls.priority} "
            f"matched={matched} url={url}"
        )
        checked.append(f"{adapter_cls.name}({'✓' if matched else '✗'})")
        if matched:
            _log.info(
                f"adapter selected | adapter={adapter_cls.name} priority={adapter_cls.priority} "
                f"checked=[{', '.join(checked)}]"
            )
            return adapter_cls()
    # GenericAdapter always matches, so this should never happen
    _log.warning(f"no adapter matched — falling back to generic | url={url}")
    return GenericAdapter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _try_load_llm_client() -> Any | None:
    """Try to instantiate an OpenAI client. Returns None if not available."""
    try:
        import os
        from openai import OpenAI

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            _log.debug("OPENAI_API_KEY not set — long-form fallback disabled")
            return None
        return OpenAI(api_key=api_key)
    except ImportError:
        _log.debug("openai package not installed — long-form fallback disabled")
        return None


def _error_result(run_id: str, job_url: str, failure_reason: str, status: str) -> ApplyResult:
    return ApplyResult(
        run_id=run_id,
        job_url=job_url,
        status=status,
        review_reached=False,
        adapter_name="none",
        site_name="none",
        fields_filled_count=0,
        fields_failed_count=0,
        step_count=0,
        screenshots=[],
        summary_path=None,
        failure_reason=failure_reason,
        llm_calls_used=0,
        notes=[],
        fields_manifest=[],
    )


def _build_summary(
    run_id: str,
    job_url: str,
    started_at: str,
    adapter: SiteAdapter,
    adapter_result: AdapterResult,
    llm_calls: int,
) -> RunSummary:
    summary = RunSummary(
        run_id=run_id,
        job_url=job_url,
        site=adapter_result.site_name,
        adapter_used=adapter.name,
        started_at=started_at,
        llm_calls=llm_calls,
        screenshots=list(adapter_result.screenshots),
        notes=list(adapter_result.notes),
    )
    summary.finish(
        status=adapter_result.status,
        failure_reason=adapter_result.failure_reason,
    )
    summary.review_reached = adapter_result.review_reached
    summary.submitted = adapter_result.submitted
    return summary


def _write_fields_log(
    run_id: str,
    job_url: str,
    adapter_name: str,
    status: str,
    fields: "list[Any]",
    output_dir: Path,
) -> Path:
    """
    Write a human-readable fields report to {output_dir}/{run_id}-fields.log.

    Columns: #  REQ  TYPE        STATUS    LABEL                         VALUE
    Groups entries by status so filled fields come first, then skipped, then failed.
    """
    from .forms.handlers import FieldFillResult  # avoid circular at module level

    path = output_dir / f"{run_id}-fields.log"

    # Counters
    filled = [f for f in fields if f.status == "filled"]
    skipped = [f for f in fields if f.status == "skipped"]
    failed = [f for f in fields if f.status not in ("filled", "skipped")]

    lines: list[str] = []
    sep = "=" * 78

    lines += [
        sep,
        f"  FIELDS REPORT — {run_id}",
        sep,
        f"  URL     : {job_url}",
        f"  Adapter : {adapter_name}",
        f"  Status  : {status}",
        f"  Total   : {len(fields)}  filled={len(filled)}  skipped={len(skipped)}  failed={len(failed)}",
        sep,
        "",
    ]

    # Column header
    lines.append(f"  {'#':>3}  {'REQ':<3}  {'TYPE':<10}  {'STATUS':<8}  {'LABEL':<35}  VALUE")
    lines.append(f"  {'-'*3}  {'-'*3}  {'-'*10}  {'-'*8}  {'-'*35}  {'-'*30}")

    def _row(i: int, f: "FieldFillResult") -> str:
        req = "YES" if f.required else "no"
        label = (f.field_label or "(unlabeled)")[:35]
        value = (f.value_preview or "")[:50]
        err = f"  ERROR: {f.error}" if f.error else ""
        return f"  {i:>3}  {req:<3}  {f.field_type:<10}  {f.status:<8}  {label:<35}  {value}{err}"

    i = 1
    for section, group in [("FILLED", filled), ("SKIPPED", skipped), ("FAILED / NO VALUE", failed)]:
        if not group:
            continue
        lines.append("")
        lines.append(f"  --- {section} ({len(group)}) ---")
        for f in group:
            lines.append(_row(i, f))
            i += 1

    lines += ["", sep, ""]

    path.write_text("\n".join(lines), encoding="utf-8")
    return path
