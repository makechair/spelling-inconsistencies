"""Synthetic adapter for development and tests.

Exists so the full pipeline (collector -> SQLite -> API -> SSE -> chart) can be
exercised without provider credentials, which is what phase 2 of the spec's
plan needs before phase 1's live comparison is done.

Its data is fabricated and is tagged ``source='mock'`` in every row, so it can
never be mistaken for market data in the database or on screen. It is not
selectable unless explicitly configured.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from ..calendar_us import classify
from ..models import Bar, Quote, Trade
from .base import MarketDataAdapter

_UNIVERSE = {
    "AAPL": ("Apple Inc.", 210.0),
    "MSFT": ("Microsoft Corporation", 430.0),
    "NVDA": ("NVIDIA Corporation", 125.0),
    "VOO": ("Vanguard S&P 500 ETF", 520.0),
    "SPY": ("SPDR S&P 500 ETF Trust", 560.0),
}


class MockAdapter(MarketDataAdapter):
    name = "mock"

    def __init__(self, *, interval_seconds: float = 0.5, seed: int | None = None) -> None:
        self._interval = interval_seconds
        self._random = random.Random(seed)
        self._prices: dict[str, float] = {}
        self._bytes_received = 0

    @property
    def bytes_received(self) -> int:
        return self._bytes_received

    def _price(self, symbol: str) -> float:
        if symbol not in self._prices:
            base = _UNIVERSE.get(symbol, (None, 100.0))[1]
            self._prices[symbol] = base
        return self._prices[symbol]

    async def fetch_bars(self, symbol: str, start: datetime, end: datetime) -> list[Bar]:
        symbol = symbol.upper()
        cursor = start.astimezone(UTC).replace(second=0, microsecond=0)
        end = end.astimezone(UTC)
        price = self._price(symbol)
        now = datetime.now(tz=UTC)
        bars: list[Bar] = []
        while cursor < end and len(bars) < 20_000:
            session = classify(cursor)
            if session.value != "closed":
                drift = self._random.gauss(0, price * 0.0008)
                open_price = price
                close_price = max(0.01, price + drift)
                high = max(open_price, close_price) * (1 + abs(self._random.gauss(0, 0.0004)))
                low = min(open_price, close_price) * (1 - abs(self._random.gauss(0, 0.0004)))
                volume = self._random.randint(500, 40_000)
                bars.append(
                    Bar(
                        symbol=symbol,
                        timestamp=cursor,
                        session=session,
                        open=round(open_price, 4),
                        high=round(high, 4),
                        low=round(low, 4),
                        close=round(close_price, 4),
                        volume=volume,
                        vwap=round((high + low + close_price) / 3, 4),
                        trade_count=self._random.randint(5, 300),
                        source=self.name,
                        is_final=True,
                        received_at=now,
                    )
                )
                price = close_price
            cursor += timedelta(minutes=1)
        self._prices[symbol] = price
        return bars

    async def search_symbols(self, query: str, limit: int = 20) -> list[dict[str, str]]:
        needle = query.strip().upper()
        matches = [
            {
                "symbol": symbol,
                "name": name,
                "exchange": "MOCK",
                "asset_type": "Stock",
            }
            for symbol, (name, _) in _UNIVERSE.items()
            if needle in symbol or needle in name.upper()
        ]
        return matches[:limit]

    async def stream(self, symbols: list[str]) -> AsyncIterator[Trade | Quote]:
        symbols = [symbol.upper() for symbol in symbols]
        if not symbols:
            # Mirror a real stream: idle rather than terminate.
            while True:
                await asyncio.sleep(self._interval)
        while True:
            await asyncio.sleep(self._interval)
            symbol = self._random.choice(symbols)
            price = max(0.01, self._price(symbol) + self._random.gauss(0, 0.05))
            self._prices[symbol] = price
            self._bytes_received += 160
            yield Trade(
                symbol=symbol,
                timestamp=datetime.now(tz=UTC),
                price=round(price, 4),
                size=self._random.randint(1, 500),
                source=self.name,
            )
