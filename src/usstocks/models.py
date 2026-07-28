"""Internal domain model.

Provider payloads are translated into these types by the adapters, so nothing
downstream of ``usstocks.adapters`` knows what Tiingo or Alpaca look like.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime


class Session(enum.StrEnum):
    """US equity trading session, in Eastern time."""

    PRE = "pre"
    REGULAR = "regular"
    POST = "post"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class Trade:
    """A single executed trade."""

    symbol: str
    timestamp: datetime  # timezone-aware, UTC
    price: float
    size: int
    source: str


@dataclass(frozen=True, slots=True)
class Quote:
    """Top-of-book quote. Never folded into OHLCV (see spec-review B-3)."""

    symbol: str
    timestamp: datetime
    bid: float | None
    ask: float | None
    source: str


@dataclass(slots=True)
class Bar:
    """A one-minute OHLCV bar.

    ``timestamp`` is the *start* of the interval and is always UTC. The
    09:30:00 bar covers trades in [09:30:00.000, 09:30:59.999]. The spec left
    this undefined (spec-review B-2); adapters normalise to this convention.
    """

    symbol: str
    timestamp: datetime
    session: Session
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float | None = None
    trade_count: int | None = None
    source: str = "unknown"
    is_final: bool = False
    received_at: datetime | None = None

    def as_row(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "timestamp_utc": self.timestamp.isoformat(),
            "session": self.session.value,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "vwap": self.vwap,
            "trade_count": self.trade_count,
            "source": self.source,
            "is_final": int(self.is_final),
            "received_at": (self.received_at or datetime.now(tz=UTC)).isoformat(),
        }


@dataclass(slots=True)
class SymbolInfo:
    symbol: str
    name: str | None = None
    exchange: str | None = None
    asset_type: str | None = None
    is_watched: bool = False
    is_held: bool = False
    supported: bool | None = None  # None = not yet determined (spec gap D-6)
    note: str | None = None


@dataclass(slots=True)
class LiveSnapshot:
    """What the browser sees between bar closes."""

    symbol: str
    last_price: float | None = None
    last_trade_at: datetime | None = None
    session: Session = Session.CLOSED
    current_bar: Bar | None = None
    previous_close: float | None = None
    source: str = "unknown"
    updated_at: datetime | None = None

    @property
    def change(self) -> float | None:
        if self.last_price is None or self.previous_close is None:
            return None
        return self.last_price - self.previous_close

    @property
    def change_pct(self) -> float | None:
        if self.previous_close in (None, 0) or self.last_price is None:
            return None
        return (self.last_price - self.previous_close) / self.previous_close * 100.0


@dataclass(slots=True)
class CollectorStatus:
    """Health surface for the collector process (spec 3.6)."""

    source: str = "unknown"
    connected: bool = False
    connected_since: datetime | None = None
    last_message_at: datetime | None = None
    last_trade_at: datetime | None = None
    subscribed_symbols: list[str] = field(default_factory=list)
    reconnect_count: int = 0
    last_error: str | None = None
    bytes_received_today: int = 0
    bytes_received_month: int = 0
    rest_calls_hour: int = 0
    rest_calls_day: int = 0
    updated_at: datetime | None = None
