"""Mirror SEC EDGAR XBRL company facts to partitioned Parquet.

Phase A of docs/earnings-spec.md: fetch and normalize only. Metrics are
computed downstream from what this writes.

Three properties of the source drive the shape of this module:

* EDGAR costs nothing and draws on no Tiingo quota, so this never competes
  with the daily bars for the provider allowance. It has its own politeness
  limit instead (SEC asks for <= 10 requests/second and a real User-Agent).
* Filers tag the same concept differently, so every concept carries an
  ordered list of candidates and the row records which one was used. A
  concept whose candidates all miss is absent, never estimated.
* Foreign private issuers file 20-F annually and have no quarterly figures.
  Those periods are simply not present; nothing here fabricates them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import time
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings, get_settings
from ..logging_setup import configure_logging
from .daily import (
    CorpusError,
    ProviderResponseError,
    Uploader,
    aws_upload,
    corpus_s3_root,
    in_closed_market_window,
    load_universe,
    save_state,
)

log = logging.getLogger(__name__)

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# SEC asks for no more than ten requests a second. One every 150ms leaves
# headroom and still finishes the whole universe in under a minute.
REQUEST_INTERVAL_SECONDS = 0.15

# Ordered candidates per concept: the first tag present wins. Ordering is by
# specificity, so a filer that reports both a narrow and a broad revenue tag
# contributes the narrow one rather than whichever the dict happened to yield.
CONCEPTS: dict[str, tuple[str, ...]] = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ),
    "cost_of_revenue": (
        "CostOfGoodsAndServicesSold",
        "CostOfRevenue",
        "CostOfGoodsSold",
    ),
    "gross_profit": ("GrossProfit",),
    "operating_income": ("OperatingIncomeLoss",),
    "net_income": (
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
    ),
    "research_development": (
        "ResearchAndDevelopmentExpense",
        "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
    ),
    "inventory": ("InventoryNet", "InventoryGross"),
    "assets": ("Assets",),
    "liabilities": ("Liabilities",),
    "equity": (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ),
    "cash_and_equivalents": (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",),
    "capex": (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    ),
    "shares_outstanding": (
        "CommonStockSharesOutstanding",
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingBasic",
    ),
}

# Balance-sheet concepts are instantaneous; the rest cover a period. XBRL marks
# this by whether a fact carries a start date, and mixing the two would compare
# a stock against a flow.
INSTANT_CONCEPTS = frozenset(
    {
        "inventory",
        "assets",
        "liabilities",
        "equity",
        "cash_and_equivalents",
        "shares_outstanding",
    }
)

_CIK_PATTERN = re.compile(r"^\d{1,10}$")


def fundamentals_s3_root(settings: Settings) -> str:
    return f"{corpus_s3_root(settings)}/fundamentals"


def load_fundamentals_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "symbols": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorpusError(f"cannot read fundamentals state: {type(exc).__name__}") from exc
    if payload.get("version") != 1 or not isinstance(payload.get("symbols"), dict):
        raise CorpusError("unsupported fundamentals state format")
    return payload


def _headers(settings: Settings) -> dict[str, str]:
    contact = (settings.sec_user_agent or "").strip()
    if not contact:
        raise CorpusError(
            "USSTOCKS_SEC_USER_AGENT is not set; SEC requires a contact in the User-Agent"
        )
    return {"User-Agent": contact, "Accept-Encoding": "gzip, deflate"}


def _get_json(client: httpx.Client, url: str, settings: Settings, *, what: str) -> Any:
    try:
        response = client.get(url, headers=_headers(settings))
    except httpx.HTTPError as exc:
        raise CorpusError(f"SEC request failed for {what}: {type(exc).__name__}") from exc
    if response.status_code >= 400:
        raise ProviderResponseError(what, response.status_code)
    try:
        return response.json()
    except ValueError as exc:
        raise CorpusError(f"SEC returned invalid JSON for {what}") from exc


def fetch_cik_map(client: httpx.Client, settings: Settings) -> dict[str, str]:
    """symbol -> zero-padded CIK, from SEC's own ticker directory.

    Shipping a static copy would rot silently as tickers change hands, and the
    file is one request for the whole universe.
    """
    payload = _get_json(client, TICKER_MAP_URL, settings, what="company tickers")
    if not isinstance(payload, dict):
        raise CorpusError("SEC ticker directory has an unexpected shape")
    mapping: dict[str, str] = {}
    for entry in payload.values():
        if not isinstance(entry, dict):
            continue
        ticker = str(entry.get("ticker", "")).strip().upper()
        cik = str(entry.get("cik_str", "")).strip()
        if ticker and _CIK_PATTERN.fullmatch(cik):
            mapping.setdefault(ticker, cik.zfill(10))
    if not mapping:
        raise CorpusError("SEC ticker directory contained no usable entries")
    return mapping


def fetch_company_facts(client: httpx.Client, settings: Settings, cik: str) -> dict[str, Any]:
    payload = _get_json(
        client, COMPANY_FACTS_URL.format(cik=cik), settings, what=f"CIK {cik}"
    )
    if not isinstance(payload, dict):
        raise CorpusError(f"SEC company facts for CIK {cik} has an unexpected shape")
    return payload


def _parse_iso_date(value: object) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _select_facts(
    facts: dict[str, Any], candidates: Sequence[str]
) -> tuple[str, list[dict]] | None:
    """First candidate tag that carries USD (or share) facts, with its rows."""
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    dei = facts.get("facts", {}).get("dei", {})
    for tag in candidates:
        entry = us_gaap.get(tag) or dei.get(tag)
        if not isinstance(entry, dict):
            continue
        units = entry.get("units")
        if not isinstance(units, dict):
            continue
        for unit_name in ("USD", "shares", "USD/shares"):
            rows = units.get(unit_name)
            if isinstance(rows, list) and rows:
                return tag, rows
    return None


def normalize_company_facts(symbol: str, payload: dict[str, Any]) -> list[dict[str, object]]:
    """One row per (concept, filed period), carrying the tag that supplied it.

    Keyed on the accession number so a later amendment lands as its own row
    rather than silently replacing the figure a report may already cite.
    """
    rows: list[dict[str, object]] = []
    entity = str(payload.get("entityName") or "")
    cik = str(payload.get("cik") or "")

    for concept, candidates in CONCEPTS.items():
        selected = _select_facts(payload, candidates)
        if selected is None:
            continue
        tag, facts = selected
        instant = concept in INSTANT_CONCEPTS
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            value = fact.get("val")
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            end = _parse_iso_date(fact.get("end"))
            if end is None:
                continue
            start = _parse_iso_date(fact.get("start"))
            # A duration concept without a start, or an instant one with a
            # start, is not the measurement this concept means.
            if instant and start is not None:
                continue
            if not instant and start is None:
                continue
            form = str(fact.get("form") or "")
            fiscal_period = str(fact.get("fp") or "")
            rows.append(
                {
                    "symbol": symbol,
                    "cik": cik,
                    "entity_name": entity,
                    "concept": concept,
                    "xbrl_tag": tag,
                    "period_start": start,
                    "period_end": end,
                    "fiscal_year": int(fact["fy"]) if isinstance(fact.get("fy"), int) else None,
                    "fiscal_period": fiscal_period,
                    "form": form,
                    "accession": str(fact.get("accn") or ""),
                    "filed": _parse_iso_date(fact.get("filed")),
                    "value": float(value),
                }
            )
    return rows


def _parquet_modules() -> tuple[Any, Any]:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise CorpusError("fundamentals corpus requires the 'parquet' package extra") from exc
    return pa, pq


def _schema() -> Any:
    pa, _ = _parquet_modules()
    return pa.schema(
        [
            ("symbol", pa.string()),
            ("cik", pa.string()),
            ("entity_name", pa.string()),
            ("concept", pa.string()),
            ("xbrl_tag", pa.string()),
            ("period_start", pa.date32()),
            ("period_end", pa.date32()),
            ("fiscal_year", pa.int64()),
            ("fiscal_period", pa.string()),
            ("form", pa.string()),
            ("accession", pa.string()),
            ("filed", pa.date32()),
            ("value", pa.float64()),
        ]
    )


def write_fundamentals_parquet(path: Path, rows: Sequence[dict[str, object]]) -> str:
    pa, pq = _parquet_modules()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    ordered = sorted(
        rows,
        key=lambda row: (
            str(row["concept"]),
            str(row["period_end"]),
            str(row["accession"]),
        ),
    )
    pq.write_table(
        pa.Table.from_pylist(ordered, schema=_schema()), temporary, compression="zstd"
    )
    digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
    temporary.replace(path)
    return digest


def run(
    settings: Settings,
    *,
    force: bool = False,
    symbols: Sequence[str] | None = None,
    uploader: Uploader = aws_upload,
    client: httpx.Client | None = None,
    now: datetime | None = None,
    sleeper: Any = time.sleep,
) -> int:
    moment = (now or datetime.now(tz=UTC)).astimezone(UTC)
    if not force and not in_closed_market_window(moment):
        log.info("outside 09:00-17:00 JST; fundamentals run skipped")
        return 0

    entries = load_universe(settings.corpus_universe_path)
    known = {entry.symbol for entry in entries}
    if symbols is None:
        targets = [entry.symbol for entry in entries]
    else:
        requested = [value.strip().upper() for value in symbols if value.strip()]
        unknown = [value for value in requested if value not in known]
        if unknown:
            raise CorpusError(f"target symbol is not in universe: {', '.join(unknown)}")
        seen: set[str] = set()
        targets = [value for value in requested if not (value in seen or seen.add(value))]
    if not targets:
        raise CorpusError("no target symbols")
    limit = settings.fundamentals_max_symbols_per_run
    if len(targets) > limit:
        log.info("limiting run to %d of %d symbol(s)", limit, len(targets))
        targets = targets[:limit]

    local_root = settings.corpus_local_dir
    state_path = local_root / "fundamentals-state.json"
    state = load_fundamentals_state(state_path)
    symbol_state = state["symbols"]
    s3_root = fundamentals_s3_root(settings)

    own_client = client is None
    http_client = client or httpx.Client(
        timeout=settings.sec_timeout_seconds, follow_redirects=True
    )
    processed = 0
    try:
        cik_map = fetch_cik_map(http_client, settings)
        for symbol in targets:
            cik = cik_map.get(symbol)
            if cik is None:
                # Foreign issuers without an SEC listing simply are not here.
                # That is a fact about the universe, not a failure of the run.
                log.warning("no SEC CIK for %s; skipping", symbol)
                symbol_state.setdefault(symbol, {})["cik"] = None
                continue
            sleeper(REQUEST_INTERVAL_SECONDS)
            payload = fetch_company_facts(http_client, settings, cik)
            rows = normalize_company_facts(symbol, payload)
            if not rows:
                log.warning("no usable XBRL facts for %s (CIK %s)", symbol, cik)
            path = local_root / "fundamentals" / f"symbol={symbol}" / "part.parquet"
            digest = write_fundamentals_parquet(path, rows)
            entry_state = symbol_state.setdefault(symbol, {})
            entry_state["cik"] = cik
            entry_state["row_count"] = len(rows)
            if entry_state.get("digest") != digest:
                uploader(path, f"{s3_root}/symbol={symbol}/part.parquet")
                entry_state["digest"] = digest
            entry_state["last_success_utc"] = datetime.now(tz=UTC).isoformat()
            save_state(state_path, state)
            processed += 1
    except ProviderResponseError as exc:
        # Stop the rest of the run: SEC answering with an error once will
        # usually answer the same way to the next request, and hammering it is
        # exactly what gets an agent blocked.
        save_state(state_path, state)
        log.error("SEC responded with an error; stopping after %d symbol(s): %s", processed, exc)
        return 2
    finally:
        if own_client:
            http_client.close()

    state["last_success_utc"] = datetime.now(tz=UTC).isoformat()
    save_state(state_path, state)
    log.info("fundamentals run complete: %d/%d symbol(s)", processed, len(targets))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="ignore the JST safety window")
    parser.add_argument(
        "--symbols",
        help="comma-separated universe symbols; start with one to prove the response shape",
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        return run(
            settings,
            force=args.force,
            symbols=(args.symbols.split(",") if args.symbols is not None else None),
        )
    except CorpusError as exc:
        log.error("%s", exc)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
