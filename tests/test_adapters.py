"""Provider payload translation (spec 4.6: keep provider quirks in adapters)."""

from __future__ import annotations

from datetime import UTC, datetime

from usstocks.adapters.alpaca import AlpacaAdapter
from usstocks.adapters.mock import MockAdapter
from usstocks.adapters.tiingo import TiingoAdapter
from usstocks.models import Quote, Session, Trade


def tiingo() -> TiingoAdapter:
    return TiingoAdapter("test-key")


def test_tiingo_parses_a_trade():
    raw = (
        '{"messageType":"A","service":"iex","data":["T",'
        '"2026-07-27T10:30:15.123456789-04:00",1753626615123456789,"aapl",'
        'null,null,null,null,null,211.55,300]}'
    )
    event = tiingo()._parse_message(raw)
    assert isinstance(event, Trade)
    assert event.symbol == "AAPL"
    assert event.price == 211.55
    assert event.size == 300
    assert event.source == "tiingo"
    # Nanosecond precision is truncated, and the offset is normalised to UTC.
    assert event.timestamp == datetime(2026, 7, 27, 14, 30, 15, 123456, tzinfo=UTC)


def test_tiingo_parses_a_quote_but_keeps_it_separate_from_trades():
    """Quotes carry no size; folding them into OHLCV would yield zero-volume
    bars at non-executed prices (docs/spec-review.md B-3)."""
    raw = (
        '{"messageType":"A","service":"iex","data":["Q",'
        '"2026-07-27T10:30:15.000000000-04:00",1753626615000000000,"aapl",'
        '211.50,100,211.52,200]}'
    )
    event = tiingo()._parse_message(raw)
    assert isinstance(event, Quote)
    assert not isinstance(event, Trade)
    assert event.bid == 211.50


def test_tiingo_ignores_heartbeats_and_malformed_frames():
    adapter = tiingo()
    assert adapter._parse_message('{"messageType":"H"}') is None
    assert adapter._parse_message('{"messageType":"A","data":[]}') is None
    assert adapter._parse_message("not json") is None
    assert adapter._parse_message('{"messageType":"E","response":{"message":"bad"}}') is None


def test_tiingo_drops_a_trade_without_a_price():
    raw = (
        '{"messageType":"A","data":["T","2026-07-27T10:30:15Z",0,"aapl",'
        'null,null,null,null,null,null,100]}'
    )
    assert tiingo()._parse_message(raw) is None


def test_alpaca_parses_a_batch():
    adapter = AlpacaAdapter("key", "secret")
    raw = (
        '[{"T":"t","S":"AAPL","p":211.5,"s":100,"t":"2026-07-27T14:30:15.5Z"},'
        '{"T":"q","S":"MSFT","bp":430.1,"ap":430.2,"t":"2026-07-27T14:30:16Z"},'
        '{"T":"success","msg":"authenticated"}]'
    )
    events = adapter._parse_message(raw)
    assert len(events) == 2
    assert isinstance(events[0], Trade)
    assert events[0].price == 211.5
    assert events[0].source == "alpaca"
    assert isinstance(events[1], Quote)


def test_alpaca_bars_are_tagged_with_its_own_source():
    """Never blended with Tiingo: IEX-only volume differs from consolidated
    (spec 5.2)."""
    adapter = AlpacaAdapter("key", "secret")
    assert adapter.name == "alpaca"


async def test_mock_adapter_produces_a_usable_series():
    adapter = MockAdapter(seed=7)
    start = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)
    end = datetime(2026, 7, 27, 15, 0, tzinfo=UTC)
    bars = await adapter.fetch_bars("AAPL", start, end)

    assert len(bars) == 60
    assert all(bar.source == "mock" for bar in bars)
    assert all(bar.is_final for bar in bars)
    assert all(bar.session is Session.REGULAR for bar in bars)
    assert all(bar.low <= bar.open <= bar.high for bar in bars)
    assert all(bar.low <= bar.close <= bar.high for bar in bars)
    assert bars == sorted(bars, key=lambda bar: bar.timestamp)


async def test_mock_adapter_skips_closed_minutes():
    adapter = MockAdapter(seed=1)
    # A Saturday: no session is open, so no bars exist to fabricate.
    start = datetime(2026, 7, 25, 14, 0, tzinfo=UTC)
    end = datetime(2026, 7, 25, 15, 0, tzinfo=UTC)
    assert await adapter.fetch_bars("AAPL", start, end) == []


async def test_mock_search():
    results = await MockAdapter().search_symbols("app")
    assert any(entry["symbol"] == "AAPL" for entry in results)
