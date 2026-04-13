"""
Workday adapter.

Workday is one of the most complex and fragile ATS platforms.
It uses heavily custom React widgets that do NOT map to standard HTML.

Known Workday challenges:
  - React-driven DOM: no native <select>, no <input type="radio"> in many places
  - Custom combobox/listbox widgets with dynamic option loading
  - Multi-step wizard with non-standard navigation
  - File upload uses a custom drag-and-drop widget
  - Session timeouts / CAPTCHAs common

Phase 2 target: implement basic Workday support.
For now, this adapter detects Workday URLs and returns a structured "unsupported" result
so the runner can surface this clearly to the user.
"""
from __future__ import annotations

import re
from typing import Any

from ..answer_engine import AnswerEngine
from ..observability import get_logger
from ..session import ApplySession
from .base import AdapterResult, SiteAdapter

_log = get_logger("workday")

_WORKDAY_URL_PATTERNS = [
    r"myworkdayjobs\.com",
    r"workday\.com",
    r"wd\d+\.myworkdayjobs\.com",
    r"wd1\.myworkdaysite\.com",
]


class WorkdayAdapter(SiteAdapter):
    name = "workday"
    priority = 8

    @classmethod
    def detect(cls, url: str, page_title: str = "", page_content: str = "") -> bool:
        url_lower = url.lower()
        return any(re.search(p, url_lower) for p in _WORKDAY_URL_PATTERNS)

    async def run(
        self,
        session: ApplySession,
        answer_engine: AnswerEngine,
        job_metadata: dict[str, Any] | None = None,
    ) -> AdapterResult:
        _log.info(f"workday adapter — basic support only | url={await session.current_url()}")
        await session.screenshot("01-workday-detected")

        # TODO (Phase 2): Implement full Workday navigation
        # Workday requires:
        # 1. Handle SSO / login flow
        # 2. Navigate multi-step wizard (typically 4-8 steps)
        # 3. Handle custom combobox widgets for dropdowns
        # 4. Handle custom file upload widget
        # 5. Detect review/summary page

        return AdapterResult(
            status="blocked",
            adapter_name=self.name,
            site_name="Workday",
            failure_reason=(
                "Workday full automation is Phase 2. "
                "Navigate manually and use the profile config for answers."
            ),
            notes=[
                "Workday detected. Full adapter coming in Phase 2.",
                "URL: " + await session.current_url(),
            ],
            screenshots=session.all_screenshots,
        )
