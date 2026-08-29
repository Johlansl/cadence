from datetime import datetime, timezone

from app.core.schedule_timing import next_run_at


def _utc(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def test_weekly_same_day_later():
    # Wed 2026-01-07 08:00 UTC -> next Wed 12:00 is same day
    got = next_run_at(
        kind="weekly", day_of_month=None, weekday=2, hour=12, minute=0,
        timezone="UTC", after=_utc(2026, 1, 7, 8, 0),
    )
    assert got == _utc(2026, 1, 7, 12, 0)


def test_weekly_wraps_to_next_week():
    # Wed 14:00 -> target Wed 12:00 already passed -> +7 days
    got = next_run_at(
        kind="weekly", day_of_month=None, weekday=2, hour=12, minute=0,
        timezone="UTC", after=_utc(2026, 1, 7, 14, 0),
    )
    assert got == _utc(2026, 1, 14, 12, 0)


def test_weekly_moves_forward_within_week():
    # Mon -> next Fri (weekday=4)
    got = next_run_at(
        kind="weekly", day_of_month=None, weekday=4, hour=3, minute=30,
        timezone="UTC", after=_utc(2026, 1, 5, 9, 0),
    )
    assert got == _utc(2026, 1, 9, 3, 30)


def test_monthly_this_month():
    got = next_run_at(
        kind="monthly", day_of_month=15, weekday=None, hour=2, minute=0,
        timezone="UTC", after=_utc(2026, 3, 4, 0, 0),
    )
    assert got == _utc(2026, 3, 15, 2, 0)


def test_monthly_rolls_to_next_month():
    got = next_run_at(
        kind="monthly", day_of_month=1, weekday=None, hour=0, minute=0,
        timezone="UTC", after=_utc(2026, 3, 4, 0, 0),
    )
    assert got == _utc(2026, 4, 1, 0, 0)


def test_monthly_year_wrap():
    got = next_run_at(
        kind="monthly", day_of_month=1, weekday=None, hour=0, minute=0,
        timezone="UTC", after=_utc(2026, 12, 20, 0, 0),
    )
    assert got == _utc(2027, 1, 1, 0, 0)


def test_timezone_offset_is_respected():
    # 02:00 Europe/Paris (UTC+1 in January) == 01:00 UTC
    got = next_run_at(
        kind="weekly", day_of_month=None, weekday=0, hour=2, minute=0,
        timezone="Europe/Paris", after=_utc(2026, 1, 5, 0, 0),  # Mon 00:00 UTC
    )
    assert got == _utc(2026, 1, 5, 1, 0)
