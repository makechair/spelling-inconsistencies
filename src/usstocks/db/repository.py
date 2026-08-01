"""Data access for the durable database.

This module owns the rules the spec left undefined (docs/spec-review.md):

* B-1  Two sources may hold the same (symbol, minute). Reads resolve one row
       per minute using the configured source priority.
* B-5  Upsert precedence: an in-progress bar never overwrites a final one from
       the same source, REST-confirmed finals do overwrite live-aggregated
       finals, and different sources never overwrite each other.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from ..models import Bar, Session, SymbolInfo
from .connection import ConnectionPool, transaction

BAR_COLUMNS = (
    "symbol",
    "timestamp_utc",
    "session",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "vwap",
    "trade_count",
    "source",
    "is_final",
    "received_at",
)

# Upsert precedence, expressed in SQL so it is atomic and applies equally to
# the collector and to backfill.
#
#   excluded.is_final = 1 AND bars_1m.is_final = 0  -> always take the new row
#   excluded.is_final = 1 AND bars_1m.is_final = 1  -> take it (a correction or
#                                                      the provider's own
#                                                      authoritative history)
#   excluded.is_final = 0 AND bars_1m.is_final = 1  -> keep what we have
#   excluded.is_final = 0 AND bars_1m.is_final = 0  -> take the newer snapshot
_UPSERT_BAR = f"""
INSERT INTO bars_1m ({", ".join(BAR_COLUMNS)})
VALUES ({", ".join(":" + column for column in BAR_COLUMNS)})
ON CONFLICT (symbol, timestamp_utc, source) DO UPDATE SET
    session     = excluded.session,
    open        = excluded.open,
    high        = excluded.high,
    low         = excluded.low,
    close       = excluded.close,
    volume      = excluded.volume,
    vwap        = excluded.vwap,
    trade_count = excluded.trade_count,
    is_final    = excluded.is_final,
    received_at = excluded.received_at
