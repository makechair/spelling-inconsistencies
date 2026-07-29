"""Symbol search and watchlist management (spec 3.1)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from ...adapters.base import AdapterError
from ...db.repository import Repository
from ...models import SymbolInfo
from ..deps import AppState, get_repository, get_state
from ..schemas import SymbolOut, SymbolUpsert

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/symbols", tags=["symbols"])


@router.get("", response_model=list[SymbolOut])
def list_symbols(
    watched_only: bool = Query(False),
    repository: Repository = Depends(get_repository),
) -> list[SymbolOut]:
    return [
        SymbolOut.from_info(info)
        for info in repository.list_symbols(watched_only=watched_only)
    ]


@router.get("/search", response_model=list[SymbolOut])
async def search_symbols(
    q: str = Query(min_length=1, max_length=64),
    limit: int = Query(20, ge=1, le=50),
    state: AppState = Depends(get_state),
) -> list[SymbolOut]:
    """Ticker or company name search.

    Local matches come first and cost nothing; the provider is only consulted
    when the local list cannot answer, which keeps search off the REST budget
    for symbols already being tracked (docs/spec-review.md A-2).
    """
    needle = q.strip().upper()
    known = state.repository.list_symbols()
    local = [
        info
        for info in known
        if needle in info.symbol or (info.name and needle in info.name.upper())
    ]

    remote: list[SymbolOut] = []
    if len(local) < limit and state.adapter is not None:
        try:
            results = await state.adapter.search_symbols(q, limit=limit)
        except AdapterError as exc:
            log.warning("provider symbol search failed: %s", exc)
            results = []
        # `seen` grows as results are accepted, not just from the local list.
        # The provider's search returns one row per listing, so a symbol quoted
        # on more than one venue arrives twice -- MU comes back as two identical
        # "Micron Technology Inc" entries. Deduplicating only against local
        # matches let those through whenever the symbol was not already tracked,
        # which is exactly when the user is looking it up.
        seen = {info.symbol for info in local}
        for item in results:
            if item["symbol"] in seen:
                continue
            seen.add(item["symbol"])
            remote.append(
                SymbolOut(
                    symbol=item["symbol"],
                    name=item.get("name") or None,
                    exchange=item.get("exchange") or None,
                    asset_type=item.get("asset_type") or None,
                )
            )

    combined = [SymbolOut.from_info(info) for info in local] + remote
    return combined[:limit]


@router.put("/{symbol}", response_model=SymbolOut)
def upsert_symbol(
    symbol: str,
    payload: SymbolUpsert,
    repository: Repository = Depends(get_repository),
) -> SymbolOut:
    """Add or update a watchlist / holdings entry.

    The collector notices the change within ``symbol_refresh_seconds`` and
    adjusts its subscription.
    """
    symbol = symbol.strip().upper()
    if not symbol or not symbol.replace(".", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="invalid ticker symbol")

    repository.upsert_symbol(
        SymbolInfo(
            symbol=symbol,
            name=payload.name,
            exchange=payload.exchange,
            asset_type=payload.asset_type,
            is_watched=payload.is_watched,
            is_held=payload.is_held,
        )
    )
    info = repository.get_symbol(symbol)
    if info is None:  # pragma: no cover - just written
        raise HTTPException(status_code=500, detail="symbol not persisted")
    return SymbolOut.from_info(info)


@router.delete("/{symbol}", response_model=SymbolOut)
def remove_symbol(
    symbol: str,
    repository: Repository = Depends(get_repository),
) -> SymbolOut:
    """Stop tracking a symbol.

    History is deliberately kept: accumulating bars is the point of the system
    (spec 2.1), so unwatching must not delete them.
    """
    symbol = symbol.strip().upper()
    info = repository.get_symbol(symbol)
    if info is None:
        raise HTTPException(status_code=404, detail="unknown symbol")
    repository.remove_symbol(symbol)
    updated = repository.get_symbol(symbol)
    return SymbolOut.from_info(updated or info)
