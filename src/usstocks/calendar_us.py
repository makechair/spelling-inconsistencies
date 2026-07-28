"""US equity market calendar and session classification.

The spec requires a ``session`` column and asks the UI to distinguish pre /
regular / post (3.2, 10.1) but never says how those boundaries are decided.
Getting it wrong invents "regular" bars on Thanksgiving afternoon, so the rules
are computed here rather than assumed:

* Regular session   09:30-16:00 ET (13:00 ET on an early-close day)
* Pre-market        04:00-09:30 ET
* After-hours       16:00-20:00 ET (13:00-17:00 ET on an early-close day)

Holidays are derived, not table-driven, so the calendar does not expire.
Unscheduled closures (national days of mourning) cannot be derived; the
repository layer supplies overrides from the ``market_calendar_overrides``
table.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from .models import Session

EASTERN = ZoneInfo("America/New_York")

PRE_OPEN = time(4, 0)
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
POST_CLOSE = time(20, 0)
EARLY_REGULAR_CLOSE = time(13, 0)
EARLY_POST_CLOSE = time(17, 0)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the nth `weekday` (Mon=0) of a month."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    """Weekend observance: Saturday -> preceding Friday, Sunday -> next Monday."""
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def easter_sunday(year: int) -> date:
    """Anonymous Gregorian algorithm. Needed only for Good Friday."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    lunar = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lunar) // 451
    month, day = divmod(h + lunar - 7 * m + 114, 31)
    return date(year, month, day + 1)


@lru_cache(maxsize=32)
def full_holidays(year: int) -> frozenset[date]:
    """Days on which NYSE/Nasdaq do not trade at all."""
    good_friday = easter_sunday(year) - timedelta(days=2)
    days = {
        _observed(date(year, 1, 1)),  # New Year's Day
        _nth_weekday(year, 1, 0, 3),  # MLK Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        good_friday,
        _last_weekday(year, 5, 0),  # Memorial Day
        _observed(date(year, 7, 4)),  # Independence Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),  # Christmas
    }
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))  # Juneteenth
    # New Year's Day falling on Saturday is not observed on the prior Friday by
    # the exchanges -- that Friday belongs to the previous year and trades.
    if date(year, 1, 1).weekday() == 5:
        days.discard(date(year - 1, 12, 31))
        days.discard(date(year, 1, 1) - timedelta(days=1))
    return frozenset(d for d in days if d.year == year)


@lru_cache(maxsize=32)
def early_closes(year: int) -> frozenset[date]:
    """Days with a 13:00 ET close."""
    days: set[date] = set()
    holidays = full_holidays(year)

    # Day after Thanksgiving.
    days.add(_nth_weekday(year, 11, 3, 4) + timedelta(days=1))

    # July 3rd, when it is a weekday and the 4th is the observed holiday.
    july3 = date(year, 7, 3)
    if july3.weekday() < 5 and date(year, 7, 4).weekday() < 5:
        days.add(july3)

    # Christmas Eve, when it is a weekday and Christmas itself is a weekday.
    dec24 = date(year, 12, 24)
    if dec24.weekday() < 5 and date(year, 12, 25).weekday() < 5:
        days.add(dec24)

    return frozenset(d for d in days if d.weekday() < 5 and d not in holidays)


def is_trading_day(day: date, *, closed_overrides: frozenset[date] = frozenset()) -> bool:
    if day.weekday() >= 5:
        return False
    if day in closed_overrides:
        return False
    return day not in full_holidays(day.year)


def session_bounds(
    day: date,
    *,
    closed_overrides: frozenset[date] = frozenset(),
    early_overrides: frozenset[date] = frozenset(),
) -> dict[Session, tuple[time, time]] | None:
    """Return ET session windows for a trading day, or None if closed."""
    if not is_trading_day(day, closed_overrides=closed_overrides):
        return None
    early = day in early_closes(day.year) or day in early_overrides
    regular_close = EARLY_REGULAR_CLOSE if early else REGULAR_CLOSE
    post_close = EARLY_POST_CLOSE if early else POST_CLOSE
    return {
        Session.PRE: (PRE_OPEN, REGULAR_OPEN),
        Session.REGULAR: (REGULAR_OPEN, regular_close),
        Session.POST: (regular_close, post_close),
    }


def classify(
    moment: datetime,
    *,
    closed_overrides: frozenset[date] = frozenset(),
    early_overrides: frozenset[date] = frozenset(),
) -> Session:
    """Classify an instant into a trading session.

    ``moment`` may be naive (assumed UTC) or timezone-aware.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    eastern = moment.astimezone(EASTERN)
    bounds = session_bounds(
        eastern.date(),
        closed_overrides=closed_overrides,
        early_overrides=early_overrides,
    )
    if bounds is None:
        return Session.CLOSED
    clock = eastern.time()
    for session, (start, end) in bounds.items():
        if start <= clock < end:
            return session
    return Session.CLOSED


def reference_close_boundary(
    moment: datetime,
    *,
    closed_overrides: frozenset[date] = frozenset(),
    early_overrides: frozenset[date] = frozenset(),
) -> datetime:
    """Upper bound for the "previous close" used by change / change%.

    Spec 3.2 asks for 前日比 without saying what counts as the previous close
    once the session has ended. The rule here is "the most recent *completed*
    regular session":

    * pre-market or during regular hours -> the prior trading day's close
    * after the regular close (including after-hours) -> today's own close,
      which is what a broker screen shows in extended trading
    * a weekend or holiday -> the last trading day's close

    Returns the instant that bars must precede to qualify.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    eastern = moment.astimezone(EASTERN)
    bounds = session_bounds(
        eastern.date(),
        closed_overrides=closed_overrides,
        early_overrides=early_overrides,
    )
    if bounds is None:
        # Not a trading day: everything before now is already settled.
        return moment
    regular_open, regular_close = bounds[Session.REGULAR]
    if eastern.time() >= regular_close:
        return moment
    return datetime.combine(eastern.date(), regular_open, tzinfo=EASTERN).astimezone(UTC)


def previous_trading_day(day: date, *, closed_overrides: frozenset[date] = frozenset()) -> date:
    cursor = day - timedelta(days=1)
    for _ in range(15):
        if is_trading_day(cursor, closed_overrides=closed_overrides):
            return cursor
        cursor -= timedelta(days=1)
    raise ValueError(f"no trading day found before {day}")


def market_open_utc(day: date) -> datetime:
    """UTC instant of the regular open for a given ET date."""
    return datetime.combine(day, REGULAR_OPEN, tzinfo=EASTERN).astimezone(UTC)


def has_open_window(
    start: datetime,
    end: datetime,
    *,
    closed_overrides: frozenset[date] = frozenset(),
) -> bool:
    """Was any extended-hours window open in [start, end)?

    Used to skip REST backfill for gaps that happened while the market was
    shut, which is the cheapest way to protect the 50 calls/hour budget
    (spec-review A-2).
    """
    if end <= start:
        return False
    cursor = start.astimezone(EASTERN).date()
    last = end.astimezone(EASTERN).date()
    while cursor <= last:
        bounds = session_bounds(cursor, closed_overrides=closed_overrides)
        if bounds is not None:
            day_start = datetime.combine(cursor, PRE_OPEN, tzinfo=EASTERN).astimezone(UTC)
            close_time = bounds[Session.POST][1]
            day_end = datetime.combine(cursor, close_time, tzinfo=EASTERN).astimezone(UTC)
            if start < day_end and end > day_start:
                return True
        cursor += timedelta(days=1)
    return False
