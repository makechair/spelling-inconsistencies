"""Alpaca IEX adapter (standby source, spec 5, 5.2).

Deliberately never blended with Tiingo data: Alpaca Basic serves IEX-only
volume, so its OHLCV differs from a consolidated feed. Bars land under
``source='alpaca'`` and the read path picks one source per minute by priority
(spec-review B-1). Switching is an explicit operator action, not automatic.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import websockets

from ..calendar_us import classify
from ..models import Bar, Quote, Trade
from .base import AdapterError, MarketDataAdapter, RateLimited, SymbolNotSupported

log = logging.getLogger(__name__)


class AlpacaAdapter(MarketDataAdapter):
    name = "alpaca"

    # Alpaca Basic caps concurrent subscriptions; exceeding it drops the
    # connection rather than truncating (spec 5 comparison table).
    max_subscriptions = 30

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        rest_base: str = "https://data.alpaca.markets",
        feed: str = "iex",
        ws_url: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("Alpaca key and secret are required")
        self._key = api_key
        self._secret = api_secret
        self._feed = feed
        # Derived from the feed unless overridden. Holding the feed name in two
        # places -- a REST parameter and a path segment in the socket URL --
        # invites setting one and not the other, which reads as "the paid feed
        # is not working" rather than as a half-applied setting.
        self._ws_url = ws_url or f"wss://stream.data.alpaca.markets/v2/{feed}"

        self._client = httpx.AsyncClient(
            base_url=rest_base.rstrip("/"),
            timeout=timeout,
            headers={
                "APCA-API-KEY-ID": api_key,
                "APCA-API-SECRET-KEY": api_secret,
            },
        )
        self._bytes_received = 0

    @property
    def bytes_received(self) -> int:
        return self._bytes_received

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch_bars(self, symbol: str, start: datetime, end: datetime) -> list[Bar]:
        bars: list[Bar] = []
        page_token: str | None = None
        now = datetime.now(tz=UTC)
        while True:
            params = {
                "symbols": symbol.upper(),
                "timeframe": "1Min",
                "start": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "limit": 10_000,
                "feed": self._feed,
                "adjustment": "raw",
            }
            if page_token:
                params["page_token"] = page_token
            try:
                response = await self._client.get("/v2/stocks/bars", params=params)
            except httpx.HTTPError as exc:
                raise AdapterError(f"alpaca request failed: {exc}") from exc

            self._bytes_received += len(response.content)
            if response.status_code == 404:
                raise SymbolNotSupported(f"alpaca has no data for {symbol}")
            if response.status_code == 429:
                raise RateLimited("alpaca rate limit hit")
            if response.status_code >= 400:
                raise AdapterError(
                    f"alpaca returned {response.status_code}: {response.text[:200]}"
                )

            payload = response.json()
            for item in (payload.get("bars") or {}).get(symbol.upper(), []):
                timestamp = _parse_timestamp(item.get("t"))
                if timestamp is None:
                    continue
                bars.append(
                    Bar(
                        symbol=symbol.upper(),
                        timestamp=timestamp,
                        session=classify(timestamp),
                        open=float(item["o"]),
                        high=float(item["h"]),
                        low=float(item["l"]),
                        close=float(item["c"]),
                        volume=int(item.get("v") or 0),
                        vwap=_optional_float(item.get("vw")),
                        trade_count=_optional_int(item.get("n")),
                        source=self.name,
                        is_final=True,
                        received_at=now,
                    )
                )
            page_token = payload.get("next_page_token")
            if not page_token:
                break
        bars.sort(key=lambda bar: bar.timestamp)
        return bars

    async def search_symbols(self, query: str, limit: int = 20) -> list[dict[str, str]]:
        # The market-data host has no search endpoint; symbol discovery stays
        # with the primary source.
        raise AdapterError("alpaca adapter does not provide symbol search")

    async def stream(self, symbols: list[str]) -> AsyncIterator[Trade | Quote]:
        if len(symbols) > self.max_subscriptions:
            raise AdapterError(
                f"alpaca basic allows {self.max_subscriptions} symbols, got {len(symbols)}"
            )
        async with websockets.connect(
            self._ws_url, ping_interval=20, ping_timeout=20, max_queue=1024
        ) as socket:
            await socket.send(
                json.dumps({"action": "auth", "key": self._key, "secret": self._secret})
            )
            await socket.send(
                json.dumps({"action": "subscribe", "trades": [s.upper() for s in symbols]})
            )
            async for raw in socket:
                if isinstance(raw, bytes):
                    self._bytes_received += len(raw)
                    raw = raw.decode("utf-8", "replace")
                else:
                    self._bytes_received += len(raw.encode("utf-8"))
                for event in self._parse_message(raw):
                    yield event

    def _parse_message(self, raw: str) -> list[Trade | Quote]:
        try:
            messages = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(messages, list):
            return []

        events: list[Trade | Quote] = []
        for message in messages:
            kind = message.get("T")
            if kind == "error":
                log.warning("alpaca stream error: %s", message.get("msg"))
                continue
            if kind in {"c", "x"}:
                # Subscribing to trades subscribes to two companion channels as
                # well -- corrections and cancelErrors -- because an exchange can
                # retract or restate a trade it already reported. Those arrive
                # here, and this adapter has nothing to do with them: a bar
                # already aggregated from the retracted trade keeps a price the
                # exchange withdrew.
                #
                # The impact is one minute's extremes moving slightly, and the
                # REST view of that minute is authoritative (spec-review B-5) --
                # but backfill only visits minutes with gaps, so a minute filled
                # live is never re-fetched and never corrected. Logged rather
                # than silently dropped so the frequency is knowable before
                # deciding whether it earns the re-backfill path.
                log.warning(
                    "alpaca %s for %s at %s: bar may retain a retracted trade",
                    "correction" if kind == "c" else "cancel",
                    message.get("S"),
                    message.get("t"),
                )
                continue
            timestamp = _parse_timestamp(message.get("t"))
            symbol = str(message.get("S") or "").upper()
            if timestamp is None or not symbol:
                continue
            if kind == "t":
                price = _optional_float(message.get("p"))
                if price is None:
                    continue
                events.append(
                    Trade(
                        symbol=symbol,
                        timestamp=timestamp,
                        price=price,
                        size=_optional_int(message.get("s")) or 0,
                        source=self.name,
                    )
                )
            elif kind == "q":
                events.append(
                    Quote(
                        symbol=symbol,
                        timestamp=timestamp,
                        bid=_optional_float(message.get("bp")),
                        ask=_optional_float(message.get("ap")),
                        source=self.name,
                    )
                )
        return events


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    if "." in text:
        head, _, tail = text.partition(".")
        digits = "".join(char for char in tail if char.isdigit())
        suffix = tail[len(digits) :]
        text = f"{head}.{digits[:6]:<06}{suffix}"
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
