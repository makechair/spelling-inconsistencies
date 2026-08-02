"""Inventory of the market-data history accumulated for each ticker."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends

from ...config import Settings
from ...db.repository import Repository
from ..deps import get_repository, get_settings_dep
from ..schemas import CoverageOut

router = APIRouter(prefix="/api/coverage", tags=["coverage"])


def _daily_dates(path: Path) -> set[date]:
    """Read only the date column; daily files are small and updated in place."""
    import pyarrow.parquet as pq

    table = pq.read_table(path, columns=["date"])
    return {value for value in table.column("date").to_pylist() if value is not None}


def _continuous_range(dates: set[date], *, max_gap_days: int = 10) -> tuple[date, date]:
    """Latest dense range, excluding stale islands separated by a large gap.

    Weekends, exchange holidays and short exceptional closures fit inside ten
    calendar days. A larger gap means the older data cannot form a continuous
    chart with the latest accumulation and must not widen the displayed range.
    """
    ordered = sorted(dates)
    start_index = 0
    for index in range(len(ordered) - 1, 0, -1):
        if (ordered[index] - ordered[index - 1]).days > max_gap_days:
            start_index = index
            break
    return ordered[start_index], ordered[-1]


def build_coverage(repository: Repository, corpus_root: Path) -> list[CoverageOut]:
    date_sets = repository.bar_coverage_dates()
    counts: dict[str, dict[str, int]] = {
        symbol: {"minute_bars": len(dates), "daily_bars": 0}
        for symbol, dates in date_sets.items()
    }
    for row in repository.bar_coverage():
        symbol = str(row["symbol"])
        counts.setdefault(symbol, {"minute_bars": 0, "daily_bars": 0})[
            "minute_bars"
        ] = int(row["bar_count"])

    for path in sorted((corpus_root / "daily").glob("symbol=*/part.parquet")):
        symbol = path.parent.name.removeprefix("symbol=").upper()
        daily_dates = _daily_dates(path)
        if not daily_dates:
            continue
        date_sets.setdefault(symbol, set()).update(daily_dates)
        counts.setdefault(symbol, {"minute_bars": 0, "daily_bars": 0})[
            "daily_bars"
        ] = len(daily_dates)

    items = []
    for symbol, dates in date_sets.items():
        if not dates:
            continue
        first, last = _continuous_range(dates)
        items.append(
            CoverageOut(
                symbol=symbol,
                first_date=first,
                last_date=last,
                **counts[symbol],
            )
        )
    return sorted(
        items,
        key=lambda item: (-(item.last_date - item.first_date).days, item.symbol),
    )


@router.get("", response_model=list[CoverageOut])
def coverage(
    repository: Repository = Depends(get_repository),
    settings: Settings = Depends(get_settings_dep),
) -> list[CoverageOut]:
    return build_coverage(repository, settings.corpus_local_dir)
