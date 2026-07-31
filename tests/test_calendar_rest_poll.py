"""REST polling window follows the provider rather than exchange post-market."""

from datetime import UTC, date, datetime

from usstocks.calendar_us import is_rest_poll_window


def _utc(hour: int, minute: int, day: int = 27) -> datetime:
    # July is EDT (UTC-4).
    return datetime(2026, 7, day, hour + 4, minute, tzinfo=UTC)


def test_normal_day_stops_45_minutes_after_regular_close():
    assert is_rest_poll_window(_utc(4, 0))
    assert is_rest_poll_window(_utc(16, 44))
    assert not is_rest_poll_window(_utc(16, 45))
    assert not is_rest_poll_window(_utc(19, 0))


def test_early_close_uses_the_days_regular_close():
    # 2026-11-27, the day after Thanksgiving, closes at 13:00 ET.
    # November is EST (UTC-5), so 13:45 ET is 18:45 UTC.
    assert is_rest_poll_window(datetime(2026, 11, 27, 18, 44, tzinfo=UTC))
    assert not is_rest_poll_window(datetime(2026, 11, 27, 18, 45, tzinfo=UTC))


def test_closed_override_never_polls():
    moment = _utc(12, 0)
    assert not is_rest_poll_window(moment, closed_overrides=frozenset({date(2026, 7, 27)}))
