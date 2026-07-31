"""Gap backfill over REST (spec 3.3, 11.2).

The naive reading of the spec — "on reconnect, fetch from the last stored bar
to now" — costs one call per symbol per reconnect and exhausts a 50 calls/hour
budget after two reconnect storms (docs/spec-review.md A-2). Four things keep
it affordable:

1. Requests are coalesced per symbol: a queued symbol is not queued twice, and
   the widest requested range wins.
2. Gaps shorter than ``min_gap_seconds`` are ignored; a two-second blip loses
   nothing worth a REST call.
3. Gaps that fall entirely outside market hours are skipped, since there is no
   data to recover.
4. Every fetch spends budget from the persistent bucket and waits if empty,
   rather than failing or hammering.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..adapters.base import (
    AdapterError,
    MarketDataAdapter,
    RateLimited,
    SymbolNotSupported,
)
from ..calendar_us import has_open_window
from ..db.repository import Repository
from .ratelimit import RestBudget

log = logging.getLogger(__name__)


@dataclass
class BackfillRequest:
    symbol: str
    start: datetime
    end: datetime

    def widen(self, other: BackfillRequest) -> BackfillRequest:
        return BackfillRequest(
            symbol=self.symbol,
            start=min(self.start, other.start),
            end=max(self.end, other.end),
        )


@dataclass
class BackfillResult:
    symbol: str
    bars_written: int
    skipped_reason: str | None = None


class BackfillCoordinator:
    def __init__(
        self,
        adapter: MarketDataAdapter,
        repository: Repository,
        budget: RestBudget,
        *,
        min_gap_seconds: int = 120,
        max_lookback_days: int = 30,
        closed_overrides: frozenset = frozenset(),
        rate_limit_cooldown_seconds: float = 300.0,
        empty_fetch_warning_threshold: int = 3,
    ) -> None:
        self._adapter = adapter
        self._repo = repository
        self._budget = budget
        self._min_gap = timedelta(seconds=min_gap_seconds)
        self._max_lookback = timedelta(days=max_lookback_days)
        self._closed_overrides = closed_overrides
        self._pending: dict[str, BackfillRequest] = {}
        self._lock = asyncio.Lock()
        self._cooldown = timedelta(seconds=rate_limit_cooldown_seconds)
        self._retry_after: datetime | None = None
        self._empty_fetch_warning_threshold = empty_fetch_warning_threshold
        self._consecutive_empty_fetches: dict[str, int] = {}

    async def request(self, symbol: str, start: datetime, end: datetime) -> None:
        """Queue a gap. Duplicate symbols merge instead of stacking."""
        symbol = symbol.upper()
        candidate = BackfillRequest(symbol=symbol, start=start, end=end)
        async with self._lock:
            existing = self._pending.get(symbol)
            self._pending[symbol] = existing.widen(candidate) if existing else candidate

    async def request_gap_since_last_bar(self, symbol: str, now: datetime | None = None) -> bool:
        """Queue whatever is missing between the last stored bar and now."""
        now = now or datetime.now(tz=UTC)
        symbol = symbol.upper()
        last = self._repo.last_bar_timestamp(symbol, self._adapter.name)
        start = last + timedelta(minutes=1) if last else now - self._max_lookback
        start = max(start, now - self._max_lookback)
        if now - start < self._min_gap:
            return False
        if not has_open_window(start, now, closed_overrides=self._closed_overrides):
            log.debug("skipping backfill for %s: market was closed for the gap", symbol)
            return False
        await self.request(symbol, start, now)
        return True

    async def drain(self, *, budget_timeout: float | None = 300.0) -> list[BackfillResult]:
        """Process the queue, spending REST budget one symbol at a time."""
        # Without this, a re-queued request would come straight back around on
        # the next loop and spend another budget token on a call the provider
        # is still refusing.
        if self._retry_after is not None:
            if datetime.now(tz=UTC) < self._retry_after:
                return []
            self._retry_after = None

        async with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()

        results: list[BackfillResult] = []
        for request in pending:
            results.append(await self._execute(request, budget_timeout))
        return results

    async def _execute(
        self, request: BackfillRequest, budget_timeout: float | None
    ) -> BackfillResult:
        if not await self._budget.acquire(1, timeout=budget_timeout):
            # Put it back; the next drain will retry once budget frees up.
            await self.request(request.symbol, request.start, request.end)
            log.warning("backfill for %s deferred: REST budget unavailable", request.symbol)
            return BackfillResult(request.symbol, 0, "budget_unavailable")

        try:
            bars = await self._adapter.fetch_bars(request.symbol, request.start, request.end)
        except SymbolNotSupported as exc:
            log.warning("%s is not supported by %s: %s", request.symbol, self._adapter.name, exc)
            self._repo.set_supported(request.symbol, False, note=str(exc))
            return BackfillResult(request.symbol, 0, "unsupported")
        except RateLimited as exc:
            # Re-queue, exactly as an exhausted local budget does. The provider
            # can refuse even when our own bucket has room -- failed calls still
            # count on their side, and anything else using the key spends from
            # the same allowance. Dropping the request here meant the gap stayed
            # open until something unrelated queued the symbol again, so a
            # transient 429 turned into permanently missing history.
            await self.request(request.symbol, request.start, request.end)
            self._retry_after = datetime.now(tz=UTC) + self._cooldown
            log.warning(
                "backfill for %s deferred until %s: %s",
                request.symbol,
                self._retry_after.isoformat(timespec="seconds"),
                exc,
            )
            return BackfillResult(request.symbol, 0, "rate_limited")
        except AdapterError as exc:
            log.warning("backfill failed for %s: %s", request.symbol, exc)
            return BackfillResult(request.symbol, 0, "error")

        written = self._repo.upsert_bars(bars)
        if bars:
            self._consecutive_empty_fetches.pop(request.symbol, None)
            self._repo.set_supported(request.symbol, True)
            self._repo.record_state(
                request.symbol,
                self._adapter.name,
                last_bar=bars[-1].timestamp,
                last_backfill=datetime.now(tz=UTC),
            )
        else:
            count = self._consecutive_empty_fetches.get(request.symbol, 0) + 1
            self._consecutive_empty_fetches[request.symbol] = count
            if count >= self._empty_fetch_warning_threshold:
                note = (
                    f"{self._adapter.name} returned no bars for {count} "
                    "consecutive successful fetches"
                )
                self._repo.set_supported(request.symbol, False, note=note)
                log.warning("%s: %s", request.symbol, note)
            self._repo.record_state(
                request.symbol, self._adapter.name, last_backfill=datetime.now(tz=UTC)
            )
        log.info(
            "backfilled %s: %d bars covering %s..%s",
            request.symbol,
            len(bars),
            request.start.isoformat(timespec="minutes"),
            request.end.isoformat(timespec="minutes"),
        )
        return BackfillResult(request.symbol, written)

    @property
    def pending_count(self) -> int:
        return len(self._pending)