WHERE excluded.is_final = 1 OR bars_1m.is_final = 0
"""


def _row_to_bar(row: sqlite3.Row) -> Bar:
    return Bar(
        symbol=row["symbol"],
        timestamp=datetime.fromisoformat(row["timestamp_utc"]),
        session=Session(row["session"]),
        open=row["open"],
        high=row["high"],
        low=row["low"],
        close=row["close"],
        volume=row["volume"],
        vwap=row["vwap"],
        trade_count=row["trade_count"],
        source=row["source"],
        is_final=bool(row["is_final"]),
        received_at=datetime.fromisoformat(row["received_at"]) if row["received_at"] else None,
    )


class Repository:
    """Synchronous SQLite access. Callers in async code wrap it in a thread."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        busy_timeout_ms: int = 5_000,
        source_priority: Sequence[str] = ("tiingo", "alpaca"),
        read_only: bool = False,
    ) -> None:
        self.db_path = Path(db_path)
        self.source_priority = list(source_priority)
        self._pool = ConnectionPool(
            db_path, busy_timeout_ms=busy_timeout_ms, read_only=read_only
        )

    def close(self) -> None:
        self._pool.close()

    def __enter__(self) -> Repository:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        """This thread's connection (see ConnectionPool)."""
        return self._pool.get()

    # ------------------------------------------------------------------ bars
    def upsert_bars(self, bars: Iterable[Bar]) -> int:
        rows = [bar.as_row() for bar in bars]
        if not rows:
            return 0
        with transaction(self.connection) as conn:
            cursor = conn.executemany(_UPSERT_BAR, rows)
            written = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        return written

    def upsert_bar(self, bar: Bar) -> int:
        return self.upsert_bars([bar])

    def _priority_case(self, alias: str = "b") -> str:
        """ORDER BY fragment turning the configured priority into a rank."""
        if not self.source_priority:
            return "0"
        whens = " ".join(
            f"WHEN '{source}' THEN {index}"
            for index, source in enumerate(self.source_priority)
        )
        return f"CASE {alias}.source {whens} ELSE {len(self.source_priority)} END"

    def get_bars(
        self,
        symbol: str,
        start: datetime | None = None,
        end: datetime | None = None,
        *,
        limit: int | None = None,
        sources: Sequence[str] | None = None,
        sessions: Sequence[str] | None = None,
        newest_first: bool = False,
    ) -> list[Bar]:
        """One bar per minute, resolved by source priority (spec-review B-1).

        ``newest_first`` applies the SQL limit from the newest edge, then
        restores chronological order for chart consumers.
        """
        clauses = ["b.symbol = :symbol"]
        params: dict[str, object] = {"symbol": symbol.upper()}
        if start is not None:
            clauses.append("b.timestamp_utc >= :start")
            params["start"] = start.astimezone(UTC).isoformat()
        if end is not None:
            clauses.append("b.timestamp_utc < :end")
            params["end"] = end.astimezone(UTC).isoformat()
        if sources:
            placeholders = ", ".join(f":src{i}" for i in range(len(sources)))
            clauses.append(f"b.source IN ({placeholders})")
            for index, source in enumerate(sources):
                params[f"src{index}"] = source
        if sessions:
            placeholders = ", ".join(f":session{i}" for i in range(len(sessions)))
            clauses.append(f"b.session IN ({placeholders})")
            for index, session in enumerate(sessions):
                params[f"session{index}"] = session

        # Rank inside each minute, keep rank 1. A window function is cheaper
        # here than post-filtering in Python for multi-thousand-bar ranges.
        sql = f"""
        WITH ranked AS (
            SELECT b.*, ROW_NUMBER() OVER (
                PARTITION BY b.timestamp_utc
                ORDER BY {self._priority_case()} ASC, b.is_final DESC, b.received_at DESC
            ) AS rank
            FROM bars_1m AS b
            WHERE {" AND ".join(clauses)}
        )
        SELECT * FROM ranked WHERE rank = 1
        ORDER BY timestamp_utc {"DESC" if newest_first else "ASC"}
        """
        if limit is not None:
            sql += " LIMIT :limit"
            params["limit"] = int(limit)
        bars = [_row_to_bar(row) for row in self.connection.execute(sql, params)]
        return list(reversed(bars)) if newest_first else bars

    def get_aggregated_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        interval: str,
        limit: int | None = None,
        sources: Sequence[str] | None = None,
        sessions: Sequence[str] | None = None,
        newest_first: bool = False,
    ) -> list[Bar]:
        """Aggregate resolved minute bars before applying the response limit.

        Doing this in SQLite is important for 1Y charts: returning tens of
        thousands of minute rows only to collapse them in the browser wastes
        both Lightsail memory and network bandwidth.
        """
        interval_seconds = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600}
        if interval not in {*interval_seconds, "1d"}:
            raise ValueError(f"unsupported aggregate interval: {interval}")
        clauses = [
            "b.symbol = :symbol",
            "b.timestamp_utc >= :start",
            "b.timestamp_utc < :end",
        ]
        params: dict[str, object] = {
            "symbol": symbol.upper(),
            "start": start.astimezone(UTC).isoformat(),
            "end": end.astimezone(UTC).isoformat(),
        }
        if sources:
            placeholders = ", ".join(f":src{i}" for i in range(len(sources)))
            clauses.append(f"b.source IN ({placeholders})")
            for index, source in enumerate(sources):
                params[f"src{index}"] = source
        if sessions:
            placeholders = ", ".join(f":session{i}" for i in range(len(sessions)))
            clauses.append(f"b.session IN ({placeholders})")
            for index, session in enumerate(sessions):
                params[f"session{index}"] = session

        bucket = (
            "CAST(CAST(strftime('%s', timestamp_utc) AS INTEGER) / "
            f"{interval_seconds[interval]} AS INTEGER)"
            if interval != "1d"
            # -5h keeps US pre/regular/post-market prints on their market date.
            else "date(timestamp_utc, '-5 hours')"
        )
        sql = f"""
        WITH ranked AS (
            SELECT b.*, ROW_NUMBER() OVER (
                PARTITION BY b.timestamp_utc
                ORDER BY {self._priority_case()} ASC, b.is_final DESC, b.received_at DESC
            ) AS source_rank
            FROM bars_1m AS b
            WHERE {" AND ".join(clauses)}
        ), resolved AS (
            SELECT * FROM ranked WHERE source_rank = 1
        ), bucketed AS (
            SELECT resolved.*, {bucket} AS time_bucket
            FROM resolved
        ), sequenced AS (
            SELECT bucketed.*,
                ROW_NUMBER() OVER (
                    PARTITION BY symbol, time_bucket ORDER BY timestamp_utc ASC
                ) AS first_rank,
                ROW_NUMBER() OVER (
                    PARTITION BY symbol, time_bucket ORDER BY timestamp_utc DESC
                ) AS last_rank
            FROM bucketed
        )
        SELECT
            symbol,
            MIN(timestamp_utc) AS timestamp_utc,
            CASE WHEN COUNT(DISTINCT session) = 1 THEN MIN(session) ELSE 'regular' END AS session,
            MAX(CASE WHEN first_rank = 1 THEN open END) AS open,
            MAX(high) AS high,
            MIN(low) AS low,
            MAX(CASE WHEN last_rank = 1 THEN close END) AS close,
            SUM(volume) AS volume,
            CASE WHEN SUM(volume) > 0
                THEN SUM(COALESCE(vwap, close) * volume) / SUM(volume)
                ELSE AVG(COALESCE(vwap, close))
            END AS vwap,
            SUM(trade_count) AS trade_count,
            CASE WHEN COUNT(DISTINCT source) = 1 THEN MIN(source) ELSE 'mixed' END AS source,
            MIN(is_final) AS is_final,
            MAX(received_at) AS received_at
        FROM sequenced
        GROUP BY symbol, time_bucket
        ORDER BY timestamp_utc {"DESC" if newest_first else "ASC"}
        """
        if limit is not None:
            sql += " LIMIT :limit"
            params["limit"] = int(limit)
        bars = [_row_to_bar(row) for row in self.connection.execute(sql, params)]
        return list(reversed(bars)) if newest_first else bars

    def bar_coverage(self) -> list[dict[str, object]]:
        """Return the stored minute-bar range for every symbol."""
        rows = self.connection.execute(
            """
            SELECT symbol,
                   MIN(timestamp_utc) AS first_timestamp,
                   MAX(timestamp_utc) AS last_timestamp,
                   COUNT(DISTINCT timestamp_utc) AS bar_count
            FROM bars_1m
            GROUP BY symbol
            ORDER BY symbol
            """
        )
        return [dict(row) for row in rows]

    def iter_bars(
        self,
        symbols: Sequence[str],
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Iterator[Bar]:
        """Streaming read for export (spec 3.4). Never materialises the range."""
        for symbol in symbols:
            clauses = ["b.symbol = :symbol"]
            params: dict[str, object] = {"symbol": symbol.upper()}
            if start is not None:
                clauses.append("b.timestamp_utc >= :start")
                params["start"] = start.astimezone(UTC).isoformat()
            if end is not None:
                clauses.append("b.timestamp_utc < :end")
                params["end"] = end.astimezone(UTC).isoformat()
            sql = f"""
            WITH ranked AS (
                SELECT b.*, ROW_NUMBER() OVER (
                    PARTITION BY b.timestamp_utc
                    ORDER BY {self._priority_case()} ASC, b.is_final DESC, b.received_at DESC
                ) AS rank
                FROM bars_1m AS b
                WHERE {" AND ".join(clauses)}
            )
            SELECT * FROM ranked WHERE rank = 1 ORDER BY timestamp_utc ASC
            """
            cursor = self.connection.execute(sql, params)
            while True:
                rows = cursor.fetchmany(2_000)
                if not rows:
                    break
                for row in rows:
                    yield _row_to_bar(row)

    def last_bar_timestamp(self, symbol: str, source: str) -> datetime | None:
        row = self.connection.execute(
            "SELECT MAX(timestamp_utc) AS ts FROM bars_1m WHERE symbol = ? AND source = ?",
            (symbol.upper(), source),
        ).fetchone()
        return datetime.fromisoformat(row["ts"]) if row and row["ts"] else None

    def previous_close(self, symbol: str, before: datetime) -> float | None:
        """Last regular-session close strictly before ``before``'s ET date.

        Used for the change / change% display required by spec 3.2.
        """
        row = self.connection.execute(
            f"""
            SELECT b.close FROM bars_1m AS b
            WHERE b.symbol = ? AND b.session = 'regular' AND b.timestamp_utc < ?
            ORDER BY b.timestamp_utc DESC, {self._priority_case()} ASC
            LIMIT 1
            """,
            (symbol.upper(), before.astimezone(UTC).isoformat()),
        ).fetchone()
        return row["close"] if row else None

    def count_bars(self, symbol: str | None = None) -> int:
        if symbol:
            row = self.connection.execute(
                "SELECT COUNT(*) AS n FROM bars_1m WHERE symbol = ?", (symbol.upper(),)
            ).fetchone()
        else:
            row = self.connection.execute("SELECT COUNT(*) AS n FROM bars_1m").fetchone()
        return int(row["n"])

    # --------------------------------------------------------------- symbols
    def upsert_symbol(self, info: SymbolInfo) -> None:
        with transaction(self.connection) as conn:
            conn.execute(
                """
                INSERT INTO symbols
                    (symbol, name, exchange, asset_type, is_watched, is_held,
                     supported, note, updated_at)
                VALUES (:symbol, :name, :exchange, :asset_type, :is_watched,
                        :is_held, :supported, :note, :updated_at)
                ON CONFLICT (symbol) DO UPDATE SET
                    name       = COALESCE(excluded.name, symbols.name),
                    exchange   = COALESCE(excluded.exchange, symbols.exchange),
                    asset_type = COALESCE(excluded.asset_type, symbols.asset_type),
                    is_watched = excluded.is_watched,
                    is_held    = excluded.is_held,
                    supported  = COALESCE(excluded.supported, symbols.supported),
                    note       = COALESCE(excluded.note, symbols.note),
                    updated_at = excluded.updated_at
                """,
                {
                    "symbol": info.symbol.upper(),
                    "name": info.name,
                    "exchange": info.exchange,
                    "asset_type": info.asset_type,
                    "is_watched": int(info.is_watched),
                    "is_held": int(info.is_held),
                    "supported": None if info.supported is None else int(info.supported),
                    "note": info.note,
                    "updated_at": datetime.now(tz=UTC).isoformat(),
                },
            )

    def set_supported(self, symbol: str, supported: bool, note: str | None = None) -> None:
        with transaction(self.connection) as conn:
            conn.execute(
                "UPDATE symbols SET supported = ?, note = ?,"
                " updated_at = ? WHERE symbol = ?",
                (int(supported), note, datetime.now(tz=UTC).isoformat(), symbol.upper()),
            )

    def remove_symbol(self, symbol: str) -> None:
        """Unwatch, keeping history. Deleting bars would throw away the very
        data the system exists to accumulate (spec 2.1)."""
        with transaction(self.connection) as conn:
            conn.execute(
                "UPDATE symbols SET is_watched = 0, is_held = 0, updated_at = ?"
                " WHERE symbol = ?",
                (datetime.now(tz=UTC).isoformat(), symbol.upper()),
            )

    def get_symbol(self, symbol: str) -> SymbolInfo | None:
        row = self.connection.execute(
            "SELECT * FROM symbols WHERE symbol = ?", (symbol.upper(),)
        ).fetchone()
        return self._row_to_symbol(row) if row else None

    def list_symbols(self, *, watched_only: bool = False) -> list[SymbolInfo]:
        sql = "SELECT * FROM symbols"
        if watched_only:
            sql += " WHERE is_watched = 1 OR is_held = 1"
        sql += " ORDER BY symbol"
        return [self._row_to_symbol(row) for row in self.connection.execute(sql)]

    def mark_viewed(self, symbols: list[str], now: datetime | None = None) -> None:
        """Record that these symbols are being looked at right now.

        The collector reads this to decide where to spend a REST allowance that
        can no longer be spread across everything (spec-review A-6). Written by
        the API, which is the only process that knows what a browser asked for.
        """
        if not symbols:
            return
        stamp = (now or datetime.now(tz=UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.connection as conn:
            conn.executemany(
                "UPDATE symbols SET last_viewed_at = ? WHERE symbol = ?",
                [(stamp, symbol.upper()) for symbol in symbols],
            )

    def recently_viewed(self, within: timedelta, now: datetime | None = None) -> list[str]:
        """Watched symbols looked at inside `within`, most recent first."""
        cutoff = ((now or datetime.now(tz=UTC)) - within).strftime("%Y-%m-%dT%H:%M:%SZ")
        return [
            row["symbol"]
            for row in self.connection.execute(
                "SELECT symbol FROM symbols"
                " WHERE (is_watched = 1 OR is_held = 1)"
                "   AND last_viewed_at IS NOT NULL AND last_viewed_at >= ?"
                " ORDER BY last_viewed_at DESC",
                (cutoff,),
            )
        ]

    # ------------------------------------------------------------- catalog
    def upsert_catalog(self, rows: list[tuple]) -> int:
        """Write one import chunk. Rows are (symbol, ..., refreshed_at)."""
        with self.connection as conn:
            conn.executemany(
                "INSERT INTO symbol_catalog"
                " (symbol, name, exchange, asset_type, price_currency,"
                "  start_date, end_date, refreshed_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(symbol) DO UPDATE SET"
                "   name = excluded.name,"
                "   exchange = excluded.exchange,"
                "   asset_type = excluded.asset_type,"
                "   price_currency = excluded.price_currency,"
                "   start_date = excluded.start_date,"
                "   end_date = excluded.end_date,"
                "   refreshed_at = excluded.refreshed_at",
                rows,
            )
        return len(rows)

    def prune_catalog(self, stamp: str) -> int:
        """Drop entries the latest import did not carry.

        Matched by inequality, not ordering. The stamp has second resolution, so
        `refreshed_at < stamp` silently keeps everything when two imports land
        in the same second -- and "rows this run did not write" is what is meant
        regardless of how the clock compares.
        """
        with self.connection as conn:
            cursor = conn.execute(
                "DELETE FROM symbol_catalog WHERE refreshed_at <> ?", (stamp,)
            )
        return cursor.rowcount

    def catalog_size(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) AS n FROM symbol_catalog").fetchone()
        return int(row["n"]) if row else 0

    def search_catalog(self, needle: str, limit: int = 20) -> list[SymbolInfo]:
        """Local ticker/name search.

        Ordered so an exact ticker wins, then ticker prefixes, then anything
        matching by name -- typing "MU" should not surface MUAIX above Micron.
        """
        needle = needle.strip().upper()
        if not needle:
            return []
        pattern = f"%{needle}%"
        rows = self.connection.execute(
            "SELECT symbol, name, exchange, asset_type FROM symbol_catalog"
            " WHERE symbol LIKE ? OR UPPER(name) LIKE ?"
            " ORDER BY"
            "   CASE WHEN symbol = ? THEN 0"
            "        WHEN symbol LIKE ? THEN 1"
            "        ELSE 2 END,"
            "   LENGTH(symbol), symbol"
            " LIMIT ?",
            (pattern, pattern, needle, f"{needle}%", limit),
        ).fetchall()
        return [
            SymbolInfo(
                symbol=row["symbol"],
                name=row["name"],
                exchange=row["exchange"],
                asset_type=row["asset_type"],
            )
            for row in rows
        ]

    def watched_symbols(self) -> list[str]:
        return [
            row["symbol"]
            for row in self.connection.execute(
                "SELECT symbol FROM symbols WHERE is_watched = 1 OR is_held = 1"
                " ORDER BY symbol"
            )
        ]

    @staticmethod
    def _row_to_symbol(row: sqlite3.Row) -> SymbolInfo:
        return SymbolInfo(
            symbol=row["symbol"],
            name=row["name"],
            exchange=row["exchange"],
            asset_type=row["asset_type"],
            is_watched=bool(row["is_watched"]),
            is_held=bool(row["is_held"]),
            supported=None if row["supported"] is None else bool(row["supported"]),
            note=row["note"],
        )

    # ------------------------------------------------------- collector state
    def record_state(
        self,
        symbol: str,
        source: str,
        *,
        last_bar: datetime | None = None,
        last_trade: datetime | None = None,
        last_backfill: datetime | None = None,
    ) -> None:
        now = datetime.now(tz=UTC).isoformat()
        with transaction(self.connection) as conn:
            conn.execute(
                """
                INSERT INTO collector_state
                    (symbol, source, last_bar_utc, last_trade_utc,
                     last_backfill_utc, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (symbol, source) DO UPDATE SET
                    last_bar_utc      = COALESCE(excluded.last_bar_utc,
                                                 collector_state.last_bar_utc),
                    last_trade_utc    = COALESCE(excluded.last_trade_utc,
                                                 collector_state.last_trade_utc),
                    last_backfill_utc = COALESCE(excluded.last_backfill_utc,
                                                 collector_state.last_backfill_utc),
                    updated_at        = excluded.updated_at
                """,
                (
                    symbol.upper(),
                    source,
                    last_bar.astimezone(UTC).isoformat() if last_bar else None,
                    last_trade.astimezone(UTC).isoformat() if last_trade else None,
                    last_backfill.astimezone(UTC).isoformat() if last_backfill else None,
                    now,
                ),
            )

    def last_checked(self, symbol: str, source: str) -> datetime | None:
        """When a REST fetch for this symbol last completed.

        Distinct from the newest bar, and that distinction is the whole point.
        A fetch that returns nothing still stamps this, so a frozen chart can be
        read two ways: last bar old but this recent means the market produced
        nothing, while both being old means collection itself stopped.
        """
        row = self.connection.execute(
            "SELECT last_backfill_utc FROM collector_state "
            "WHERE symbol = ? AND source = ?",
            (symbol.upper(), source),
        ).fetchone()
        if row is None or not row["last_backfill_utc"]:
            return None
        return datetime.fromisoformat(row["last_backfill_utc"])

    def get_state(self, symbol: str, source: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM collector_state WHERE symbol = ? AND source = ?",
            (symbol.upper(), source),
        ).fetchone()

    # ------------------------------------------------------------- calendar
    def calendar_overrides(self) -> tuple[frozenset[date], frozenset[date]]:
        closed: set[date] = set()
        early: set[date] = set()
        for row in self.connection.execute("SELECT day, kind FROM market_calendar_overrides"):
            day = date.fromisoformat(row["day"])
            (closed if row["kind"] == "closed" else early).add(day)
        return frozenset(closed), frozenset(early)

    # ------------------------------------------------------------ maintenance
    def insert_ticks(self, rows: Sequence[tuple[str, str, float, int | None, str]]) -> None:
        if not rows:
            return
        with transaction(self.connection) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO ticks"
                " (symbol, timestamp_utc, price, size, source) VALUES (?, ?, ?, ?, ?)",
                rows,
            )

    def prune_ticks(self, older_than: datetime) -> int:
        with transaction(self.connection) as conn:
            cursor = conn.execute(
                "DELETE FROM ticks WHERE timestamp_utc < ?",
                (older_than.astimezone(UTC).isoformat(),),
            )
        return cursor.rowcount or 0

    def database_size_bytes(self) -> int:
        row = self.connection.execute(
            "SELECT page_count * page_size AS size FROM pragma_page_count(),"
            " pragma_page_size()"
        ).fetchone()
        return int(row["size"])
