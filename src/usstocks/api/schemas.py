"""Wire formats.

Bar timestamps go out as epoch seconds because Lightweight Charts wants
UTCTimestamp; ``timestamp_utc`` is included alongside so the payload stays
readable and self-describing for the analysis path.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field

from ..models import Bar, LiveSnapshot, SymbolInfo


class BarOut(BaseModel):
    time: int = Field(description="Interval start, epoch seconds (UTC)")
    timestamp_utc: str
    session: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float | None = None
    trade_count: int | None = None
    source: str
    is_final: bool

    @classmethod
    def from_bar(cls, bar: Bar) -> BarOut:
        return cls(
            time=int(bar.timestamp.timestamp()),
            timestamp_utc=bar.timestamp.isoformat(),
            session=bar.session.value,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            vwap=bar.vwap,
            trade_count=bar.trade_count,
            source=bar.source,
            is_final=bar.is_final,
        )


class BarsResponse(BaseModel):
    symbol: str
    count: int
    truncated: bool = False
    # When the collector last completed a fetch for this symbol, whether or not
    # it produced bars. Without it a stalled chart is unreadable: an empty
    # after-hours session and a dead collector look identical.
    checked_at: str | None = None
    bars: list[BarOut]


class CoverageOut(BaseModel):
    symbol: str
    first_date: date
    last_date: date
    daily_bars: int = 0
    minute_bars: int = 0


class SymbolOut(BaseModel):
    symbol: str
    name: str | None = None
    exchange: str | None = None
    asset_type: str | None = None
    is_watched: bool = False
    is_held: bool = False
    supported: bool | None = None
    note: str | None = None

    @classmethod
    def from_info(cls, info: SymbolInfo) -> SymbolOut:
        return cls(
            symbol=info.symbol,
            name=info.name,
            exchange=info.exchange,
            asset_type=info.asset_type,
            is_watched=info.is_watched,
            is_held=info.is_held,
            supported=info.supported,
            note=info.note,
        )


class SymbolUpsert(BaseModel):
    symbol: str
    name: str | None = None
    exchange: str | None = None
    asset_type: str | None = None
    is_watched: bool = True
    is_held: bool = False


class LiveOut(BaseModel):
    symbol: str
    last_price: float | None = None
    last_trade_at: str | None = None
    session: str
    change: float | None = None
    change_pct: float | None = None
    previous_close: float | None = None
    source: str
    updated_at: str | None = None
    # Seconds since the last trade actually received. The UI uses this to tell
    # "quiet market" from "we lost the feed" (spec 3.2).
    staleness_seconds: float | None = None
    bar: BarOut | None = None

    @classmethod
    def from_snapshot(cls, snapshot: LiveSnapshot, now: datetime) -> LiveOut:
        staleness = None
        if snapshot.last_trade_at is not None:
            staleness = max(0.0, (now - snapshot.last_trade_at).total_seconds())
        return cls(
            symbol=snapshot.symbol,
            last_price=snapshot.last_price,
            last_trade_at=(
                snapshot.last_trade_at.isoformat() if snapshot.last_trade_at else None
            ),
            session=snapshot.session.value,
            change=snapshot.change,
            change_pct=snapshot.change_pct,
            previous_close=snapshot.previous_close,
            source=snapshot.source,
            updated_at=snapshot.updated_at.isoformat() if snapshot.updated_at else None,
            staleness_seconds=staleness,
            bar=BarOut.from_bar(snapshot.current_bar) if snapshot.current_bar else None,
        )


class HealthOut(BaseModel):
    status: str
    api: dict
    collector: dict
    database: dict
    bandwidth: dict
    rest_budget: dict
