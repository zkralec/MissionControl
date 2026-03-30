from __future__ import annotations

"""Inactive legacy Handshake collector kept for compatibility coverage."""

from typing import Any

from .base import collect_board_jobs, supported_fields

SOURCE = "handshake"
ACTIVE = False
SUPPORTED_FIELDS = supported_fields(SOURCE)


def collect_jobs(request: dict[str, Any], *, url_override: str | None = None) -> dict[str, Any]:
    return collect_board_jobs(SOURCE, request, url_override=url_override)
