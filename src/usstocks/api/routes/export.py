"""Bulk export for analysis (spec 3.4).

CSV streams row by row and is always available. Parquet needs pyarrow, which
costs a few hundred MB of resident memory to build a table — real money on a
1 GB instance (docs/spec-review.md D-1) — so it is an optional extra and the
endpoint says so plainly rather than dying with an ImportError.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse

from ...db.repository import Repository
from ..deps import get_repository

router = APIRouter(prefix="/api/export", tags=["export"])

COLUMNS = [
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
]


def _range(start: str | None, end: str | None, days: int | None) -> tuple[datetime, datetime]:
    def parse(value: str | None, field: str) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid {field}") from exc
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    end_dt = parse(end, "end") or datetime.now(tz=UTC)
    start_dt = parse(start, "start") or (end_dt - timedelta(days=days or 30))
    if start_dt >= end_dt:
        raise HTTPException(status_code=400, detail="start must be before end")
    return start_dt, end_dt


@router.get("/csv")
def export_csv(
    symbols: str = Query(..., description="Comma-separated tickers"),
    start: str | None = None,
    end: str | None = None,
    days: int | None = Query(None, ge=1, le=3650),
    repository: Repository = Depends(get_repository),
) -> StreamingResponse:
    tickers = [item.strip().upper() for item in symbols.split(",") if item.strip()]
    if not tickers:
        raise HTTPException(status_code=400, detail="at least one symbol is required")
    start_dt, end_dt = _range(start, end, days)

    def rows():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(COLUMNS)
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)

        for bar in repository.iter_bars(tickers, start_dt, end_dt):
            writer.writerow(
                [
                    bar.symbol,
                    bar.timestamp.isoformat(),
                    bar.session.value,
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.volume,
                    bar.vwap,
                    bar.trade_count,
                    bar.source,
                    int(bar.is_final),
                ]
            )
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    filename = f"bars_1m_{start_dt:%Y%m%d}_{end_dt:%Y%m%d}.csv"
    return StreamingResponse(
        rows(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/parquet")
def export_parquet(
    symbols: str = Query(...),
    start: str | None = None,
    end: str | None = None,
    days: int | None = Query(None, ge=1, le=3650),
    repository: Repository = Depends(get_repository),
) -> Response:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise HTTPException(
            status_code=501,
            detail=(
                "parquet export requires the optional 'parquet' extra "
                "(pip install '.[parquet]'); CSV export is always available"
            ),
        ) from exc

    tickers = [item.strip().upper() for item in symbols.split(",") if item.strip()]
    if not tickers:
        raise HTTPException(status_code=400, detail="at least one symbol is required")
    start_dt, end_dt = _range(start, end, days)

    columns: dict[str, list] = {name: [] for name in COLUMNS}
    for bar in repository.iter_bars(tickers, start_dt, end_dt):
        columns["symbol"].append(bar.symbol)
        columns["timestamp_utc"].append(bar.timestamp)
        columns["session"].append(bar.session.value)
        columns["open"].append(bar.open)
        columns["high"].append(bar.high)
        columns["low"].append(bar.low)
        columns["close"].append(bar.close)
        columns["volume"].append(bar.volume)
        columns["vwap"].append(bar.vwap)
        columns["trade_count"].append(bar.trade_count)
        columns["source"].append(bar.source)
        columns["is_final"].append(bar.is_final)

    table = pa.table(columns)
    sink = io.BytesIO()
    pq.write_table(table, sink, compression="zstd")
    filename = f"bars_1m_{start_dt:%Y%m%d}_{end_dt:%Y%m%d}.parquet"
    return Response(
        content=sink.getvalue(),
        media_type="application/vnd.apache.parquet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
