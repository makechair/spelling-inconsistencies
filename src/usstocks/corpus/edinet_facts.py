"""Extract concept rows from the archived EDINET XBRL.

JP-A2 of docs/earnings-spec.md. Reads the zips JP-A stored and writes the same
fact schema the EDGAR loader produces, so the metrics job treats Japanese and
US filers identically.

EDINET differs from EDGAR's companyfacts in ways that decide the parsing:

* It is a raw XBRL instance, not parsed JSON, so contexts and units have to be
  resolved before any figure means anything.
* **Contexts carry dimensions.** A filer reports consolidated revenue and also
  revenue per segment and per non-consolidated entity, all tagged with the same
  concept. Taking every fact would sum a company's sales several times over,
  so only dimensionless contexts are read.
* Three taxonomies are in use -- jppfs (Japanese GAAP), jpigp and ifrs -- and a
  filer picks one. Concepts are matched on local name across all of them.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import zipfile
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from ..config import Settings, get_settings
from ..logging_setup import configure_logging
from .daily import CorpusError, Uploader, aws_upload, corpus_s3_root, save_state
from .edinet import load_universe_jp
from .fundamentals import INSTANT_CONCEPTS, write_fundamentals_parquet

log = logging.getLogger(__name__)

# Ordered per concept, Japanese GAAP first then IFRS. A filer uses one
# taxonomy, so the order only breaks ties where a name is shared.
JP_CONCEPTS: dict[str, tuple[str, ...]] = {
    "revenue": (
        "NetSales",
        "NetSalesSummaryOfBusinessResults",
        "OperatingRevenue1",
        "RevenueIFRS",
        "RevenueFromContractsWithCustomersIFRS",
        "TotalRevenuesIFRS",
    ),
    "cost_of_revenue": ("CostOfSales", "CostOfSalesIFRS"),
    "gross_profit": ("GrossProfit", "GrossProfitIFRS"),
    "operating_income": (
        "OperatingIncome",
        "OperatingProfitLossIFRS",
        "OperatingIncomeIFRS",
    ),
    "net_income": (
        "ProfitLossAttributableToOwnersOfParent",
        "ProfitLoss",
        "NetIncomeLossIFRS",
        "ProfitLossAttributableToOwnersOfParentIFRS",
    ),
    "research_development": (
        "ResearchAndDevelopmentExpenses",
        "ResearchAndDevelopmentExpensesSGA",
    ),
    "inventory": (
        "Inventories",
        "MerchandiseAndFinishedGoods",
        "InventoriesIFRS",
    ),
    "assets": ("Assets", "TotalAssetsIFRS", "AssetsIFRS"),
    "liabilities": ("Liabilities", "LiabilitiesIFRS"),
    "equity": (
        "NetAssets",
        "EquityAttributableToOwnersOfParentIFRS",
        "EquityIFRS",
    ),
    "cash_and_equivalents": (
        "CashAndDeposits",
        "CashAndCashEquivalents",
        "CashAndCashEquivalentsIFRS",
    ),
    "operating_cash_flow": (
        "NetCashProvidedByUsedInOperatingActivities",
        "CashFlowsFromUsedInOperatingActivitiesIFRS",
    ),
    "capex": (
        "PurchaseOfPropertyPlantAndEquipment",
        "PurchaseOfPropertyPlantAndEquipmentIFRS",
    ),
    "shares_outstanding": (
        "TotalNumberOfIssuedSharesSummaryOfBusinessResults",
        "NumberOfIssuedSharesAsOfFiscalYearEnd",
    ),
}

# Only the taxonomies that carry financial statements. jpcrp holds cover-page
# and narrative items, which are not figures this corpus can use.
FINANCIAL_NAMESPACE_HINTS = ("jppfs", "jpigp", "ifrs")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _namespace(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def _parse_date(text: str | None) -> date | None:
    if not text:
        return None
    try:
        return date.fromisoformat(text.strip()[:10])
    except ValueError:
        return None


def parse_contexts(root: ElementTree.Element) -> dict[str, dict[str, Any]]:
    """Dimensionless contexts only.

    A context with an explicitMember is a segment, a non-consolidated entity or
    a prior-year restatement axis. Their facts carry the same concept names as
    the consolidated ones, so reading them would multiply every total.
    """
    contexts: dict[str, dict[str, Any]] = {}
    for context in root.iter():
        if _local_name(context.tag) != "context":
            continue
        identifier = context.get("id")
        if not identifier:
            continue
        if any(_local_name(node.tag) == "explicitMember" for node in context.iter()):
            continue
        start = end = None
        for node in context.iter():
            name = _local_name(node.tag)
            if name == "startDate":
                start = _parse_date(node.text)
            elif name == "endDate":
                end = _parse_date(node.text)
            elif name == "instant":
                end = _parse_date(node.text)
        if end is None:
            continue
        contexts[identifier] = {"period_start": start, "period_end": end}
    return contexts


def parse_units(root: ElementTree.Element) -> dict[str, str]:
    units: dict[str, str] = {}
    for unit in root.iter():
        if _local_name(unit.tag) != "unit":
            continue
        identifier = unit.get("id")
        if not identifier:
            continue
        measures = [
            (node.text or "").strip().rsplit(":", 1)[-1]
            for node in unit.iter()
            if _local_name(node.tag) == "measure"
        ]
        # A divide unit (JPY per share) has two measures and is not a level.
        if len(measures) == 1 and measures[0]:
            units[identifier] = measures[0]
    return units


def instance_documents(archive: Path) -> Iterator[tuple[str, bytes]]:
    """The public instance documents inside an EDINET zip.

    AuditDoc holds the auditor's report and PublicDoc the filing itself; only
    the latter carries the statements.
    """
    with zipfile.ZipFile(archive) as bundle:
        for name in bundle.namelist():
            if name.endswith(".xbrl") and "PublicDoc" in name:
                yield name, bundle.read(name)


def normalize_instance(
    code: str, payload: bytes, *, doc_id: str, form: str, filed: date | None
) -> list[dict[str, object]]:
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise CorpusError(f"{doc_id}: XBRL instance is not parseable") from exc

    contexts = parse_contexts(root)
    units = parse_units(root)
    wanted = {
        name: (concept, priority)
        for concept, candidates in JP_CONCEPTS.items()
        for priority, name in enumerate(candidates)
    }

    best: dict[tuple[str, Any, Any], tuple[int, dict[str, object]]] = {}
    for element in root.iter():
        namespace = _namespace(element.tag)
        if not any(hint in namespace for hint in FINANCIAL_NAMESPACE_HINTS):
            continue
        match = wanted.get(_local_name(element.tag))
        if match is None:
            continue
        concept, priority = match
        context = contexts.get(element.get("contextRef") or "")
        if context is None:
            continue
        unit = units.get(element.get("unitRef") or "")
        if unit is None:
            continue
        try:
            value = float((element.text or "").strip())
        except (TypeError, ValueError):
            continue
        instant = concept in INSTANT_CONCEPTS
        # The same distinction EDGAR draws with a start date: a balance has no
        # duration, and comparing one against a flow is meaningless.
        if instant and context["period_start"] is not None:
            continue
        if not instant and context["period_start"] is None:
            continue
        key = (concept, context["period_start"], context["period_end"])
        existing = best.get(key)
        if existing is not None and existing[0] <= priority:
            continue
        best[key] = (
            priority,
            {
                "symbol": code,
                "cik": "",
                "entity_name": "",
                "concept": concept,
                "xbrl_tag": _local_name(element.tag),
                "unit": unit,
                "period_start": context["period_start"],
                "period_end": context["period_end"],
                "fiscal_year": None,
                "fiscal_period": "",
                # The metrics job filters on filing type; EDINET's periodic
                # reports map onto the annual/semi/quarterly forms it keeps.
                "form": form,
                "accession": doc_id,
                "filed": filed,
                "value": value,
            },
        )
    return [row for _, row in best.values()]


# EDINET document type codes to a form label the metrics filter accepts.
DOC_TYPE_FORMS = {
    "120": "20-F", "130": "20-F/A",
    "140": "10-Q", "150": "10-Q/A",
    "160": "10-Q", "170": "10-Q/A",
}


def _index_rows(local_root: Path) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    path = local_root / "edinet_index" / "part.parquet"
    if not path.exists():
        raise CorpusError(f"EDINET index is missing: {path}; run the EDINET job first")
    return pq.read_table(path).to_pylist()


def run(
    settings: Settings,
    *,
    uploader: Uploader = aws_upload,
    codes: Sequence[str] | None = None,
) -> int:
    entries = load_universe_jp(settings.universe_jp_path)
    by_code = {entry.code: entry for entry in entries}
    local_root = settings.corpus_local_dir
    index = _index_rows(local_root)

    wanted = {value.strip() for value in codes} if codes else None
    grouped: dict[str, list[dict[str, object]]] = {}
    skipped: dict[str, str] = {}
    for entry in index:
        code = str(entry.get("code") or "")
        if code not in by_code or (wanted is not None and code not in wanted):
            continue
        doc_id = str(entry.get("doc_id") or "")
        archive = local_root / "edinet" / f"code={code}" / doc_id / "xbrl.zip"
        if not archive.exists():
            skipped[doc_id] = "no archive"
            continue
        form = DOC_TYPE_FORMS.get(str(entry.get("doc_type_code") or ""))
        if form is None:
            skipped[doc_id] = "unmapped document type"
            continue
        filed = _parse_date(str(entry.get("submit_datetime") or "")[:10])
        found = 0
        for name, payload in instance_documents(archive):
            try:
                rows = normalize_instance(
                    code, payload, doc_id=doc_id, form=form, filed=filed
                )
            except CorpusError as exc:
                log.warning("%s (%s): %s", doc_id, name, exc)
                continue
            grouped.setdefault(code, []).extend(rows)
            found += len(rows)
        if found == 0:
            skipped[doc_id] = "no usable facts"

    if not grouped:
        raise CorpusError("no EDINET facts extracted; check the archived documents")

    s3_root = f"{corpus_s3_root(settings)}/fundamentals"
    state_path = local_root / "edinet-facts-state.json"
    state: dict[str, Any] = {"version": 1, "symbols": {}}
    for code, rows in sorted(grouped.items()):
        path = local_root / "fundamentals" / f"symbol={code}" / "part.parquet"
        digest = write_fundamentals_parquet(path, rows)
        uploader(path, f"{s3_root}/symbol={code}/part.parquet")
        state["symbols"][code] = {"rows": len(rows), "digest": digest}

    # The Japanese subsectors live beside the US ones so the metrics job can
    # label both without knowing which market a symbol came from.
    write_sectors_jp(local_root / "universe" / "sectors_jp.parquet", entries, uploader,
                     f"{corpus_s3_root(settings)}/universe/sectors_jp.parquet")

    state["last_success_utc"] = datetime.now(tz=UTC).isoformat()
    state["skipped"] = skipped
    save_state(state_path, state)
    log.info(
        "EDINET facts complete: %d symbol(s), %d row(s), %d document(s) skipped",
        len(grouped),
        sum(len(rows) for rows in grouped.values()),
        len(skipped),
    )
    return 0


def write_sectors_jp(path: Path, entries: Sequence[Any], uploader: Uploader, destination: str):
    import pyarrow as pa
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    pq.write_table(
        pa.Table.from_pylist(
            [{"symbol": entry.code, "subsector": entry.subsector} for entry in entries],
            schema=pa.schema([("symbol", pa.string()), ("subsector", pa.string())]),
        ),
        temporary,
        compression="zstd",
    )
    temporary.replace(path)
    uploader(path, destination)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def describe_archive(archive: Path) -> str:
    """Which concepts a filing actually contains, for when none matched.

    EDINET is unreachable from development, so a bare "no usable facts" costs
    a round trip to whoever can run it.
    """
    names: dict[str, int] = {}
    for _, payload in instance_documents(archive):
        try:
            root = ElementTree.fromstring(payload)
        except ElementTree.ParseError:
            continue
        for element in root.iter():
            namespace = _namespace(element.tag)
            if any(hint in namespace for hint in FINANCIAL_NAMESPACE_HINTS):
                name = _local_name(element.tag)
                names[name] = names.get(name, 0) + 1
    if not names:
        return "no financial-taxonomy elements found"
    ranked = sorted(names.items(), key=lambda item: item[1], reverse=True)[:25]
    return ", ".join(f"{name}({count})" for name, count in ranked)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codes", help="comma-separated securities codes")
    parser.add_argument(
        "--describe",
        help="print the concepts one archived document contains, then exit",
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        if args.describe:
            matches = sorted(
                (settings.corpus_local_dir / "edinet").glob(f"code=*/{args.describe}/xbrl.zip")
            )
            if not matches:
                raise CorpusError(f"no archived XBRL for document {args.describe}")
            print(describe_archive(matches[0]))
            return 0
        return run(settings, codes=args.codes.split(",") if args.codes else None)
    except CorpusError as exc:
        log.error("%s", exc)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
