"""Persistent REST budget and bandwidth meter.

The spec lists the free-tier limits (50 calls/hour, 1000/day, 1 GB/month) as
provider *constraints* but never treats them as a design requirement. Even at
the reduced 10-symbol cap, a single reconnect storm exhausts the hourly
allowance, after which backfill and history both stop
(docs/spec-review.md A-2).

Consumption is stored in the database rather than in memory precisely because
the failure mode we care about is a crash loop: an in-memory counter would
reset on every restart and happily blow through the quota.

The bandwidth meter exists because the 1 GB/month cap is the constraint that
drove the symbol cap down from the spec's 30 to 10, and the spec never checks
it (A-1). Measuring it is the only way to confirm 10 actually fits.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..db.connection import transaction
from ..db.repository import Repository

log = logging.getLogger(__name__)


def _window_key(kind: str, moment: datetime) -> str:
    moment = moment.astimezone(UTC)
    if kind == "hour":
        return moment.strftime("%Y-%m-%dT%H")
    if kind == "day":
        return moment.strftime("%Y-%m-%d")
    if kind == "month":
        return moment.strftime("%Y-%m")
    raise ValueError(f"unknown window kind {kind!r}")


@dataclass(frozen=True)
class BudgetSnapshot:
    calls_hour: int
    calls_day: int
    bytes_month: int
    limit_hour: int
    limit_day: int
    limit_month_bytes: int

    @property
    def hour_remaining(self) -> int:
        return max(0, self.limit_hour - self.calls_hour)

    @property
    def day_remaining(self) -> int:
        return max(0, self.limit_day - self.calls_day)

    @property
    def bandwidth_ratio(self) -> float:
        if self.limit_month_bytes <= 0:
            return 0.0
        return self.bytes_month / self.limit_month_bytes


class RestBudget:
    """Token bucket over hour and day windows, backed by SQLite."""

    def __init__(
        self,
        repository: Repository,
        source: str,
        *,
        per_hour: int,
        per_day: int,
        monthly_bandwidth_bytes: int = 1_000_000_000,
    ) -> None:
        self._repo = repository
        self._source = source
        self._per_hour = per_hour
        self._per_day = per_day
        self._monthly_bytes = monthly_bandwidth_bytes
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- counters
    def _read(self, kind: str, now: datetime) -> tuple[int, int]:
        row = self._repo.connection.execute(
            "SELECT calls, bytes FROM api_usage"
            " WHERE source = ? AND window_kind = ? AND window_start = ?",
            (self._source, kind, _window_key(kind, now)),
        ).fetchone()
        return (int(row["calls"]), int(row["bytes"])) if row else (0, 0)

    def _bump(self, kind: str, now: datetime, *, calls: int = 0, byte_count: int = 0) -> None:
        with transaction(self._repo.connection) as conn:
            conn.execute(
                "INSERT INTO api_usage (source, window_kind, window_start, calls, bytes)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (source, window_kind, window_start) DO UPDATE SET"
                " calls = api_usage.calls + excluded.calls,"
                " bytes = api_usage.bytes + excluded.bytes",
                (self._source, kind, _window_key(kind, now), calls, byte_count),
            )

    def snapshot(self, now: datetime | None = None) -> BudgetSnapshot:
        now = now or datetime.now(tz=UTC)
        calls_hour, _ = self._read("hour", now)
        calls_day, _ = self._read("day", now)
        _, bytes_month = self._read("month", now)
        return BudgetSnapshot(
            calls_hour=calls_hour,
            calls_day=calls_day,
            bytes_month=bytes_month,
            limit_hour=self._per_hour,
            limit_day=self._per_day,
            limit_month_bytes=self._monthly_bytes,
        )

    # ------------------------------------------------------------- spending
    def try_acquire(self, cost: int = 1, now: datetime | None = None) -> bool:
        """Spend budget if available. Never blocks.

        The read and both counter increments live in one IMMEDIATE transaction.
        That matters now that the collector and daily-corpus timer are separate
        processes sharing the same provider allowance: a read-then-bump sequence
        in separate transactions lets both processes observe the same final
        token and overspend it.
        """
        now = now or datetime.now(tz=UTC)
        hour_key = _window_key("hour", now)
        day_key = _window_key("day", now)
        with transaction(self._repo.connection) as conn:
            hour_row = conn.execute(
                "SELECT calls FROM api_usage"
                " WHERE source = ? AND window_kind = 'hour' AND window_start = ?",
                (self._source, hour_key),
            ).fetchone()
            day_row = conn.execute(
                "SELECT calls FROM api_usage"
                " WHERE source = ? AND window_kind = 'day' AND window_start = ?",
                (self._source, day_key),
            ).fetchone()
            hour_calls = int(hour_row["calls"]) if hour_row else 0
            day_calls = int(day_row["calls"]) if day_row else 0
            if hour_calls + cost > self._per_hour or day_calls + cost > self._per_day:
                return False
            for kind, key in (("hour", hour_key), ("day", day_key)):
                conn.execute(
                    "INSERT INTO api_usage"
                    " (source, window_kind, window_start, calls, bytes)"
                    " VALUES (?, ?, ?, ?, 0)"
                    " ON CONFLICT (source, window_kind, window_start) DO UPDATE SET"
                    " calls = api_usage.calls + excluded.calls",
                    (self._source, kind, key, cost),
                )
        return True

    async def acquire(self, cost: int = 1, *, timeout: float | None = None) -> bool:
        """Wait for budget, up to ``timeout`` seconds.

        Requests queue rather than being discarded, so a gap eventually gets
        backfilled instead of being lost.
        """
        deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
        async with self._lock:
            while True:
                if self.try_acquire(cost):
                    return True
                if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                    return False
                wait = self._seconds_until_next_window()
                if deadline is not None:
                    wait = min(wait, deadline - asyncio.get_running_loop().time())
                log.info(
                    "REST budget exhausted for %s; waiting %.0fs", self._source, max(0.0, wait)
                )
                await asyncio.sleep(max(1.0, wait))

    @staticmethod
    def _seconds_until_next_window(now: datetime | None = None) -> float:
        """Seconds until the hourly window rolls over."""
        now = (now or datetime.now(tz=UTC)).astimezone(UTC)
        next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        return max(1.0, (next_hour - now).total_seconds())

    # ------------------------------------------------------------ bandwidth
    def record_bytes(self, byte_count: int, now: datetime | None = None) -> None:
        if byte_count <= 0:
            return
        now = now or datetime.now(tz=UTC)
        self._bump("day", now, byte_count=byte_count)
        self._bump("month", now, byte_count=byte_count)

    def bytes_today(self, now: datetime | None = None) -> int:
        _, byte_count = self._read("day", now or datetime.now(tz=UTC))
        return byte_count

    def bytes_this_month(self, now: datetime | None = None) -> int:
        _, byte_count = self._read("month", now or datetime.now(tz=UTC))
        return byte_count


class BandwidthMeter:
    """Turns an adapter's cumulative byte counter into budget deltas."""

    def __init__(self, budget: RestBudget, *, warn_ratio: float = 0.8) -> None:
        self._budget = budget
        self._warn_ratio = warn_ratio
        self._last_total = 0
        self._warned = False

    def update(self, cumulative_bytes: int) -> None:
        delta = cumulative_bytes - self._last_total
        if delta <= 0:
            self._last_total = cumulative_bytes
            return
        self._last_total = cumulative_bytes
        self._budget.record_bytes(delta)

        ratio = self._budget.snapshot().bandwidth_ratio
        if ratio >= self._warn_ratio and not self._warned:
            self._warned = True
            log.warning(
                "monthly ingress at %.0f%% of the configured budget; "
                "reduce symbols or move to a paid tier (see docs/spec-review.md A-1)",
                ratio * 100,
            )
        elif ratio < self._warn_ratio:
            self._warned = False
