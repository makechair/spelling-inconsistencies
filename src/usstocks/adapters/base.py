"""Adapter contract.

Spec 4.6 requires provider-specific handling to be isolated so Tiingo can be
swapped for another API. Everything above this line speaks only in
``usstocks.models`` types.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from datetime import datetime

from ..models import Bar, Quote, Trade


class AdapterError(RuntimeError):
    """Provider call failed in a way the caller may retry."""


class SymbolNotSupported(AdapterError):
    """The provider does not serve this symbol (spec 3.1, spec-review D-6)."""


class RateLimited(AdapterError):
    """Provider rejected the call for quota reasons."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class MarketDataAdapter(abc.ABC):
    """A single market data provider."""

    name: str = "base"

    @abc.abstractmethod
    async def fetch_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        """Historical 1-minute bars, interval-start timestamps, UTC.

        Costs one unit of REST budget; callers must acquire it first.
        """

    @abc.abstractmethod
    async def search_symbols(self, query: str, limit: int = 20) -> list[dict[str, str]]:
        """Ticker or company-name search (spec 3.1)."""

    @abc.abstractmethod
    def stream(self, symbols: list[str]) -> AsyncIterator[Trade | Quote]:
        """Yield normalised live events until cancelled or disconnected.

        Implementations raise on disconnect; reconnection and backoff are the
        collector's responsibility, not the adapter's.
        """

    async def close(self) -> None:
        return None

    @property
    def bytes_received(self) -> int:
        """Cumulative stream bytes, for the bandwidth meter (spec-review A-1)."""
        return 0
