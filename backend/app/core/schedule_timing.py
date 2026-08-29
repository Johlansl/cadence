"""Compute the next run time of a maintenance-window schedule.

Pure function, no DB. The window is expressed in the schedule's own timezone;
the result is returned in UTC. day_of_month is capped at 28 (migration 0003),
so every month has it -- no month-length special-casing. DST edge cases are
left to zoneinfo's resolution of the wall-clock time.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_UTC = ZoneInfo("UTC")


def next_run_at(
    *,
    kind: str,
    day_of_month: int | None,
    weekday: int | None,
    hour: int,
    minute: int,
    timezone: str,
    after: datetime,
) -> datetime:
    """First UTC datetime strictly after `after` that matches the window."""
    tz = ZoneInfo(timezone)
    local = after.astimezone(tz)

    if kind == "weekly":
        if weekday is None:
            raise ValueError("weekly schedule needs a weekday")
        cand = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        cand += timedelta(days=(weekday - cand.weekday()) % 7)
        if cand <= local:
            cand += timedelta(days=7)
        return cand.astimezone(_UTC)

    if kind == "monthly":
        if day_of_month is None:
            raise ValueError("monthly schedule needs a day_of_month")
        cand = local.replace(
            day=day_of_month, hour=hour, minute=minute, second=0, microsecond=0
        )
        if cand <= local:
            year, month = local.year, local.month
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
            cand = cand.replace(year=year, month=month)
        return cand.astimezone(_UTC)

    raise ValueError(f"unknown schedule kind: {kind!r}")
