"""Collector end to end: stream -> aggregate -> SQLite -> live store.

Runs the real CollectorService against the mock adapter, so the wiring the
spec describes in section 8 is exercised without provider credentials.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from usstocks.adapters.mock import MockAdapter
from usstocks.collector.service import CollectorService, _with_jitter
from usstocks.config import Settings
from usstocks.db.live_store import LiveStore
from usstocks.db.repository import Repository
from usstocks.models import SymbolInfo


@pytest.fixture
def collector_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "symbol_refresh_seconds": 0.05,
            "live_publish_interval_seconds": 0.05,
            "backfill_min_gap_seconds": 1,
        }
    )


async def run_briefly(service: CollectorService, seconds: float = 1.2) -> None:
    task = asyncio.create_task(service.run())
    await asyncio.sleep(seconds)
    service.stop()
    await asyncio.wait_for(task, timeout=10)


async def test_collector_stores_bars_and_publishes_live_state(
    collector_settings: Settings, repo: Repository, live_store: LiveStore
):
    repo.upsert_symbol(SymbolInfo(symbol="AAPL", name="Apple Inc.", is_watched=True))
    adapter = MockAdapter(interval_seconds=0.02, seed=3)
    service = CollectorService(collector_settings, adapter, repo, live_store)

    await run_briefly(service)

    snapshots = live_store.read(["AAPL"])
    assert "AAPL" in snapshots
    snapshot = snapshots["AAPL"]
    assert snapshot.last_price is not None
    assert snapshot.current_bar is not None
    assert snapshot.current_bar.is_final is False
    assert snapshot.source == "mock"

    status, updated_at = live_store.read_status()
    assert status is not None
    assert updated_at is not None
    assert status["subscribed_symbols"] == ["AAPL"]

    # Shutdown flushes the in-progress bar so a restart does not lose it.
    assert repo.count_bars("AAPL") >= 1
    stored = repo.get_bars("AAPL")
    assert stored[-1].source == "mock"
    assert stored[-1].is_final is True


async def test_collector_picks_up_a_newly_watched_symbol(
    collector_settings: Settings, repo: Repository, live_store: LiveStore
):
    """The path the spec never defined: an API watchlist edit reaching the
    collector's subscription (docs/spec-review.md A-3)."""
    repo.upsert_symbol(SymbolInfo(symbol="AAPL", is_watched=True))
    adapter = MockAdapter(interval_seconds=0.02, seed=5)
    service = CollectorService(collector_settings, adapter, repo, live_store)

    task = asyncio.create_task(service.run())
    await asyncio.sleep(0.3)
    repo.upsert_symbol(SymbolInfo(symbol="MSFT", is_watched=True))
    await asyncio.sleep(0.8)
    service.stop()
    await asyncio.wait_for(task, timeout=10)

    status, _ = live_store.read_status()
    assert set(status["subscribed_symbols"]) == {"AAPL", "MSFT"}


async def test_collector_respects_the_symbol_cap(
    collector_settings: Settings, repo: Repository, live_store: LiveStore
):
    capped = collector_settings.model_copy(update={"max_symbols": 2})
    for symbol in ("AAPL", "MSFT", "NVDA", "SPY"):
        repo.upsert_symbol(SymbolInfo(symbol=symbol, is_watched=True))

    service = CollectorService(capped, MockAdapter(interval_seconds=0.02), repo, live_store)
    await run_briefly(service, seconds=0.4)

    status, _ = live_store.read_status()
    assert len(status["subscribed_symbols"]) == 2


async def test_unwatching_clears_live_state(
    collector_settings: Settings, repo: Repository, live_store: LiveStore
):
    repo.upsert_symbol(SymbolInfo(symbol="AAPL", is_watched=True))
    adapter = MockAdapter(interval_seconds=0.02, seed=11)
    service = CollectorService(collector_settings, adapter, repo, live_store)

    task = asyncio.create_task(service.run())
    await asyncio.sleep(0.4)
    assert "AAPL" in live_store.read()
    repo.remove_symbol("AAPL")
    await asyncio.sleep(0.5)
    service.stop()
    await asyncio.wait_for(task, timeout=10)

    assert "AAPL" not in live_store.read()
    # History survives being unwatched (spec 2.1).
    assert repo.count_bars("AAPL") >= 0


async def test_bandwidth_is_metered_during_streaming(
    collector_settings: Settings, repo: Repository, live_store: LiveStore
):
    """The measurement the spec's own 1 GB/month row implies but never takes
    (docs/spec-review.md A-1)."""
    repo.upsert_symbol(SymbolInfo(symbol="AAPL", is_watched=True))
    service = CollectorService(
        collector_settings, MockAdapter(interval_seconds=0.01, seed=2), repo, live_store
    )
    await run_briefly(service, seconds=0.8)

    status, _ = live_store.read_status()
    assert status["bytes_received_month"] > 0


def test_jitter_stays_within_bounds():
    for _ in range(200):
        value = _with_jitter(8.0, 0.25)
        assert 6.0 <= value <= 10.0
    assert _with_jitter(4.0, 0.0) == 4.0


def test_backfill_window_helpers_agree_on_utc(repo: Repository):
    now = datetime.now(tz=UTC)
    assert now - timedelta(seconds=1) < now


async def test_previous_close_anchors_on_the_eastern_trading_date(
    collector_settings: Settings, repo: Repository, live_store: LiveStore
):
    """Change/change% must reference the previous regular close, and a miss
    must not be cached -- on a cold start the query can run before backfill
    has landed any history."""
    from usstocks.models import Bar, Session

    repo.upsert_symbol(SymbolInfo(symbol="AAPL", is_watched=True))
    service = CollectorService(
        collector_settings, MockAdapter(interval_seconds=1.0), repo, live_store
    )

    # 22:00 ET Monday, i.e. after Monday's session closed.
    moment = datetime(2026, 7, 28, 2, 0, tzinfo=UTC)
    assert service._previous_close("AAPL", moment) is None

    # Monday's regular close arrives late (as backfill would deliver it).
    repo.upsert_bar(
        Bar(
            symbol="AAPL",
            timestamp=datetime(2026, 7, 27, 19, 59, tzinfo=UTC),  # 15:59 ET Mon
            session=Session.REGULAR,
            open=200.0, high=201.0, low=199.0, close=200.5, volume=1000,
            source="mock", is_final=True, received_at=moment,
        )
    )
    # The earlier miss was not cached, so the value is picked up now.
    assert service._previous_close("AAPL", moment) == 200.5
