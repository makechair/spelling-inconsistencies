"""Adapter construction from settings."""

from __future__ import annotations

from ..config import Settings
from ..logging_setup import register_secret
from .alpaca import AlpacaAdapter
from .base import MarketDataAdapter
from .mock import MockAdapter
from .tiingo import TiingoAdapter


def build_adapter(settings: Settings, source: str | None = None) -> MarketDataAdapter:
    source = (source or settings.primary_source).lower()

    if source == "tiingo":
        register_secret(settings.tiingo_api_key)
        if not settings.tiingo_api_key:
            raise RuntimeError("USSTOCKS_TIINGO_API_KEY is not set")
        return TiingoAdapter(
            settings.tiingo_api_key,
            rest_base=settings.tiingo_rest_base,
            ws_url=settings.tiingo_ws_url,
            threshold_level=settings.tiingo_threshold_level,
        )

    if source == "alpaca":
        register_secret(settings.alpaca_api_key)
        register_secret(settings.alpaca_api_secret)
        if not (settings.alpaca_api_key and settings.alpaca_api_secret):
            raise RuntimeError("USSTOCKS_ALPACA_API_KEY / _SECRET are not set")
        return AlpacaAdapter(
            settings.alpaca_api_key,
            settings.alpaca_api_secret,
            rest_base=settings.alpaca_rest_base,
            feed=settings.alpaca_feed,
            ws_url=settings.alpaca_ws_url,
        )

    if source == "mock":
        return MockAdapter()

    raise RuntimeError(f"unknown market data source: {source!r}")
