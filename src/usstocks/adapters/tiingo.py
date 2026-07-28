"""Tiingo adapter (primary source, spec 5.1).

Two notes that shape this implementation:

* Only trade ("T") messages feed OHLCV. Quote messages carry no size and would
  produce zero-volume bars with non-executed prices (spec-review B-3). They are
  still surfaced as ``Quote`` objects for display, but the default threshold
  level asks Tiingo not to send them at all, which is also the single biggest
  lever on the 1 GB/month bandwidth budget (spec-review A-1).
* Tiingo returns intraday bar timestamps as the interval start; we keep that
  convention throughout (spec-review B-2).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import websockets

from ..calendar_us import classify
from ..models import Bar, Quote, Session, Trade
from .base import AdapterError, MarketDataAdapter, RateLimited, SymbolNotSupported

log = logging.getLogger(__name__)


class TiingoAdapter(MarketDataAdapter):
    name = "tiingo"

    def __init__(
        self,
        api_key: str,
        *,
        rest_base: str = "https://api.tiingo.com",
        ws_url: str = "wss://api.tiingo.com/iex",
        threshold_level: int = 5,
        timeout: float = 20.0,
    ) -> None:
        if not api_key:
            raise ValueError("Tiingo API key is required")
        self._api_key = api_key
        self._rest_base = rest_base.rstrip("/")
        self._ws_url = ws_url
        self._threshold_level = threshold_level
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )
        self._bytes_received = 0

    @property
    def bytes_received(self) -> int:
        return self._bytes_received

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ REST
    async def fetch_bars(self, symbol: str, start: datetime, end: datetime) -> list[Bar]:
        url = f"{self._rest_base}/iex/{symbol.lower()}/prices"
        params = {
            "startDate": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endDate": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "resampleFreq": "1min",
            "columns": "open,high,low,close,volume",
            "afterHours": "true",
            "forceFill": "false",
            "token": self._api_key,
        }
        try:
            response = await self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise AdapterError(f"tiingo request failed: {exc}") from exc

        self._bytes_received += len(response.content)
        if response.status_code == 404:
            raise SymbolNotSupported(f"tiingo has no intraday data for {symbol}")
        if response.status_code == 429:
            raise RateLimited("tiingo rate limit hit")
        if response.status_code >= 400:
            raise AdapterError(f"tiingo returned {response.status_code}: {response.text[:200]}")

        payload = response.json()
        if not isinstance(payload, list):
            raise AdapterError(f"unexpected tiingo payload type: {type(payload).__name__}")

        now = datetime.now(tz=UTC)
        bars: list[Bar] = []
        for item in payload:
            timestamp = _parse_timestamp(item.get("date"))
            if timestamp is None:
                continue
            try:
                bars.append(
                    Bar(
                        symbol=symbol.upper(),
                        timestamp=timestamp,
                        session=classify(timestamp),
                        open=float(item["open"]),
                        high=float(item["high"]),
                        low=float(item["low"]),
                        close=float(item["close"]),
                        volume=int(item.get("volume") or 0),
                        vwap=_optional_float(item.get("vwap")),
                        trade_count=_optional_int(item.get("tradesDone")),
                        source=self.name,
                        # REST history is the provider's own settled view, so
                        # it is authoritative over anything we aggregated live
                        # (spec-review B-5).
                        is_final=True,
                        received_at=now,
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("skipping malformed tiingo bar for %s: %s", symbol, exc)
        bars.sort(key=lambda bar: bar.timestamp)
        return bars

    async def search_symbols(self, query: str, limit: int = 20) -> list[dict[str, str]]:
        url = f"{self._rest_base}/tiingo/utilities/search"
        try:
            response = await self._client.get(
                url, params={"query": query, "limit": limit, "token": self._api_key}
            )
        except httpx.HTTPError as exc:
            raise AdapterError(f"tiingo search failed: {exc}") from exc

        self._bytes_received += len(response.content)
        if response.status_code == 429:
            raise RateLimited("tiingo rate limit hit")
        if response.status_code >= 400:
            raise AdapterError(f"tiingo search returned {response.status_code}")

        results = []
        for item in response.json():
            results.append(
                {
                    "symbol": (item.get("ticker") or "").upper(),
                    "name": item.get("name") or "",
                    "exchange": item.get("assetType") and item.get("exchange") or "",
                    "asset_type": item.get("assetType") or "",
                }
            )
        return [item for item in results if item["symbol"]]

    # ------------------------------------------------------------- WebSocket
    async def stream(self, symbols: list[str]) -> AsyncIterator[Trade | Quote]:
        subscribe = {
            "eventName": "subscribe",
            "authorization": self._api_key,
            "eventData": {
                "thresholdLevel": self._threshold_level,
                "tickers": [symbol.lower() for symbol in symbols],
            },
        }
        async with websockets.connect(
            self._ws_url, ping_interval=20, ping_timeout=20, max_queue=1024
        ) as socket:
            await socket.send(json.dumps(subscribe))
            async for raw in socket:
                if isinstance(raw, bytes):
                    self._bytes_received += len(raw)
                    raw = raw.decode("utf-8", "replace")
                else:
                    self._bytes_received += len(raw.encode("utf-8"))
                event = self._parse_message(raw)
                if event is not None:
                    yield event

    def _parse_message(self, raw: str) -> Trade | Quote | None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            log.debug("non-JSON frame from tiingo")
            return None

        message_type = message.get("messageType")
        if message_type == "E":
            log.warning("tiingo stream error: %s", message.get("response", {}).get("message"))
            return None
        if message_type != "A":
            return None

        data = message.get("data")
        if not isinstance(data, list) or len(data) < 4:
            return None

        # IEX payload: [kind, timestamp, nanoseconds, ticker, ...]
        kind = data[0]
        timestamp = _parse_timestamp(data[1])
        symbol = str(data[3]).upper()
        if timestamp is None or not symbol:
            return None

        if kind == "T":
            price = _optional_float(data[9] if len(data) > 9 else None)
            size = _optional_int(data[10] if len(data) > 10 else None)
            if price is None:
                return None
            return Trade(
                symbol=symbol,
                timestamp=timestamp,
                price=price,
                size=size or 0,
                source=self.name,
            )
        if kind == "Q":
            return Quote(
                symbol=symbol,
                timestamp=timestamp,
                bid=_optional_float(data[4] if len(data) > 4 else None),
                ask=_optional_float(data[7] if len(data) > 7 else None),
                source=self.name,
            )
        return None


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    # Tiingo emits nanosecond precision; datetime accepts at most microseconds.
    if "." in text:
        head, _, tail = text.partition(".")
        digits = ""
        for char in tail:
            if char.isdigit():
                digits += char
            else:
                tail = tail[len(digits) :]
                break
        else:
            tail = ""
        text = f"{head}.{digits[:6]:<06}{tail}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _optional_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _optional_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


__all__ = ["TiingoAdapter", "Session"]
