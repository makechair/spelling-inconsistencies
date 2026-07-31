"""Gap backfill and its budget discipline (spec 3.3, spec-review A-2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from usstocks.adapters.base import MarketDataAdapter, RateLimited, SymbolNotSupported
from usstocks.collector.backfill import BackfillCoordinator
from usstocks.collector.ratelimit import RestBudget
from usstocks.db.repository import Repository
from usstocks.models import Bar, Session, SymbolInfo

# A Monday, mid regular session.
NOW = datetime(2026, 7, 27, 15, 0, tzinfo=UTC)


class RecordingAdapter(MarketDataAdapter):
    name = "tiingo"

    def __init__(self, *, bars_per_call: int = 3, fail_with: Exception | None = None) -> None:
        self.calls: list[tuple[str, datetime, datetime]] = []
        self._bars_per_call = bars_per_call
        self._fail_with = fail_with

    async def fetch_bars(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        if self._fail_with is not None:
            raise self._fail_with
        return [
            Bar(
                symbol=symbol,
                timestamp=start + timedelta(minutes=index),
                session=Session.REGULAR,
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=1000,
                source=self.name,
                is_final=True,
                received_at=NOW,
            )
            for index in range(self._bars_per_call)
        ]

    async def search_symbols(self, query, limit=20):
        return []

    def stream(self, symbols):  # pragma: no cover - unused here
        raise NotImplementedError


def make_coordinator(repo: Repository, adapter, **kwargs) -> BackfillCoordinator:
    budget = RestBudget(repo, adapter.name, per_hour=50, per_day=1000)
    return BackfillCoordinator(adapter, repo, budget, **kwargs)


async def test_drain_fetches_and_stores(repo: Repository):
    adapter = RecordingAdapter()
    coordinator = make_coordinator(repo, adapter)

    await coordinator.request("AAPL", NOW - timedelta(minutes=10), NOW)
    results = await coordinator.drain()

    assert len(adapter.calls) == 1
    assert results[0].bars_written == 3
    assert repo.count_bars("AAPL") == 3


async def test_duplicate_requests_coalesce_into_one_call(repo: Repository):
    """Reconnect storms must not multiply REST calls (spec-review A-2)."""
    adapter = RecordingAdapter()
    coordinator = make_coordinator(repo, adapter)

    await coordinator.request("AAPL", NOW - timedelta(minutes=10), NOW - timedelta(minutes=5))
    await coordinator.request("AAPL", NOW - timedelta(minutes=30), NOW)
    await coordinator.request("AAPL", NOW - timedelta(minutes=8), NOW)
    await coordinator.drain()

    assert len(adapter.calls) == 1
    symbol, start, end = adapter.calls[0]
    # The merged request covers the widest span requested.
    assert start == NOW - timedelta(minutes=30)
    assert end == NOW


async def test_short_gap_is_not_worth_a_rest_call(repo: Repository):
    adapter = RecordingAdapter()
    coordinator = make_coordinator(repo, adapter, min_gap_seconds=120)
    repo.upsert_symbol(SymbolInfo(symbol="AAPL", is_watched=True))
    repo.upsert_bar(
        Bar(
            symbol="AAPL",
            timestamp=NOW - timedelta(seconds=30),
            session=Session.REGULAR,
            open=1, high=1, low=1, close=1, volume=1,
            source="tiingo", is_final=True, received_at=NOW,
        )
    )

    queued = await coordinator.request_gap_since_last_bar("AAPL", now=NOW)
    assert queued is False
    assert coordinator.pending_count == 0


async def test_gap_entirely_outside_market_hours_is_skipped(repo: Repository):
    """No data exists to recover over a weekend, so spending budget on it is
    pure waste."""
    adapter = RecordingAdapter()
    coordinator = make_coordinator(repo, adapter)
    # 19:59 ET Friday -- the final minute of after-hours, so the gap that
    # follows begins exactly at the 20:00 ET close.
    friday_close = datetime(2026, 7, 24, 23, 59, tzinfo=UTC)
    repo.upsert_bar(
        Bar(
            symbol="AAPL",
            timestamp=friday_close,
            session=Session.POST,
            open=1, high=1, low=1, close=1, volume=1,
            source="tiingo", is_final=True, received_at=friday_close,
        )
    )
    saturday = datetime(2026, 7, 25, 18, 0, tzinfo=UTC)

    assert await coordinator.request_gap_since_last_bar("AAPL", now=saturday) is False
    assert coordinator.pending_count == 0

    # The same gap on a Monday morning does span an open window, so it is
    # queued rather than skipped.
    monday = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)
    assert await coordinator.request_gap_since_last_bar("AAPL", now=monday) is True


async def test_exhausted_budget_defers_instead_of_dropping(repo: Repository):
    adapter = RecordingAdapter()
    budget = RestBudget(repo, "tiingo", per_hour=0, per_day=0)
    coordinator = BackfillCoordinator(adapter, repo, budget)

    await coordinator.request("AAPL", NOW - timedelta(minutes=10), NOW)
    results = await coordinator.drain(budget_timeout=0.01)

    assert results[0].skipped_reason == "budget_unavailable"
    assert adapter.calls == []
    # Re-queued, so it is retried rather than lost.
    assert coordinator.pending_count == 1


async def test_unsupported_symbol_is_flagged_not_retried_forever(repo: Repository):
    adapter = RecordingAdapter(fail_with=SymbolNotSupported("no data"))
    coordinator = make_coordinator(repo, adapter)
    repo.upsert_symbol(SymbolInfo(symbol="XYZQ", is_watched=True))

    await coordinator.request("XYZQ", NOW - timedelta(minutes=10), NOW)
    results = await coordinator.drain()

    assert results[0].skipped_reason == "unsupported"
    assert repo.get_symbol("XYZQ").supported is False


@pytest.mark.parametrize("bars", [0, 5])
async def test_state_is_recorded_after_backfill(repo: Repository, bars: int):
    adapter = RecordingAdapter(bars_per_call=bars)
    coordinator = make_coordinator(repo, adapter)

    await coordinator.request("AAPL", NOW - timedelta(minutes=10), NOW)
    await coordinator.drain()

    state = repo.get_state("AAPL", "tiingo")
    assert state is not None
    assert state["last_backfill_utc"] is not None


async def test_consecutive_empty_fetches_set_and_success_clears_warning(
    repo: Repository,
):
    adapter = RecordingAdapter(bars_per_call=0)
    coordinator = make_coordinator(repo, adapter, empty_fetch_warning_threshold=3)
    repo.upsert_symbol(SymbolInfo(symbol="AAPL", is_watched=True))

    for _ in range(2):
        await coordinator.request("AAPL", NOW - timedelta(minutes=10), NOW)
        await coordinator.drain()
    assert repo.get_symbol("AAPL").supported is None

    await coordinator.request("AAPL", NOW - timedelta(minutes=10), NOW)
    await coordinator.drain()
    warned = repo.get_symbol("AAPL")
    assert warned.supported is False
    assert "3 consecutive successful fetches" in (warned.note or "")

    adapter._bars_per_call = 1
    await coordinator.request("AAPL", NOW - timedelta(minutes=10), NOW)
    await coordinator.drain()
    recovered = repo.get_symbol("AAPL")
    assert recovered.supported is True
    assert recovered.note is None


async def test_provider_rate_limit_requeues_and_backs_off(repo: Repository):
    """A 429 is not the same as a permanent failure.

    The provider can refuse while our own bucket still has room: failed calls
    count on their side too, and anything else using the key spends the same
    allowance. Treating it like any other AdapterError dropped the request, so
    the gap stayed open until something unrelated happened to queue that symbol
    again -- on a fresh install, nothing ever did.
    """
    adapter = RecordingAdapter(fail_with=RateLimited("tiingo rate limit hit"))
    coordinator = make_coordinator(repo, adapter, rate_limit_cooldown_seconds=300.0)

    await coordinator.request("AAPL", NOW - timedelta(minutes=10), NOW)
    results = await coordinator.drain()

    assert results[0].skipped_reason == "rate_limited"
    assert coordinator.pending_count == 1

    # The cooldown holds the retry back rather than spending another token on a
    # call the provider is still refusing.
    assert await coordinator.drain() == []
    assert len(adapter.calls) == 1
    assert coordinator.pending_count == 1


async def test_backfill_resumes_once_the_cooldown_expires(repo: Repository):
    adapter = RecordingAdapter(fail_with=RateLimited("tiingo rate limit hit"))
    coordinator = make_coordinator(repo, adapter, rate_limit_cooldown_seconds=0.0)

    await coordinator.request("AAPL", NOW - timedelta(minutes=10), NOW)
    await coordinator.drain()

    adapter._fail_with = None
    results = await coordinator.drain()

    assert results[0].bars_written == 3
    assert coordinator.pending_count == 0
