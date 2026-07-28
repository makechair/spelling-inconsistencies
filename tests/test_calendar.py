"""Market calendar rules (spec-review B-4)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from usstocks.calendar_us import (
    classify,
    early_closes,
    easter_sunday,
    full_holidays,
    has_open_window,
    is_trading_day,
)
from usstocks.models import Session


def et(year, month, day, hour, minute=0) -> datetime:
    from zoneinfo import ZoneInfo

    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("America/New_York"))


@pytest.mark.parametrize(
    ("year", "expected"),
    [(2024, date(2024, 3, 31)), (2025, date(2025, 4, 20)), (2026, date(2026, 4, 5))],
)
def test_easter(year, expected):
    assert easter_sunday(year) == expected


def test_known_2026_holidays():
    holidays = full_holidays(2026)
    assert date(2026, 1, 1) in holidays          # New Year's Day
    assert date(2026, 1, 19) in holidays         # MLK, 3rd Monday
    assert date(2026, 2, 16) in holidays         # Washington's Birthday
    assert date(2026, 4, 3) in holidays          # Good Friday
    assert date(2026, 5, 25) in holidays         # Memorial Day
    assert date(2026, 6, 19) in holidays         # Juneteenth
    assert date(2026, 7, 3) in holidays          # July 4 falls Saturday -> Friday
    assert date(2026, 9, 7) in holidays          # Labor Day
    assert date(2026, 11, 26) in holidays        # Thanksgiving
    assert date(2026, 12, 25) in holidays        # Christmas


def test_juneteenth_absent_before_2022():
    assert date(2021, 6, 18) not in full_holidays(2021)
    assert date(2021, 6, 19) not in full_holidays(2021)


def test_early_close_day_after_thanksgiving():
    assert date(2026, 11, 27) in early_closes(2026)


def test_early_close_excluded_when_also_a_holiday():
    # In 2026 July 3 is the observed Independence Day, so it is fully closed
    # rather than an early close.
    assert date(2026, 7, 3) not in early_closes(2026)
    assert date(2026, 7, 3) in full_holidays(2026)


def test_weekend_is_not_a_trading_day():
    assert not is_trading_day(date(2026, 7, 25))  # Saturday
    assert is_trading_day(date(2026, 7, 27))      # Monday


def test_session_classification_regular_day():
    assert classify(et(2026, 7, 27, 3, 59)) is Session.CLOSED
    assert classify(et(2026, 7, 27, 4, 0)) is Session.PRE
    assert classify(et(2026, 7, 27, 9, 29)) is Session.PRE
    assert classify(et(2026, 7, 27, 9, 30)) is Session.REGULAR
    assert classify(et(2026, 7, 27, 15, 59)) is Session.REGULAR
    assert classify(et(2026, 7, 27, 16, 0)) is Session.POST
    assert classify(et(2026, 7, 27, 19, 59)) is Session.POST
    assert classify(et(2026, 7, 27, 20, 0)) is Session.CLOSED


def test_session_classification_early_close_day():
    """The bug this prevents: a 'regular' bar at 14:00 on the day after
    Thanksgiving, when the market shut at 13:00."""
    assert classify(et(2026, 11, 27, 12, 59)) is Session.REGULAR
    assert classify(et(2026, 11, 27, 13, 0)) is Session.POST
    assert classify(et(2026, 11, 27, 16, 59)) is Session.POST
    assert classify(et(2026, 11, 27, 17, 0)) is Session.CLOSED


def test_session_classification_holiday():
    assert classify(et(2026, 12, 25, 11, 0)) is Session.CLOSED


def test_override_closes_the_market():
    override = frozenset({date(2026, 7, 27)})
    assert classify(et(2026, 7, 27, 11, 0), closed_overrides=override) is Session.CLOSED


def test_naive_datetimes_are_treated_as_utc():
    naive = datetime(2026, 7, 27, 14, 0)  # 10:00 ET
    assert classify(naive) is Session.REGULAR


def test_has_open_window_skips_closed_periods():
    # Saturday 00:00 UTC to Sunday 00:00 UTC: nothing to backfill.
    start = datetime(2026, 7, 25, 4, 0, tzinfo=UTC)
    end = datetime(2026, 7, 26, 4, 0, tzinfo=UTC)
    assert has_open_window(start, end) is False

    # Monday morning spans the pre-market open.
    start = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)  # 06:00 ET
    end = datetime(2026, 7, 27, 11, 0, tzinfo=UTC)
    assert has_open_window(start, end) is True


# ------------------------------------- reference close for change / change%
def test_reference_close_boundary_during_regular_hours():
    """Mid-session the reference is yesterday's close, so the boundary is
    today's regular open."""
    from usstocks.calendar_us import reference_close_boundary

    boundary = reference_close_boundary(et(2026, 7, 27, 11, 0))
    assert boundary == et(2026, 7, 27, 9, 30).astimezone(UTC)


def test_reference_close_boundary_in_premarket():
    from usstocks.calendar_us import reference_close_boundary

    boundary = reference_close_boundary(et(2026, 7, 27, 6, 0))
    assert boundary == et(2026, 7, 27, 9, 30).astimezone(UTC)


def test_reference_close_boundary_after_the_close():
    """Once today's regular session has finished it becomes the reference,
    which is what a broker shows during after-hours."""
    from usstocks.calendar_us import reference_close_boundary

    moment = et(2026, 7, 27, 18, 0)
    assert reference_close_boundary(moment) == moment.astimezone(UTC)


def test_reference_close_boundary_uses_the_early_close():
    from usstocks.calendar_us import reference_close_boundary

    # 13:30 ET on the day after Thanksgiving: the session ended at 13:00.
    moment = et(2026, 11, 27, 13, 30)
    assert reference_close_boundary(moment) == moment.astimezone(UTC)
    # 12:30 ET the same day is still mid-session.
    mid = et(2026, 11, 27, 12, 30)
    assert reference_close_boundary(mid) == et(2026, 11, 27, 9, 30).astimezone(UTC)


def test_reference_close_boundary_on_a_weekend():
    from usstocks.calendar_us import reference_close_boundary

    moment = et(2026, 7, 25, 12, 0)  # Saturday
    assert reference_close_boundary(moment) == moment.astimezone(UTC)
