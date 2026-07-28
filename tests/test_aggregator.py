"""Bar aggregation rules (spec 3.3, spec-review B-2/B-3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from usstocks.collector.aggregator import BarAggregator, minute_start
from usstocks.models import Session, Trade

BASE = datetime(2026, 7, 27, 14, 30, 0, tzinfo=UTC)  # 10:30 ET, regular session


def trade(offset_seconds: float, price: float, size: int = 100, symbol: str = "AAPL") -> Trade:
    return Trade(
        symbol=symbol,
        timestamp=BASE + timedelta(seconds=offset_seconds),
        price=price,
        size=size,
        source="test",
    )


def test_minute_start_truncates():
    assert minute_start(BASE + timedelta(seconds=59.9)) == BASE


def test_ohlcv_from_trades():
    agg = BarAggregator(source="test")
    for event in [trade(0, 100.0, 10), trade(10, 103.0, 20), trade(20, 98.0, 30),
                  trade(50, 101.0, 40)]:
        assert agg.add_trade(event) == []

    bar = agg.current_bar("AAPL")
    assert bar is not None
    assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 103.0, 98.0, 101.0)
    assert bar.volume == 100
    assert bar.trade_count == 4
    assert bar.is_final is False
    assert bar.session is Session.REGULAR
    # Timestamp is the interval start, not the last trade (spec-review B-2).
    assert bar.timestamp == BASE


def test_vwap_is_volume_weighted():
    agg = BarAggregator(source="test")
    agg.add_trade(trade(0, 100.0, 100))
    agg.add_trade(trade(1, 110.0, 300))
    bar = agg.current_bar("AAPL")
    assert bar.vwap == (100.0 * 100 + 110.0 * 300) / 400


def test_next_minute_finalises_previous_bar():
    agg = BarAggregator(source="test")
    agg.add_trade(trade(10, 100.0))
    finalised = agg.add_trade(trade(70, 105.0))

    assert len(finalised) == 1
    assert finalised[0].timestamp == BASE
    assert finalised[0].is_final is True
    assert finalised[0].close == 100.0

    current = agg.current_bar("AAPL")
    assert current.timestamp == BASE + timedelta(minutes=1)
    assert current.open == 105.0


def test_roll_due_closes_a_bar_without_further_trades():
    """A thin symbol must still close its bar; waiting for the next trade
    would leave it open indefinitely."""
    agg = BarAggregator(source="test")
    agg.add_trade(trade(5, 100.0))
    assert agg.roll_due(now=BASE + timedelta(seconds=30)) == []

    finalised = agg.roll_due(now=BASE + timedelta(seconds=61))
    assert len(finalised) == 1
    assert finalised[0].is_final is True
    assert agg.current_bar("AAPL") is None


def test_quiet_minute_produces_no_bar():
    """Spec 3.3: never fabricate movement. No trades means no bar at all."""
    agg = BarAggregator(source="test")
    agg.add_trade(trade(5, 100.0))
    agg.roll_due(now=BASE + timedelta(seconds=61))

    finalised = agg.roll_due(now=BASE + timedelta(minutes=5))
    assert finalised == []
    assert agg.current_bar("AAPL") is None


def test_late_trade_merges_into_recent_bar():
    agg = BarAggregator(source="test", late_grace=timedelta(days=3650))
    agg.add_trade(trade(10, 100.0, 10))
    agg.add_trade(trade(70, 105.0, 10))  # closes the first bar

    merged = agg.add_trade(trade(30, 120.0, 5))  # belongs to the first minute
    assert len(merged) == 1
    assert merged[0].timestamp == BASE
    assert merged[0].high == 120.0
    assert merged[0].volume == 15
    assert agg.stats.late_trades_merged == 1


def test_late_trade_beyond_grace_is_dropped_and_counted():
    agg = BarAggregator(source="test", late_grace=timedelta(seconds=1))
    agg.add_trade(trade(10, 100.0))
    agg.add_trade(trade(70, 105.0))
    result = agg.add_trade(trade(30, 120.0))

    assert result == []
    assert agg.stats.late_trades_dropped == 1
    assert agg.stats.late_trades_merged == 0


def test_symbols_are_tracked_independently():
    agg = BarAggregator(source="test")
    agg.add_trade(trade(0, 100.0, symbol="AAPL"))
    agg.add_trade(trade(0, 400.0, symbol="MSFT"))
    assert agg.current_bar("AAPL").open == 100.0
    assert agg.current_bar("MSFT").open == 400.0


def test_flush_finalises_everything():
    agg = BarAggregator(source="test")
    agg.add_trade(trade(0, 100.0, symbol="AAPL"))
    agg.add_trade(trade(0, 400.0, symbol="MSFT"))
    bars = agg.flush()
    assert {bar.symbol for bar in bars} == {"AAPL", "MSFT"}
    assert all(bar.is_final for bar in bars)
    assert agg.current_bar("AAPL") is None


def test_forget_drops_unsubscribed_symbols():
    agg = BarAggregator(source="test")
    agg.add_trade(trade(0, 100.0))
    agg.forget(["AAPL"])
    assert agg.current_bar("AAPL") is None
