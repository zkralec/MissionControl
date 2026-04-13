"""
Base adapter interface.

Every site adapter (LinkedIn, Greenhouse, Workday, etc.) implements SiteAdapter.
The runner detects which adapter to use, then delegates entirely to it.

Adapters are responsible for:
  - Detecting if they own the current page
  - Navigating to the application form
  - Iterating through form steps
  - Stopping at the review page
  - Returning a structured AdapterResult
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..answer_engine import AnswerEngine
    from ..session import ApplySession
    from ..forms.handlers import FieldFillResult


@dataclass
class AdapterResult:
    status: str = "partial"              # draft_ready | partial | blocked | auth_required | failed | unknown
    review_reached: bool = False
    submitted: bool = False              # always False in draft mode
    step_count: int = 0
    fields_filled: list["FieldFillResult"] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    failure_reason: str | None = None
    site_name: str = ""
    adapter_name: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def filled_count(self) -> int:
        return sum(1 for f in self.fields_filled if f.success)

    @property
    def failed_count(self) -> int:
        return sum(1 for f in self.fields_filled if not f.success and f.status != "skipped")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "review_reached": self.review_reached,
            "submitted": self.submitted,
            "step_count": self.step_count,
            "adapter_name": self.adapter_name,
            "site_name": self.site_name,
            "fields_filled_count": self.filled_count,
            "fields_failed_count": self.failed_count,
            "screenshots": self.screenshots,
            "failure_reason": self.failure_reason,
            "notes": self.notes,
            "fields_manifest": [
                {
                    "label": f.field_label,
                    "type": f.field_type,
                    "status": f.status,
                    "value_preview": f.value_preview,
                }
                for f in self.fields_filled
            ],
        }


class SiteAdapter(ABC):
    """
    Abstract base class for all site-specific adapters.

    Subclasses must implement:
      - name: str (class attribute — short identifier e.g. "linkedin")
      - detect(): return True if this adapter owns the given URL/page
      - run(): navigate and fill the application, return AdapterResult
    """

    name: str = "base"
    priority: int = 0   # Higher priority = checked first during auto-detection

    @classmethod
    @abstractmethod
    def detect(cls, url: str, page_title: str = "", page_content: str = "") -> bool:
        """
        Return True if this adapter should handle the given URL/page.
        Called before the page is loaded (URL-only check) and after (title/content).
        Must be a fast, pure function — no browser calls.
        """
        ...

    @abstractmethod
    async def run(
        self,
        session: "ApplySession",
        answer_engine: "AnswerEngine",
        job_metadata: dict[str, Any] | None = None,
    ) -> AdapterResult:
        """
        Navigate the application form for this site.

        Contract:
        - MUST stop before the submit button (draft mode)
        - MUST capture screenshots at key transitions
        - MUST return AdapterResult regardless of success/failure
        - MUST NOT submit the application
        - SHOULD capture failure screenshots on exceptions
        """
        ...

    async def _handle_unexpected_error(
        self,
        session: "ApplySession",
        exc: Exception,
        context: str = "",
    ) -> AdapterResult:
        """Common error handler — capture screenshot and return failed result."""
        label = f"unexpected-error-{context}" if context else "unexpected-error"
        await session.screenshot_failure(label)
        return AdapterResult(
            status="failed",
            adapter_name=self.name,
            failure_reason=f"{type(exc).__name__}: {exc}",
        )
