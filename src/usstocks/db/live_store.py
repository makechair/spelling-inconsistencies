"""Live state shared between the collector and the API.

The spec separates the two processes (11.1) but never says how the API learns
the current price. A separate small SQLite file is the cheapest mechanism that
adds no daemon and no dependency: the collector writes, the API polls at the
SSE cadence. Keeping it out of the durable database means per-tick churn never
takes the write lock that bar upserts need, and the file can sit on tmpfs.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..models import Bar, CollectorStatus, LiveSnapshot, Session
from .connection import ConnectionPool, transaction


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class LiveStore:
    def __init__(
        self,
        db_path: Path | str,
        *,
        busy_timeout_ms: int = 5_000,
        read_only: bool = False,
    ) -> None:
        self._pool = ConnectionPool(
            db_path, busy_timeout_ms=busy_timeout_ms, read_only=read_only
        )

    @property
    def connection(self):
        return self._pool.get()

    def close(self) -> None:
        self._pool.close()

    def __enter__(self) -> LiveStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ----------------------------------------------------------- write side
    def publish(self, snapshots: list[LiveSnapshot]) -> None:
        if not snapshots:
            return
        now = datetime.now(tz=UTC).isoformat()
        rows = []
        for snapshot in snapshots:
            bar = snapshot.current_bar
            rows.append(
                (
                    snapshot.symbol,
                    snapshot.last_price,
                    snapshot.last_trade_at.isoformat() if snapshot.last_trade_at else None,
                    snapshot.session.value,
                    snapshot.previous_close,
                    json.dumps(bar.as_row()) if bar else None,
                    snapshot.source,
                    now,
                )
            )
        with transaction(self.connection) as conn:
            conn.executemany(
                """
                INSERT INTO live_state
                    (symbol, last_price, last_trade_utc, session, prev_close,
                     bar_json, source, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (symbol) DO UPDATE SET
                    last_price     = excluded.last_price,
                    last_trade_utc = excluded.last_trade_utc,
                    session        = excluded.session,
                    prev_close     = excluded.prev_close,
                    bar_json       = excluded.bar_json,
                    source         = excluded.source,
                    updated_at     = excluded.updated_at
                """,
                rows,
            )

    def drop(self, symbols: list[str]) -> None:
        if not symbols:
            return
        with transaction(self.connection) as conn:
            conn.executemany(
                "DELETE FROM live_state WHERE symbol = ?", [(s,) for s in symbols]
            )

    def write_status(self, status: CollectorStatus) -> None:
        payload = {
            "source": status.source,
            "connected": status.connected,
            "connected_since": _iso(status.connected_since),
            "last_message_at": _iso(status.last_message_at),
            "last_trade_at": _iso(status.last_trade_at),
            "subscribed_symbols": status.subscribed_symbols,
            "reconnect_count": status.reconnect_count,
            "last_error": status.last_error,
            "bytes_received_today": status.bytes_received_today,
            "bytes_received_month": status.bytes_received_month,
            "rest_calls_hour": status.rest_calls_hour,
            "rest_calls_day": status.rest_calls_day,
        }
        now = datetime.now(tz=UTC).isoformat()
        with transaction(self.connection) as conn:
            conn.execute(
                "INSERT INTO collector_status (id, status_json, updated_at)"
                " VALUES (1, ?, ?)"
                " ON CONFLICT (id) DO UPDATE SET"
                " status_json = excluded.status_json, updated_at = excluded.updated_at",
                (json.dumps(payload), now),
            )

    # ------------------------------------------------------------ read side
    def read(self, symbols: list[str] | None = None) -> dict[str, LiveSnapshot]:
        if symbols:
            placeholders = ", ".join("?" for _ in symbols)
            cursor = self.connection.execute(
                f"SELECT * FROM live_state WHERE symbol IN ({placeholders})",
                [s.upper() for s in symbols],
            )
        else:
            cursor = self.connection.execute("SELECT * FROM live_state")

        result: dict[str, LiveSnapshot] = {}
        for row in cursor:
            bar = None
            if row["bar_json"]:
                raw = json.loads(row["bar_json"])
                bar = Bar(
                    symbol=raw["symbol"],
                    timestamp=datetime.fromisoformat(raw["timestamp_utc"]),
                    session=Session(raw["session"]),
                    open=raw["open"],
                    high=raw["high"],
                    low=raw["low"],
                    close=raw["close"],
                    volume=raw["volume"],
                    vwap=raw.get("vwap"),
                    trade_count=raw.get("trade_count"),
                    source=raw.get("source", "unknown"),
                    is_final=bool(raw.get("is_final")),
                    received_at=_dt(raw.get("received_at")),
                )
            result[row["symbol"]] = LiveSnapshot(
                symbol=row["symbol"],
                last_price=row["last_price"],
                last_trade_at=_dt(row["last_trade_utc"]),
                session=Session(row["session"]) if row["session"] else Session.CLOSED,
                current_bar=bar,
                previous_close=row["prev_close"],
                source=row["source"] or "unknown",
                updated_at=_dt(row["updated_at"]),
            )
        return result

    def read_status(self) -> tuple[dict[str, Any] | None, datetime | None]:
        row = self.connection.execute(
            "SELECT status_json, updated_at FROM collector_status WHERE id = 1"
        ).fetchone()
        if not row:
            return None, None
        return json.loads(row["status_json"]), _dt(row["updated_at"])


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
