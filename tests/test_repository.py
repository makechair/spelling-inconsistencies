"""Upsert precedence and source resolution (spec-review B-1, B-5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from usstocks.db.repository import Repository
from usstocks.models import Bar, Session, SymbolInfo

BASE = datetime(2026, 7, 27, 14, 30, tzinfo=UTC)


def make_bar(
    *,
    minute: int = 0,
    close: float = 100.0,
    source: str = "tiingo",
    is_final: bool = True,
    volume: int = 1000,
    symbol: str = "AAPL",
) -> Bar:
    timestamp = BASE + timedelta(minutes=minute)
    return Bar(
        symbol=symbol,
        timestamp=timestamp,
        session=Session.REGULAR,
        open=100.0,
        high=max(100.0, close),
        low=min(100.0, close),
        close=close,
        volume=volume,
        vwap=close,
        trade_count=10,
        source=source,
        is_final=is_final,
        received_at=datetime.now(tz=UTC),
    )


def test_roundtrip(repo: Repository):
    repo.upsert_bar(make_bar())
    bars = repo.get_bars("AAPL")
    assert len(bars) == 1
    assert bars[0].close == 100.0
    assert bars[0].timestamp == BASE
    assert bars[0].session is Session.REGULAR


def test_final_bar_is_not_overwritten_by_in_progress_bar(repo: Repository):
    """The restart hazard: a fresh collector rebuilds a partial bar for a
    minute that already closed. It must not clobber the finished one."""
    repo.upsert_bar(make_bar(close=105.0, volume=5000, is_final=True))
    repo.upsert_bar(make_bar(close=101.0, volume=12, is_final=False))

    stored = repo.get_bars("AAPL")[0]
    assert stored.close == 105.0
    assert stored.volume == 5000
    assert stored.is_final is True


def test_in_progress_bar_is_replaced_by_newer_in_progress_bar(repo: Repository):
    repo.upsert_bar(make_bar(close=100.0, volume=10, is_final=False))
    repo.upsert_bar(make_bar(close=102.0, volume=40, is_final=False))
    stored = repo.get_bars("AAPL")[0]
    assert stored.close == 102.0
    assert stored.volume == 40


def test_rest_final_overwrites_live_final(repo: Repository):
    """REST history is the provider's settled view and wins over what we
    aggregated from the stream."""
    repo.upsert_bar(make_bar(close=100.0, volume=900, is_final=True))
    repo.upsert_bar(make_bar(close=100.5, volume=1150, is_final=True))
    stored = repo.get_bars("AAPL")[0]
    assert stored.close == 100.5
    assert stored.volume == 1150


def test_sources_coexist_and_priority_decides(repo: Repository):
    repo.upsert_bar(make_bar(close=100.0, source="tiingo"))
    repo.upsert_bar(make_bar(close=99.0, source="alpaca"))

    # Both rows are kept -- spec 5.2 forbids discarding the other provider.
    raw = repo.connection.execute("SELECT COUNT(*) AS n FROM bars_1m").fetchone()["n"]
    assert raw == 2

    resolved = repo.get_bars("AAPL")
    assert len(resolved) == 1
    assert resolved[0].source == "tiingo"
    assert resolved[0].close == 100.0


def test_priority_order_is_configurable(db_path):
    with Repository(db_path, source_priority=["alpaca", "tiingo"]) as repo:
        repo.upsert_bar(make_bar(close=100.0, source="tiingo"))
        repo.upsert_bar(make_bar(close=99.0, source="alpaca"))
        resolved = repo.get_bars("AAPL")
        assert len(resolved) == 1
        assert resolved[0].source == "alpaca"


def test_source_filter_inspects_a_single_provider(repo: Repository):
    repo.upsert_bar(make_bar(close=100.0, source="tiingo"))
    repo.upsert_bar(make_bar(close=99.0, source="alpaca"))
    only_alpaca = repo.get_bars("AAPL", sources=["alpaca"])
    assert len(only_alpaca) == 1
    assert only_alpaca[0].close == 99.0


def test_time_range_filtering_is_half_open(repo: Repository):
    for minute in range(5):
        repo.upsert_bar(make_bar(minute=minute, close=100.0 + minute))

    bars = repo.get_bars("AAPL", BASE + timedelta(minutes=1), BASE + timedelta(minutes=3))
    assert [bar.timestamp for bar in bars] == [
        BASE + timedelta(minutes=1),
        BASE + timedelta(minutes=2),
    ]


def test_aggregated_bars_preserve_ohlcv_and_apply_limit_after_bucketing(
    repo: Repository,
):
    for minute in range(30):
        repo.upsert_bar(
            make_bar(
                minute=minute,
                close=100.0 + minute,
                volume=100 + minute,
            )
        )

    bars = repo.get_aggregated_bars(
        "AAPL",
        BASE,
        BASE + timedelta(minutes=30),
        interval="15m",
        limit=3,
        newest_first=True,
        sessions=["regular"],
    )

    assert len(bars) == 2
    assert bars[0].timestamp == BASE
    assert bars[0].open == 100.0
    assert bars[0].close == 114.0
    assert bars[0].high == 114.0
    assert bars[0].low == 100.0
    assert bars[0].volume == sum(100 + minute for minute in range(15))
    assert bars[1].close == 129.0


def test_last_bar_timestamp_is_per_source(repo: Repository):
    repo.upsert_bar(make_bar(minute=0, source="tiingo"))
    repo.upsert_bar(make_bar(minute=9, source="alpaca"))
    assert repo.last_bar_timestamp("AAPL", "tiingo") == BASE
    assert repo.last_bar_timestamp("AAPL", "alpaca") == BASE + timedelta(minutes=9)
    assert repo.last_bar_timestamp("AAPL", "nobody") is None


def test_previous_close_uses_regular_session_only(repo: Repository):
    yesterday = BASE - timedelta(days=1)
    regular = make_bar(close=180.0)
    regular.timestamp = yesterday
    repo.upsert_bar(regular)

    after_hours = make_bar(close=999.0)
    after_hours.timestamp = yesterday + timedelta(hours=3)
    after_hours.session = Session.POST
    repo.upsert_bar(after_hours)

    assert repo.previous_close("AAPL", BASE) == 180.0


def test_watchlist_lifecycle_keeps_history(repo: Repository):
    repo.upsert_symbol(SymbolInfo(symbol="AAPL", name="Apple Inc.", is_watched=True))
    repo.upsert_bar(make_bar())
    assert repo.watched_symbols() == ["AAPL"]

    repo.remove_symbol("AAPL")
    assert repo.watched_symbols() == []
    # Unwatching must not destroy accumulated bars (spec 2.1).
    assert repo.count_bars("AAPL") == 1
    assert repo.get_symbol("AAPL") is not None


def test_upsert_symbol_preserves_unknown_metadata(repo: Repository):
    repo.upsert_symbol(SymbolInfo(symbol="AAPL", name="Apple Inc.", is_watched=True))
    repo.upsert_symbol(SymbolInfo(symbol="AAPL", is_watched=True, is_held=True))
    info = repo.get_symbol("AAPL")
    assert info.name == "Apple Inc."
    assert info.is_held is True


def test_supported_flag(repo: Repository):
    repo.upsert_symbol(SymbolInfo(symbol="XYZQ", is_watched=True))
    assert repo.get_symbol("XYZQ").supported is None
    repo.set_supported("XYZQ", False, note="no intraday data")
    info = repo.get_symbol("XYZQ")
    assert info.supported is False
    assert info.note == "no intraday data"
    repo.set_supported("XYZQ", True)
    info = repo.get_symbol("XYZQ")
    assert info.supported is True
    assert info.note is None


def test_iter_bars_streams_multiple_symbols(repo: Repository):
    for minute in range(3):
        repo.upsert_bar(make_bar(minute=minute, symbol="AAPL"))
        repo.upsert_bar(make_bar(minute=minute, symbol="MSFT"))
    rows = list(repo.iter_bars(["AAPL", "MSFT"]))
    assert len(rows) == 6
    assert {bar.symbol for bar in rows} == {"AAPL", "MSFT"}


def test_viewing_is_recorded_and_expires(repo: Repository):
    """The collector aims its REST allowance with this.

    Neither free tier streams any more (spec-review A-6), so 50 calls an hour is
    the whole live budget. Spread over ten symbols it is one refresh every
    twelve minutes; aimed at the symbol on screen it is one every seventy-two
    seconds. The API is the only process that knows which that is.
    """
    now = datetime(2026, 7, 29, 15, 0, tzinfo=UTC)
    for symbol in ("AAPL", "MSFT", "NVDA"):
        repo.upsert_symbol(SymbolInfo(symbol=symbol, is_watched=True))

    repo.mark_viewed(["AAPL"], now=now - timedelta(seconds=30))
    repo.mark_viewed(["MSFT"], now=now - timedelta(minutes=20))

    # Most recent first: that ordering is what picks the foreground symbol.
    assert repo.recently_viewed(timedelta(hours=1), now=now) == ["AAPL", "MSFT"]
    # Outside the window a symbol stops counting as watched, so a closed browser
    # does not keep spending the allowance on a chart nobody is reading.
    assert repo.recently_viewed(timedelta(minutes=5), now=now) == ["AAPL"]
    # Never viewed at all is not "viewed long ago".
    assert "NVDA" not in repo.recently_viewed(timedelta(days=365), now=now)


def test_viewing_ignores_unwatched_symbols(repo: Repository):
    """Opening a chart for something no longer tracked must not attract polls."""
    now = datetime(2026, 7, 29, 15, 0, tzinfo=UTC)
    repo.upsert_symbol(SymbolInfo(symbol="OLD", is_watched=False))
    repo.mark_viewed(["OLD"], now=now)
    assert repo.recently_viewed(timedelta(hours=1), now=now) == []
