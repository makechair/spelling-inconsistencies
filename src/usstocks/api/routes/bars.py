"""Historical bar queries (spec 3.2, 4.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from ...config import Settings
from ...db.repository import Repository
from ..deps import get_repository, get_settings_dep
from ..schemas import BarOut, BarsResponse

router = APIRouter(prefix="/api/bars", tags=["bars"])


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
    bars = repository.get_bars(
        symbol,
        start_dt,
        end_dt,
        limit=limit + 1,
        sources=[source] if source else None,
    )
    truncated = len(bars) > limit
    if truncated:
        bars = bars[:limit]

    return BarsResponse(
        symbol=symbol.upper(),
        count=len(bars),
        truncated=truncated,
        bars=[BarOut.from_bar(bar) for bar in bars],
    )
