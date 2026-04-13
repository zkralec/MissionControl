"""
Structured logging and screenshot utilities for the apply engine.

Design: deterministic step log, screenshot-per-transition, run summary.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f"apply_engine.{name}")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
    return logger


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, val in record.__dict__.items():
            if key.startswith("_") or key in {
                "msg", "args", "levelname", "levelno", "name",
                "pathname", "filename", "module", "exc_info",
                "exc_text", "stack_info", "lineno", "funcName",
                "created", "msecs", "relativeCreated", "thread",
                "threadName", "processName", "process", "message",
            }:
                continue
            payload[key] = val
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


@dataclass
class StepRecord:
    index: int
    step_type: str
    started_at: str
    completed_at: str | None = None
    fields_filled: int = 0
    fields_failed: int = 0
    screenshot_path: str | None = None
    notes: list[str] = field(default_factory=list)

    def duration_ms(self) -> int | None:
        if not self.completed_at:
            return None
        start = datetime.fromisoformat(self.started_at)
        end = datetime.fromisoformat(self.completed_at)
        return int((end - start).total_seconds() * 1000)


@dataclass
class RunSummary:
    run_id: str
    job_url: str
    site: str
    adapter_used: str
    started_at: str
    completed_at: str | None = None
    status: str = "in_progress"  # draft_ready | partial | blocked | failed | in_progress
    review_reached: bool = False
    submitted: bool = False
    step_count: int = 0
    fields_filled: int = 0
    fields_failed: int = 0
    screenshots: list[str] = field(default_factory=list)
    steps: list[StepRecord] = field(default_factory=list)
    failure_reason: str | None = None
    llm_calls: int = 0
    notes: list[str] = field(default_factory=list)
    fields_manifest: list[dict] = field(default_factory=list)

    def finish(self, status: str, failure_reason: str | None = None) -> None:
        self.completed_at = datetime.now(timezone.utc).isoformat()
        self.status = status
        self.failure_reason = failure_reason
        self.step_count = len(self.steps)
        self.fields_filled = sum(s.fields_filled for s in self.steps)
        self.fields_failed = sum(s.fields_failed for s in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "job_url": self.job_url,
            "site": self.site,
            "adapter_used": self.adapter_used,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "review_reached": self.review_reached,
            "submitted": self.submitted,
            "step_count": self.step_count,
            "fields_filled": self.fields_filled,
            "fields_failed": self.fields_failed,
            "screenshots": self.screenshots,
            "failure_reason": self.failure_reason,
            "llm_calls": self.llm_calls,
            "notes": self.notes,
            "fields_manifest": self.fields_manifest,
            "steps": [
                {
                    "index": s.index,
                    "step_type": s.step_type,
                    "started_at": s.started_at,
                    "completed_at": s.completed_at,
                    "duration_ms": s.duration_ms(),
                    "fields_filled": s.fields_filled,
                    "fields_failed": s.fields_failed,
                    "screenshot_path": s.screenshot_path,
                    "notes": s.notes,
                }
                for s in self.steps
            ],
        }

    def save(self, output_dir: Path) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"{self.run_id}-summary.json"
        path.write_text(json.dumps(self.to_dict(), indent=2))
        return path


class ScreenshotManager:
    """Manages screenshot capture and file naming for a single run."""

    def __init__(self, output_dir: Path, run_id: str) -> None:
        self.output_dir = output_dir / "screenshots" / run_id
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self._counter = 0
        self._log = get_logger("screenshots")

    async def capture(self, page: Any, label: str) -> str | None:
        """Capture a screenshot, return the file path or None on failure."""
        self._counter += 1
        filename = f"{self._counter:02d}-{_slug(label)}.png"
        path = self.output_dir / filename
        try:
            await page.screenshot(path=str(path), full_page=False)
            self._log.debug(f"screenshot captured | label={label} path={path}")
            return str(path)
        except Exception as exc:
            self._log.warning(f"screenshot failed | label={label} error={exc}")
            return None

    async def capture_failure(self, page: Any, label: str = "failure") -> str | None:
        """Capture a failure screenshot regardless of current state."""
        return await self.capture(page, f"FAIL-{label}")

    async def save_html(self, page: Any, label: str) -> str | None:
        """Save the current page's HTML for offline inspection."""
        filename = f"{self._counter:02d}-{_slug(label)}.html"
        path = self.output_dir / filename
        try:
            content = await page.content()
            path.write_text(content, encoding="utf-8")
            return str(path)
        except Exception as exc:
            self._log.warning(f"html snapshot failed | label={label} error={exc}")
            return None


def _slug(text: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]
