"""Operational day helpers for consistent daily window boundaries."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_OPERATIONAL_DAY_TZ = "America/New_York"
OPERATIONAL_DAY_TZ_ENV = "MISSION_CONTROL_DAY_BOUNDARY_TZ"


def _as_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def get_operational_day_timezone_name() -> str:
    raw = os.getenv(OPERATIONAL_DAY_TZ_ENV, "").strip()
    return raw or DEFAULT_OPERATIONAL_DAY_TZ


def get_operational_day_timezone() -> tzinfo:
    name = get_operational_day_timezone_name()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name != DEFAULT_OPERATIONAL_DAY_TZ:
            try:
                return ZoneInfo(DEFAULT_OPERATIONAL_DAY_TZ)
            except ZoneInfoNotFoundError:
                return timezone.utc
        return timezone.utc


def current_operational_day_window_utc(now: datetime | None = None) -> tuple[datetime, datetime]:
    current = _as_utc(now or datetime.now(timezone.utc))
    tz = get_operational_day_timezone()
    local_now = current.astimezone(tz)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def operational_day_window_utc(for_date: date) -> tuple[datetime, datetime]:
    tz = get_operational_day_timezone()
    local_start = datetime(for_date.year, for_date.month, for_date.day, tzinfo=tz)
    local_end = local_start + timedelta(days=1)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def operational_day_date(now: datetime | None = None) -> date:
    current = _as_utc(now or datetime.now(timezone.utc))
    return current.astimezone(get_operational_day_timezone()).date()
