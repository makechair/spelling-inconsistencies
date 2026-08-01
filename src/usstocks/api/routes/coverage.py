"""Inventory of the market-data history accumulated for each ticker."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends

from ...config import Settings
from ...db.repository import Repository
from ..deps import get_repository, get_settings_dep
from ..schemas import CoverageOut

router = APIRouter(prefix="/api/coverage", tags=["coverage"])


def _daily_range(path: Path) -> tuple[date, date, int] | None:
    """Read only the date column; daily files are small and updated in place."""
    import pyarrow.parquet as pq

    table = pq.read_table(path, columns=["date"])
    dates = [value for value in table.column("date").to_pylist() if value is not None]
    if not dates:
        return None
    return min(dates), max(dates), len(set(dates))


def build_coverage(repository: Repository, corpus_root: Path) -> list[CoverageOut]:
    combined: dict[str, dict[str, object]] = {}
    for row in repository.bar_coverage():
        symbol = str(row["symbol"])
        first = datetime.fromisoformat(str(row["first_timestamp"])).date()
        last = datetime.fromisoformat(str(row["last_timestamp"])).date()
        combined[symbol] = {
            "first_date": first,
            "last_date": last,
            "minute_bars": int(row["bar_count"]),
            "daily_bars": 0,
        }

    for path in sorted((corpus_root / "daily").glob("symbol=*/part.parquet")):
        symbol = path.parent.name.removeprefix("symbol=").upper()
        daily = _daily_range(path)
        if daily is None:
            continue
        first, last, count = daily
        entry = combined.setdefault(
            symbol,
            {"first_date": first, "last_date": last, "minute_bars": 0, "daily_bars": 0},
        )
        entry["first_date"] = min(first, entry["first_date"])  # type: ignore[type-var]
        entry["last_date"] = max(last, entry["last_date"])  # type: ignore[type-var]
        entry["daily_bars"] = count

    items = [
        CoverageOut(symbol=symbol, **values)  # type: ignore[arg-type]
        for symbol, values in combined.items()
    ]
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
