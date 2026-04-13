"""
Playwright browser session management.

Handles: browser launch, auth state, navigation, screenshot utilities.
One ApplySession per job application run.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .observability import ScreenshotManager, get_logger

_log = get_logger("session")

DEFAULT_TIMEOUT_MS = 15_000
DEFAULT_NAV_TIMEOUT_MS = 30_000


@dataclass
class SessionConfig:
    headless: bool = True
    storage_state_path: str | None = None    # saved auth (cookies + localStorage)
    browser_profile_dir: str | None = None  # persistent Chrome profile dir
    slow_mo_ms: int = 0                     # non-zero helps with some dynamic sites
    viewport_width: int = 1280
    viewport_height: int = 900
    user_agent: str | None = None
    screenshots_dir: Path = field(default_factory=lambda: Path("/tmp/apply_engine_screenshots"))
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    nav_timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS


class ApplySession:
    """
    Wraps a Playwright browser + context + page for a single application run.

    Usage (async context manager):
        async with ApplySession.create(run_id, config) as session:
            await session.navigate("https://...")
            screenshot_path = await session.screenshot("landing")
    """

    def __init__(
        self,
        run_id: str,
        config: SessionConfig,
        browser: Any,
        context: Any,
        page: Any,
        screenshots: ScreenshotManager,
    ) -> None:
        self.run_id = run_id
        self.config = config
        self.browser = browser
        self.context = context
        self.page = page
        self.screenshots = screenshots
        self._screenshot_paths: list[str] = []

    @classmethod
    async def create(
        cls,
        run_id: str,
        config: SessionConfig,
        playwright: Any,
    ) -> "ApplySession":
        """Launch a browser and create a session."""
        launch_kwargs: dict[str, Any] = {
            "headless": config.headless,
            "slow_mo": config.slow_mo_ms,
        }

        if config.browser_profile_dir:
            # Persistent context (keeps cookies, localStorage across runs)
            context = await playwright.chromium.launch_persistent_context(
                config.browser_profile_dir,
                **launch_kwargs,
                viewport={"width": config.viewport_width, "height": config.viewport_height},
                user_agent=config.user_agent,
            )
            browser = None
            page = context.pages[0] if context.pages else await context.new_page()
        else:
            browser = await playwright.chromium.launch(**launch_kwargs)
            context_kwargs: dict[str, Any] = {
                "viewport": {"width": config.viewport_width, "height": config.viewport_height},
            }
            if config.user_agent:
                context_kwargs["user_agent"] = config.user_agent
            if config.storage_state_path and Path(config.storage_state_path).exists():
                context_kwargs["storage_state"] = config.storage_state_path
                _log.info(f"loading saved auth state | path={config.storage_state_path}")
            context = await browser.new_context(**context_kwargs)
            page = await context.new_page()

        page.set_default_timeout(config.timeout_ms)
        page.set_default_navigation_timeout(config.nav_timeout_ms)

        screenshots = ScreenshotManager(config.screenshots_dir, run_id)
        _log.info(f"session created | run_id={run_id} headless={config.headless}")
        return cls(run_id, config, browser, context, page, screenshots)

    async def close(self) -> None:
        try:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
        except Exception as exc:
            _log.warning(f"error closing session | error={exc}")

    async def __aenter__(self) -> "ApplySession":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Navigation helpers
    # ------------------------------------------------------------------

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> bool:
        """Navigate to URL. Returns True on success."""
        try:
            await self.page.goto(url, wait_until=wait_until)
            _log.debug(f"navigated | url={url}")
            return True
        except Exception as exc:
            _log.warning(f"navigation failed | url={url} error={exc}")
            return False

    async def wait_for_selector(self, selector: str, timeout_ms: int | None = None) -> Any | None:
        """Wait for a selector, return the element or None."""
        try:
            return await self.page.wait_for_selector(
                selector,
                timeout=timeout_ms or self.config.timeout_ms,
            )
        except Exception:
            return None

    async def wait_stable(self, ms: int = 800) -> None:
        """Wait for network/animations to settle."""
        await asyncio.sleep(ms / 1000)

    async def save_auth_state(self, path: str | None = None) -> None:
        """Save current cookies + localStorage for future sessions."""
        target = path or self.config.storage_state_path
        if not target:
            return
        await self.context.storage_state(path=target)
        _log.info(f"auth state saved | path={target}")

    # ------------------------------------------------------------------
    # Screenshot helpers (delegates to ScreenshotManager, tracks paths)
    # ------------------------------------------------------------------

    async def screenshot(self, label: str) -> str | None:
        path = await self.screenshots.capture(self.page, label)
        if path:
            self._screenshot_paths.append(path)
        return path

    async def screenshot_failure(self, label: str = "failure") -> str | None:
        path = await self.screenshots.capture_failure(self.page, label)
        if path:
            self._screenshot_paths.append(path)
        return path

    async def save_html(self, label: str) -> str | None:
        return await self.screenshots.save_html(self.page, label)

    @property
    def all_screenshots(self) -> list[str]:
        return list(self._screenshot_paths)

    # ------------------------------------------------------------------
    # Page utilities
    # ------------------------------------------------------------------

    async def current_url(self) -> str:
        return self.page.url

    async def page_title(self) -> str:
        try:
            return await self.page.title()
        except Exception:
            return ""

    async def get_text(self, selector: str) -> str:
        """Get text content of a selector, empty string if not found."""
        try:
            el = await self.page.query_selector(selector)
            if el:
                return (await el.inner_text()).strip()
        except Exception:
            pass
        return ""

    async def click_if_present(self, selector: str) -> bool:
        """Click an element if it exists. Returns True if clicked."""
        try:
            el = await self.page.query_selector(selector)
            if el and await el.is_visible():
                await el.click()
                return True
        except Exception:
            pass
        return False

    async def try_selectors(self, selectors: list[str]) -> Any | None:
        """Try multiple selectors, return first visible element found."""
        for sel in selectors:
            try:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible():
                    return el
            except Exception:
                continue
        return None
