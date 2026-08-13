"""The user's company lists, and the EDINET company search that feeds them.

These are the only write endpoints in the API. Everything else here serves
files that the corpus jobs prepared; this one owns a small store of its own,
so it validates hard and writes atomically.

Adding a company does not fetch anything. The EDINET jobs run on their own
timers and read the same store, so a company added now appears in the table
once the next archive-and-extract pass has been through. The page says so
rather than implying the figures are on their way in seconds.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ... import watchlists
from ...config import Settings
from ..deps import get_settings_dep

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["watchlists"])


def _load(settings: Settings) -> dict[str, Any]:
    try:
        return watchlists.load(settings.watchlists_path)
    except watchlists.WatchlistError as exc:
        # A damaged store is not something the user can fix from the page, and
        # silently starting fresh would destroy the lists it still holds.
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _save(settings: Settings, store: dict[str, Any]) -> None:
    try:
        watchlists.save(settings.watchlists_path, store)
    except OSError as exc:
        raise HTTPException(
            status_code=503, detail=f"cannot write the watchlist store: {exc.strerror}"
        ) from exc


def _guard(action):
    try:
        return action()
    except watchlists.WatchlistError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _payload(store: dict[str, Any]) -> dict[str, Any]:
    return {
        "max_lists": watchlists.MAX_LISTS,
        "lists": store["lists"],
        "companies": store.get("companies", []),
    }


@router.get("/watchlists")
def read(settings: Settings = Depends(get_settings_dep)) -> dict[str, Any]:
    return _payload(_load(settings))


@router.post("/watchlists", status_code=201)
def create(
    name: str = Body(..., embed=True),
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, Any]:
    store = _load(settings)
    entry = _guard(lambda: watchlists.create_list(store, name))
    _save(settings, store)
    return entry


@router.patch("/watchlists/{list_id}")
def rename(
    list_id: str,
    name: str = Body(..., embed=True),
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, Any]:
    store = _load(settings)
    entry = _guard(lambda: watchlists.rename_list(store, list_id, name))
    _save(settings, store)
    return entry


@router.delete("/watchlists/{list_id}", status_code=204)
def remove(list_id: str, settings: Settings = Depends(get_settings_dep)) -> None:
    store = _load(settings)
    _guard(lambda: watchlists.delete_list(store, list_id))
    _save(settings, store)


@router.post("/watchlists/{list_id}/symbols")
def add_symbol(
    list_id: str,
    symbol: str = Body(..., embed=True),
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, Any]:
    store = _load(settings)
    entry = _guard(lambda: watchlists.add_symbol(store, list_id, symbol))
    _save(settings, store)
    return entry


@router.delete("/watchlists/{list_id}/symbols/{symbol}")
def drop_symbol(
    list_id: str,
    symbol: str,
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, Any]:
    store = _load(settings)
    entry = _guard(lambda: watchlists.remove_symbol(store, list_id, symbol))
    _save(settings, store)
    return entry


@router.put("/watchlists/{list_id}/holdings")
def set_holding(
    list_id: str,
    symbol: str = Body(..., embed=True),
    quantity: float | None = Body(None, embed=True),
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, Any]:
    """How many shares the list holds of this symbol; null clears it."""
    store = _load(settings)
    entry = _guard(lambda: watchlists.set_quantity(store, list_id, symbol, quantity))
    _save(settings, store)
    return entry


def _summary(settings: Settings) -> dict[str, Any]:
    path = settings.corpus_local_dir / "fundamentals" / "summary.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _analysis(settings: Settings) -> dict[str, Any]:
    path = settings.corpus_local_dir / "analysis" / "latest" / "report.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


@router.get("/risk/{list_id}")
def portfolio_risk(
    list_id: str,
    confidence: float = Query(0.95),
    horizon_days: int = Query(1, ge=1, le=20),
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, Any]:
    """Value at risk for the quantities held on one list.

    Assembled from files the jobs already wrote: volatilities and pairwise
    correlations from the event study, the latest close from the fundamentals
    summary. No DuckDB, no Parquet -- the arithmetic is small enough to do in
    this process and nothing else here would be.
    """
    from ...risk import portfolio_var

    store = _load(settings)
    entry = _guard(lambda: watchlists.get_list(store, list_id))
    quantities = {
        symbol: amount
        for symbol, amount in (entry.get("quantities") or {}).items()
        if amount
    }
    if not quantities:
        raise HTTPException(
            status_code=409,
            detail="this list has no quantities; set a share count on a symbol first",
        )

    summary = _summary(settings)
    prices = {
        str(row.get("symbol")): row.get("price")
        for row in summary.get("symbols", [])
        if row.get("price") is not None
    }
    analysis = _analysis(settings)
    volatilities = {
        str(row.get("symbol")): row.get("annualised_volatility")
        for row in analysis.get("symbol_risk_profile", [])
    }
    worst_days = {
        str(row.get("symbol")): row.get("worst_day")
        for row in analysis.get("symbol_risk_profile", [])
    }
    correlations = {
        (str(row.get("symbol")), str(row.get("peer"))): row.get("correlation")
        for row in analysis.get("symbol_correlations", [])
    }

    positions: dict[str, float] = {}
    unpriced: list[str] = []
    for symbol, amount in quantities.items():
        price = prices.get(symbol)
        if price is None:
            unpriced.append(symbol)
            continue
        positions[symbol] = float(amount) * float(price)
    try:
        result = portfolio_var(
            positions,
            volatilities,
            correlations,
            confidence=confidence,
            horizon_days=horizon_days,
            worst_days=worst_days,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result | {
        "list_id": list_id,
        "list_name": entry.get("name"),
        "priced_through": summary.get("generated_at"),
        # Held, sized, and with no price to value it by. Named rather than
        # dropped: an unvalued position is not an absent one.
        "unpriced_symbols": sorted(unpriced),
    }


def _companies_path(settings: Settings) -> Path:
    return settings.corpus_local_dir / "edinet_codes" / "companies.json"


_cache: dict[str, Any] = {"key": None, "companies": []}


def _known_companies(settings: Settings) -> list[dict[str, Any]]:
    """The mirrored EDINET code list, re-read only when the file changes.

    Several thousand entries is small enough to hold, and large enough that
    parsing it per keystroke would be felt.
    """
    path = _companies_path(settings)
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return []
    # Keyed on the path as well as the timestamp: the path is fixed in
    # production, but a cache that ignores it silently answers one directory's
    # question with another's.
    key = (str(path), stamp)
    if _cache["key"] != key:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        found = payload.get("companies")
        _cache["companies"] = found if isinstance(found, list) else []
        _cache["key"] = key
    return _cache["companies"]


@router.get("/companies/search")
def search(
    q: str = Query(..., min_length=1, max_length=64),
    limit: int = Query(20, ge=1, le=50),
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, Any]:
    from ...corpus.edinet_codes import search as search_companies

    known = _known_companies(settings)
    if not known:
        raise HTTPException(
            status_code=503,
            detail="the EDINET company list has not been mirrored yet",
        )
    store = _load(settings)
    added = {entry.get("code") for entry in store.get("companies", [])}
    return {
        "results": [
            company | {"added": company.get("code") in added}
            for company in search_companies(known, q, limit=limit)
        ]
    }


@router.post("/companies", status_code=201)
def register(
    code: str = Body(..., embed=True),
    subsector: str = Body(watchlists.UNCLASSIFIED, embed=True),
    list_id: str | None = Body(None, embed=True),
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, Any]:
    """Put a Japanese filer into the universe the EDINET jobs walk.

    The name is taken from the mirrored code list rather than from the caller:
    it has to match what EDINET knows, or the table would carry a label the
    filings never used.
    """
    wanted = str(code).strip().upper()
    known = _known_companies(settings)
    match = next((row for row in known if str(row.get("code")) == wanted), None)
    if match is None:
        raise HTTPException(
            status_code=404, detail=f"{wanted} is not a listed EDINET filer"
        )
    store = _load(settings)
    entry = _guard(
        lambda: watchlists.register_company(
            store, code=wanted, name=str(match.get("name") or ""), subsector=subsector
        )
    )
    if list_id:
        _guard(lambda: watchlists.add_symbol(store, list_id, wanted))
    _save(settings, store)
    log.info("registered EDINET company %s (%s)", wanted, entry["name"])
    return entry


@router.delete("/companies/{code}", status_code=204)
def unregister(code: str, settings: Settings = Depends(get_settings_dep)) -> None:
    store = _load(settings)
    _guard(lambda: watchlists.unregister_company(store, code))
    _save(settings, store)
