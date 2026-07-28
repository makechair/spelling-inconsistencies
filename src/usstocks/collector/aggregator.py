"""Trade -> 1-minute bar aggregation.

Rules, and why:

* A bar's timestamp is the interval START (spec-review B-2).
* Only trades contribute. Quotes are never folded in (spec-review B-3).
* A bar is emitted as final when a trade for a later minute arrives, or when
  ``roll_due`` is called past the minute boundary plus a grace period. Waiting
  for the next trade alone would leave the last bar of a thin symbol open
  forever.
* A trade that arrives after its minute closed is folded back into the stored
  bar within ``late_grace``; beyond that it is counted and dropped, because
  silently rewriting old bars hides a real data-quality problem.
* Nothing is invented when no trade arrives. A minute with no trades produces
  no bar (spec 3.3: do not artificially move values).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ..calendar_us import classify
from ..models import Bar, Session, Trade


def minute_start(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(second=0, microsecond=0)


@dataclass
class _Working:
    """A bar still accepting trades."""

    symbol: str
    timestamp: datetime
    session: Session
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    trade_count: int = 0
    notional: float = 0.0
    last_trade_at: datetime | None = None

    def apply(self, trade: Trade) -> None:
        self.high = max(self.high, trade.price)
        self.low = min(self.low, trade.price)
        self.close = trade.price
        self.volume += max(0, trade.size)
        self.trade_count += 1
        self.notional += trade.price * max(0, trade.size)
        if self.last_trade_at is None or trade.timestamp > self.last_trade_at:
            self.last_trade_at = trade.timestamp

    def to_bar(self, source: str, *, is_final: bool) -> Bar:
        return Bar(
            symbol=self.symbol,
            timestamp=self.timestamp,
            session=self.session,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            vwap=(self.notional / self.volume) if self.volume else None,
            trade_count=self.trade_count,
            source=source,
            is_final=is_final,
            received_at=datetime.now(tz=UTC),
        )


@dataclass
class AggregatorStats:
    trades_seen: int = 0
    bars_finalised: int = 0
    late_trades_merged: int = 0
    late_trades_dropped: int = 0
    out_of_universe: int = 0


@dataclass
class BarAggregator:
    """Per-symbol working bars for a single source."""

    source: str
    late_grace: timedelta = timedelta(seconds=90)
    closed_overrides: frozenset = frozenset()
    early_overrides: frozenset = frozenset()
    _working: dict[str, _Working] = field(default_factory=dict)
    _recent_final: dict[tuple[str, datetime], Bar] = field(default_factory=dict)
    stats: AggregatorStats = field(default_factory=AggregatorStats)

    # ---------------------------------------------------------------- ingest
    def add_trade(self, trade: Trade) -> list[Bar]:
        """Feed one trade. Returns any bars that just became final."""
        self.stats.trades_seen += 1
        bucket = minute_start(trade.timestamp)
        finalised: list[Bar] = []

        working = self._working.get(trade.symbol)
        if working is not None and bucket > working.timestamp:
            finalised.append(self._finalise(trade.symbol))
            working = None
        elif working is not None and bucket < working.timestamp:
            # Trade belongs to an already-closed minute.
            merged = self._merge_late(trade, bucket)
            if merged is not None:
                finalised.append(merged)
            return finalised

        if working is None:
            self._working[trade.symbol] = _Working(
                symbol=trade.symbol,
                timestamp=bucket,
                session=classify(
                    bucket,
                    closed_overrides=self.closed_overrides,
                    early_overrides=self.early_overrides,
                ),
                open=trade.price,
                high=trade.price,
                low=trade.price,
                close=trade.price,
            )
            working = self._working[trade.symbol]

        working.apply(trade)
        return finalised

    def _merge_late(self, trade: Trade, bucket: datetime) -> Bar | None:
        key = (trade.symbol, bucket)
        recent = self._recent_final.get(key)
        age = datetime.now(tz=UTC) - bucket
        if recent is None or age > self.late_grace:
            self.stats.late_trades_dropped += 1
            return None

        recent.high = max(recent.high, trade.price)
        recent.low = min(recent.low, trade.price)
        recent.close = trade.price
        recent.volume += max(0, trade.size)
        recent.trade_count = (recent.trade_count or 0) + 1
        if recent.vwap is not None and recent.volume:
            # Recompute from the running notional we can reconstruct.
            prior_notional = recent.vwap * (recent.volume - max(0, trade.size))
            recent.vwap = (prior_notional + trade.price * max(0, trade.size)) / recent.volume
        recent.received_at = datetime.now(tz=UTC)
        self.stats.late_trades_merged += 1
        return recent

    # ----------------------------------------------------------------- roll
    def roll_due(self, now: datetime | None = None) -> list[Bar]:
        """Finalise bars whose minute has ended.

        Call on a timer; a symbol that stops trading must still close its bar.
        """
        now = now or datetime.now(tz=UTC)
        current_bucket = minute_start(now)
        finalised = [
            self._finalise(symbol)
            for symbol, working in list(self._working.items())
            if working.timestamp < current_bucket
        ]
        self._expire_recent(now)
        return finalised

    def _finalise(self, symbol: str) -> Bar:
        working = self._working.pop(symbol)
        bar = working.to_bar(self.source, is_final=True)
        self._recent_final[(symbol, bar.timestamp)] = bar
        self.stats.bars_finalised += 1
        return bar

    def _expire_recent(self, now: datetime) -> None:
        cutoff = now - self.late_grace
        for key in [key for key in self._recent_final if key[1] < cutoff]:
            del self._recent_final[key]

    # --------------------------------------------------------------- readers
    def current_bar(self, symbol: str) -> Bar | None:
        """The in-progress bar, marked non-final."""
        working = self._working.get(symbol)
        return working.to_bar(self.source, is_final=False) if working else None

    def snapshot_all(self) -> list[Bar]:
        return [working.to_bar(self.source, is_final=False) for working in self._working.values()]

    def flush(self) -> list[Bar]:
        """Finalise everything (shutdown path)."""
        return [self._finalise(symbol) for symbol in list(self._working)]

    def forget(self, symbols: list[str]) -> None:
        for symbol in symbols:
            self._working.pop(symbol, None)
