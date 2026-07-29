"""Symbol search and watchlist management (spec 3.1)."""

from __future__ import annotations

import logging
import time
from collections import OrderedDict

from fastapi import APIRouter, Depends, HTTPException, Query

from ...adapters.base import AdapterError
from ...db.repository import Repository
from ...models import SymbolInfo
from ..deps import AppState, get_repository, get_state
from ..schemas import SymbolOut, SymbolUpsert

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/symbols", tags=["symbols"])

# Provider search results, keyed by the normalised query.
#
# Typing a ticker produces a request per debounced keystroke -- "M", "MU" -- and
# the same prefixes recur every time the box is used. Against a 50 calls/hour
# allowance shared with backfill, that is the difference between looking up a
# few symbols and spending the hour's budget on autocomplete.
#
# Bounded and short-lived: the ticker universe barely moves, but a stale entry
# should not outlive a session.
_SEARCH_CACHE_TTL = 600.0
_SEARCH_CACHE_MAX = 256
_search_cache: OrderedDict[str, tuple[float, list[SymbolOut]]] = OrderedDict()


def _cached_search(key: str) -> list[SymbolOut] | None:
    entry = _search_cache.get(key)
    if entry is None:
        return None
    stored_at, results = entry
    if time.monotonic() - stored_at > _SEARCH_CACHE_TTL:
        del _search_cache[key]
        return None
    _search_cache.move_to_end(key)
    return results


async def _spend_search_budget(state: AppState) -> bool:
    """Take one token for a provider search, or report that none is available.

    Search used to call the provider directly, off the bucket entirely. The
    calls still counted against the plan, so the hourly allowance drained
    invisibly and /api/health reported fewer calls than had actually been made
    -- the meter that exists to answer exactly this question (spec-review A-2).
    """
    if state.rest_budget is None:
        return True
    return await state.rest_budget.acquire(1, timeout=0.0)


def _store_search(key: str, results: list[SymbolOut]) -> None:
    _search_cache[key] = (time.monotonic(), results)
    _search_cache.move_to_end(key)
    while len(_search_cache) > _SEARCH_CACHE_MAX:
        _search_cache.popitem(last=False)


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

    Answered locally wherever possible. Three tiers, cheapest first:

    1. symbols already in the watchlist,
    2. the imported provider catalog (usstocks-catalog.timer),
    3. the provider's search endpoint.

    Only the third spends REST budget, and with the catalog present it is
    reached only for something the catalog does not know -- a listing newer
    than the last import. Before the catalog existed every distinct query went
    to the provider, so autocomplete competed with backfill for 50 calls an
    hour (docs/spec-review.md A-2).
    """
    needle = q.strip().upper()
    known = state.repository.list_symbols()
    local = [
        info
        for info in known
        if needle in info.symbol or (info.name and needle in info.name.upper())
    ]

    if len(local) < limit:
        seen_local = {info.symbol for info in local}
        local += [
            info
            for info in state.repository.search_catalog(needle, limit=limit)
            if info.symbol not in seen_local
        ]

    remote: list[SymbolOut] = []
    if len(local) < limit and state.adapter is not None:
        cache_key = f"{needle}:{limit}"
        cached = _cached_search(cache_key)
        if cached is not None:
            results = []
            remote = [entry for entry in cached if entry.symbol not in {i.symbol for i in local}]
        elif not await _spend_search_budget(state):
            # Degrade to local matches rather than failing. Backfill is the
            # better use of a nearly-empty allowance: a search the user can
            # retry costs them a moment, a gap never filled is lost history.
            log.warning("symbol search skipped: REST budget exhausted")
            results = []
        else:
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
        if results:
            _store_search(cache_key, remote)

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
