"""The collector process.

Owns: the WebSocket connection, bar aggregation, all time-series writes, the
REST budget, and the live snapshot the API serves (spec 8, 9, 11).

Reconnection follows spec 11.2 (1s, 2s, 4s ... capped at 60s, reset on a clean
connection) with added jitter (spec-review D-4). After reconnecting, missing
minutes are queued for REST backfill.

Subscription changes reach this process by polling the ``symbols`` table. The
spec never defined a path from the API's watchlist edits to the collector's
subscriptions (spec-review A-3); polling a table avoids adding a broker.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from datetime import UTC, datetime, timedelta

from ..adapters.base import MarketDataAdapter
from ..calendar_us import classify, reference_close_boundary
from ..config import Settings
from ..db.live_store import LiveStore
from ..db.repository import Repository
from ..models import Bar, CollectorStatus, LiveSnapshot, Quote, Trade
from .aggregator import BarAggregator
from .backfill import BackfillCoordinator
from .ratelimit import BandwidthMeter, RestBudget

log = logging.getLogger(__name__)


class CollectorService:
    def __init__(
        self,
        settings: Settings,
        adapter: MarketDataAdapter,
        repository: Repository,
        live_store: LiveStore,
    ) -> None:
        self._settings = settings
        self._adapter = adapter
        self._repo = repository
        self._live = live_store

        closed, early = repository.calendar_overrides()
        self._closed_overrides = closed
        self._early_overrides = early

        self._aggregator = BarAggregator(
            source=adapter.name,
            late_grace=timedelta(seconds=settings.late_trade_grace_seconds),
            closed_overrides=closed,
            early_overrides=early,
        )
        self._budget = RestBudget(
            repository,
            adapter.name,
            per_hour=settings.rest_calls_per_hour,
            per_day=settings.rest_calls_per_day,
            monthly_bandwidth_bytes=settings.monthly_bandwidth_bytes,
        )
        self._bandwidth = BandwidthMeter(
            self._budget, warn_ratio=settings.bandwidth_warn_ratio
        )
        self._backfill = BackfillCoordinator(
            adapter,
            repository,
            self._budget,
            min_gap_seconds=settings.backfill_min_gap_seconds,
            closed_overrides=closed,
        )

        self._symbols: list[str] = []
        self._snapshots: dict[str, LiveSnapshot] = {}
        self._prev_closes: dict[tuple[str, datetime], float] = {}
        self._dirty: set[str] = set()
        self._status = CollectorStatus(source=adapter.name)
        self._stop = asyncio.Event()
        self._resubscribe = asyncio.Event()

    # ------------------------------------------------------------- lifecycle
    async def run(self) -> None:
        self._symbols = self._load_symbols()
        log.info("collector starting with %d symbol(s): %s", len(self._symbols), self._symbols)
        await self._queue_startup_backfill()

        tasks = [
            asyncio.create_task(self._stream_loop(), name="stream"),
            asyncio.create_task(self._roll_loop(), name="roll"),
            asyncio.create_task(self._publish_loop(), name="publish"),
            asyncio.create_task(self._symbol_watch_loop(), name="symbols"),
            asyncio.create_task(self._backfill_loop(), name="backfill"),
            asyncio.create_task(self._maintenance_loop(), name="maintenance"),
        ]
        try:
            await self._stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._shutdown()

    def stop(self) -> None:
        self._stop.set()

    async def _shutdown(self) -> None:
        # Persist whatever is half-built so a restart does not lose the minute.
        final_bars = self._aggregator.flush()
        if final_bars:
            self._repo.upsert_bars(final_bars)
            log.info("flushed %d in-progress bar(s) on shutdown", len(final_bars))
        self._status.connected = False
        self._write_status()
        await self._adapter.close()

    # ---------------------------------------------------------------- stream
    async def _stream_loop(self) -> None:
        delay = self._settings.reconnect_initial_seconds
        while not self._stop.is_set():
            if not self._symbols:
                await asyncio.sleep(self._settings.symbol_refresh_seconds)
                continue

            connected_at = datetime.now(tz=UTC)
            error: str | None = None
            intentional = False
            try:
                intentional = await self._consume_stream()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any failure means reconnect
                error = f"{type(exc).__name__}: {exc}"

            self._status.connected = False
            self._status.last_error = error
            self._write_status()
            if self._stop.is_set():
                return

            if intentional:
                # Subscription change, not a fault: reconnect immediately.
                delay = self._settings.reconnect_initial_seconds
                continue

            self._status.reconnect_count += 1
            # A connection that stayed up counts as healthy, so the next
            # disconnect starts from the bottom of the ladder again
            # (spec 11.2: reset backoff after a normal connection).
            if (datetime.now(tz=UTC) - connected_at) >= timedelta(seconds=60):
                delay = self._settings.reconnect_initial_seconds

            sleep_for = _with_jitter(delay, self._settings.reconnect_jitter_ratio)
            log.warning(
                "stream disconnected (%s); reconnecting in %.1fs",
                error or "closed by server",
                sleep_for,
            )
            await self._queue_gap_backfill(connected_at)
            await asyncio.sleep(sleep_for)
            delay = min(delay * 2, self._settings.reconnect_max_seconds)

    async def _consume_stream(self) -> bool:
        """Consume events until the stream ends.

        Returns True when the exit was intentional (a subscription change or
        shutdown), so the caller can skip the backoff ladder.
        """
        symbols = list(self._symbols)
        log.info("subscribing to %d symbol(s) on %s", len(symbols), self._adapter.name)
        stream = self._adapter.stream(symbols)
        self._status.connected = True
        self._status.connected_since = datetime.now(tz=UTC)
        self._status.subscribed_symbols = symbols
        self._status.last_error = None
        self._write_status()

        try:
            async for event in stream:
                if self._stop.is_set() or self._resubscribe.is_set():
                    self._resubscribe.clear()
                    return True
                self._status.last_message_at = datetime.now(tz=UTC)
                self._bandwidth.update(self._adapter.bytes_received)
                if isinstance(event, Trade):
                    self._handle_trade(event)
                elif isinstance(event, Quote):
                    self._handle_quote(event)
        finally:
            with contextlib.suppress(Exception):
                await stream.aclose()  # type: ignore[attr-defined]
        return False

    # --------------------------------------------------------------- handlers
    def _handle_trade(self, trade: Trade) -> None:
        if trade.symbol not in self._snapshots and trade.symbol not in self._symbols:
            self._aggregator.stats.out_of_universe += 1
            return

        self._status.last_trade_at = trade.timestamp
        finalised = self._aggregator.add_trade(trade)
        if finalised:
            self._persist_bars(finalised)

        snapshot = self._snapshots.setdefault(
            trade.symbol, LiveSnapshot(symbol=trade.symbol)
        )
        snapshot.last_price = trade.price
        snapshot.last_trade_at = trade.timestamp
        snapshot.session = classify(
            trade.timestamp,
            closed_overrides=self._closed_overrides,
            early_overrides=self._early_overrides,
        )
        snapshot.current_bar = self._aggregator.current_bar(trade.symbol)
        snapshot.previous_close = self._previous_close(trade.symbol, trade.timestamp)
        snapshot.source = trade.source
        snapshot.updated_at = datetime.now(tz=UTC)
        self._dirty.add(trade.symbol)

        if self._settings.tick_retention_days > 0:
            self._repo.insert_ticks(
                [
                    (
                        trade.symbol,
                        trade.timestamp.isoformat(),
                        trade.price,
                        trade.size,
                        trade.source,
                    )
                ]
            )

    def _handle_quote(self, quote: Quote) -> None:
        # Quotes never enter OHLCV (spec-review B-3). Kept only as a liveness
        # signal so a quiet symbol does not look stale.
        self._status.last_message_at = quote.timestamp

    def _persist_bars(self, bars: list[Bar]) -> None:
        if not bars:
            return
        self._repo.upsert_bars(bars)
        for bar in bars:
            self._repo.record_state(bar.symbol, bar.source, last_bar=bar.timestamp)
            self._dirty.add(bar.symbol)

    def _previous_close(self, symbol: str, moment: datetime) -> float | None:
        """Reference price for the change / change% display (spec 3.2).

        The reference is the most recent *completed* regular session's close,
        computed by ``calendar_us.reference_close_boundary``. Anchoring on the
        Eastern trading date rather than UTC midnight keeps that correct
        year-round and across the DST shift.

        Cached per (symbol, boundary). A miss is not cached: on a cold start
        the query can run before backfill has landed any history, and caching
        that None would freeze the display at "--" for the rest of the session.
        """
        boundary = reference_close_boundary(
            moment,
            closed_overrides=self._closed_overrides,
            early_overrides=self._early_overrides,
        )
        # Bucket to the minute so an intraday boundary does not defeat the
        # cache on every trade.
        cache_key = (symbol, boundary.replace(second=0, microsecond=0))
        cached = self._prev_closes.get(cache_key)
        if cached is not None:
            return cached

        value = self._repo.previous_close(symbol, boundary)
        if value is not None:
            self._prev_closes[cache_key] = value
        return value

    # ------------------------------------------------------------------ loops
    async def _roll_loop(self) -> None:
        """Close bars whose minute ended even if no further trade arrives."""
        while not self._stop.is_set():
            await asyncio.sleep(1.0)
            finalised = self._aggregator.roll_due()
            if finalised:
                self._persist_bars(finalised)
                for bar in finalised:
                    snapshot = self._snapshots.get(bar.symbol)
                    if snapshot is not None:
                        snapshot.current_bar = self._aggregator.current_bar(bar.symbol)

    async def _publish_loop(self) -> None:
        """Push changed snapshots to the live store.

        Only dirty symbols are written, so a market with no trades produces no
        writes and the browser sees no artificial movement (spec 3.3).
        """
        while not self._stop.is_set():
            await asyncio.sleep(self._settings.live_publish_interval_seconds)
            if not self._dirty:
                continue
            symbols = list(self._dirty)
            self._dirty.clear()
            payload = []
            for symbol in symbols:
                snapshot = self._snapshots.get(symbol)
                if snapshot is None:
                    continue
                snapshot.current_bar = self._aggregator.current_bar(symbol) or snapshot.current_bar
                payload.append(snapshot)
            if payload:
                self._live.publish(payload)
            self._write_status()

    async def _symbol_watch_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self._settings.symbol_refresh_seconds)
            current = self._load_symbols()
            if current == self._symbols:
                continue
            added = [symbol for symbol in current if symbol not in self._symbols]
            removed = [symbol for symbol in self._symbols if symbol not in current]
            log.info("subscription change: +%s -%s", added or "-", removed or "-")
            self._symbols = current
            if removed:
                self._aggregator.forget(removed)
                for symbol in removed:
                    self._snapshots.pop(symbol, None)
                for key in [k for k in self._prev_closes if k[0] in removed]:
                    self._prev_closes.pop(key, None)
                self._live.drop(removed)
            for symbol in added:
                await self._backfill.request_gap_since_last_bar(symbol)
            # Force the stream loop to rebuild its subscription.
            self._resubscribe.set()

    async def _backfill_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(5.0)
            if self._backfill.pending_count == 0:
                continue
            results = await self._backfill.drain()
            total = sum(result.bars_written for result in results)
            if total:
                log.info("backfill wrote %d bar(s)", total)

    async def _maintenance_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(3600.0)
            self._prev_closes.clear()
            if self._settings.tick_retention_days > 0:
                cutoff = datetime.now(tz=UTC) - timedelta(
                    days=self._settings.tick_retention_days
                )
                deleted = self._repo.prune_ticks(cutoff)
                if deleted:
                    log.info("pruned %d tick row(s) older than %s", deleted, cutoff.date())

    # ----------------------------------------------------------------- helpers
    def _load_symbols(self) -> list[str]:
        symbols = self._repo.watched_symbols()
        limit = self._settings.max_symbols
        if len(symbols) > limit:
            log.warning(
                "watchlist has %d symbols but max_symbols=%d; subscribing to the first %d",
                len(symbols),
                limit,
                limit,
            )
            symbols = symbols[:limit]
        return symbols

    async def _queue_startup_backfill(self) -> None:
        for symbol in self._symbols:
            await self._backfill.request_gap_since_last_bar(symbol)

    async def _queue_gap_backfill(self, since: datetime) -> None:
        now = datetime.now(tz=UTC)
        if (now - since) < timedelta(seconds=self._settings.backfill_min_gap_seconds):
            return
        for symbol in self._symbols:
            await self._backfill.request_gap_since_last_bar(symbol, now)

    def _write_status(self) -> None:
        budget = self._budget.snapshot()
        self._status.bytes_received_today = self._budget.bytes_today()
        self._status.bytes_received_month = budget.bytes_month
        self._status.rest_calls_hour = budget.calls_hour
        self._status.rest_calls_day = budget.calls_day
        self._status.subscribed_symbols = list(self._symbols)
        self._status.updated_at = datetime.now(tz=UTC)
        self._live.write_status(self._status)


def _with_jitter(delay: float, ratio: float) -> float:
    if ratio <= 0:
        return delay
    spread = delay * ratio
    return max(0.1, delay + random.uniform(-spread, spread))
