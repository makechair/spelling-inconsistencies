"""Historical bar queries (spec 3.2, 4.3)."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query

from ...config import Settings
from ...db.repository import Repository
from ...models import Bar, Session
from ..deps import get_repository, get_settings_dep
from ..schemas import BarOut, BarsResponse

router = APIRouter(prefix="/api/bars", tags=["bars"])
_MARKET_ZONE = ZoneInfo("America/New_York")


@lru_cache(maxsize=64)
def _read_daily_rows(path_text: str, modified_ns: int) -> tuple[dict[str, object], ...]:
    """Read a small per-symbol parquet, cached by its immutable mtime."""
    del modified_ns
    import pyarrow.parquet as pq

    table = pq.read_table(
        path_text,
        columns=[
            "date",
            "adjOpen",
            "adjHigh",
            "adjLow",
            "adjClose",
            "adjVolume",
        ],
    )
    return tuple(table.to_pylist())


def _daily_corpus_bars(
    path: Path,
    symbol: str,
    start: datetime,
    end: datetime,
) -> list[Bar]:
    if not path.exists():
        return []
    received_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    rows = _read_daily_rows(str(path), path.stat().st_mtime_ns)
    bars: list[Bar] = []
    for row in rows:
        day = row["date"]
        timestamp = datetime.combine(day, time(16), tzinfo=UTC)  # type: ignore[arg-type]
        if not start <= timestamp < end:
            continue
        bars.append(
            Bar(
                symbol=symbol.upper(),
                timestamp=timestamp,
                session=Session.REGULAR,
                open=float(row["adjOpen"]),
                high=float(row["adjHigh"]),
                low=float(row["adjLow"]),
                close=float(row["adjClose"]),
                volume=int(round(float(row["adjVolume"]))),
                source="tiingo_daily",
                is_final=True,
                received_at=received_at,
            )
        )
    return bars


def _parse_time(value: str | None, field: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid {field}: {value}") from exc
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@router.get("/{symbol}", response_model=BarsResponse)
def get_bars(
    symbol: str,
    start: str | None = Query(None, description="ISO-8601; defaults to 1 day back"),
    end: str | None = Query(None, description="ISO-8601; defaults to now"),
    days: int | None = Query(None, ge=1, le=3650, description="Shorthand for start"),
    source: str | None = Query(None, description="Restrict to one provider"),
    interval: Literal["1m", "5m", "15m", "30m", "1h", "1d"] = Query(
        "1m", description="Chart aggregation interval"
    ),
    session: Literal["all", "regular"] = Query(
        "all", description="Include extended hours or regular session only"
    ),
    repository: Repository = Depends(get_repository),
    settings: Settings = Depends(get_settings_dep),
) -> BarsResponse:
    """One bar per minute.

    When two providers hold the same minute, the configured source priority
    decides which one is returned, and each bar carries the source that won
    (docs/spec-review.md B-1). Pass ``source`` to inspect a single provider.
    """
    end_dt = _parse_time(end, "end") or datetime.now(tz=UTC)
    start_dt = _parse_time(start, "start")
    if start_dt is None:
        span = timedelta(days=days) if days else timedelta(days=1)
        start_dt = end_dt - span
    if start_dt >= end_dt:
        raise HTTPException(status_code=400, detail="start must be before end")

    # Fetching a symbol's bars is the clearest signal that it is the one being
    # looked at. The collector reads this to aim a REST allowance it can no
    # longer spread across every symbol (spec-review A-6).
    repository.mark_viewed([symbol])

    limit = settings.max_bars_per_request
    query_kwargs = {
        "sources": [source] if source else None,
        "sessions": ["regular"] if session == "regular" else None,
        "newest_first": True,
    }
    if interval == "1m":
        bars = repository.get_bars(
            symbol, start_dt, end_dt, limit=limit + 1, **query_kwargs
        )
    elif interval == "1d":
        corpus_path = (
            settings.corpus_local_dir
            / "daily"
            / f"symbol={symbol.upper()}"
            / "part.parquet"
        )
        corpus_bars = (
            []
            if source and source not in {"tiingo", "tiingo_daily"}
            else _daily_corpus_bars(corpus_path, symbol, start_dt, end_dt)
        )
        market_bars = repository.get_aggregated_bars(
            symbol,
            start_dt,
            end_dt,
            interval="1d",
            limit=None,
            **query_kwargs,
        )
        # The daily corpus supplies the long history; market.db replaces its
        # newest dates so today's completed/in-progress intraday data appears.
        by_day = {
            bar.timestamp.astimezone(_MARKET_ZONE).date(): bar for bar in corpus_bars
        }
        by_day.update(
            {
                bar.timestamp.astimezone(_MARKET_ZONE).date(): bar
                for bar in market_bars
            }
        )
        bars = sorted(by_day.values(), key=lambda bar: bar.timestamp)[-(limit + 1) :]
    else:
        bars = repository.get_aggregated_bars(
            symbol,
            start_dt,
            end_dt,
            interval=interval,
            limit=limit + 1,
            **query_kwargs,
        )
    truncated = len(bars) > limit
    if truncated:
        # The repository returns chronological order even though the SQL limit
        # was applied from the newest edge. Drop the one oldest probe row.
        bars = bars[-limit:]

    # Reported against the primary source, which is the one the collector is
    # actually polling. A standby provider's state would say nothing about
    # whether the data on screen is being kept up to date.
    checked = repository.last_checked(symbol, settings.primary_source)

    return BarsResponse(
        symbol=symbol.upper(),
        count=len(bars),
        truncated=truncated,
        checked_at=checked.isoformat() if checked else None,
        bars=[BarOut.from_bar(bar) for bar in bars],
    )
