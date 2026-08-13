"""Compute comparable metrics from the archived XBRL facts.

Phase B of docs/earnings-spec.md. Reads the Parquet the EDGAR loader writes,
applies the three rules production data forced (periodic filings only, newest
filing per period, ratios only between matching units), and writes one row per
symbol and reporting period.

Nothing here is an estimate. A concept the filer never tagged stays null all
the way to the report, because a zero would read as "spent nothing" rather
than "did not say".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from ..config import Settings, get_settings
from ..logging_setup import configure_logging
from .daily import CorpusError, Uploader, aws_upload, corpus_s3_root, load_state, save_state
from .event_study import analysis_exchange_s3_root

log = logging.getLogger(__name__)


def _modules() -> tuple[Any, Any]:
    try:
        import duckdb
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise CorpusError(
            "fundamentals metrics require the 'parquet' package extra"
        ) from exc
    return duckdb, pq


# Which market each universe file describes. The metrics are market-agnostic
# -- a margin is a margin -- but the rows still have to carry the label,
# because revenue in JPY sorted against revenue in USD produces a ranking that
# means nothing. sectors.parquet has no suffix because it predates the split.
SECTOR_MARKETS = {"sectors.parquet": "US", "sectors_jp.parquet": "JP"}


def discover_inputs(local_root: Path) -> tuple[list[Path], list[Path]]:
    facts = sorted((local_root / "fundamentals").glob("symbol=*/part.parquet"))
    if not facts:
        raise CorpusError("no fundamentals Parquet found; run the EDGAR loader first")
    # Both universes, when the Japanese one has been built.
    sectors = [
        path
        for path in (
            local_root / "universe" / "sectors.parquet",
            local_root / "universe" / "sectors_jp.parquet",
        )
        if path.exists()
    ]
    if not sectors:
        raise CorpusError(
            f"universe sectors Parquet is missing: {local_root / 'universe'}"
        )
    return facts, sectors


def price_state(local_root: Path) -> dict[str, Any]:
    """The latest close and the technical state that goes with it.

    Optional on purpose: the metrics table predates the price join and is
    still complete without it. Its absence costs the price columns, not the
    run.

    Every indicator is guarded on having enough history to mean anything. A
    200-day average computed over 60 sessions is not a slow average, it is a
    fast one wearing the wrong label, and it would put a symbol at the top of
    a screen for a reason that does not exist.
    """
    duckdb, _ = _modules()
    paths = sorted((local_root / "daily").glob("symbol=*/part.parquet"))
    if not paths:
        return {}
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET threads = 1")
        connection.execute("SET memory_limit = '256MB'")
        rows = connection.execute(
            """
            WITH base AS (
                SELECT
                    symbol,
                    date,
                    "adjClose" AS close,
                    "adjVolume" AS volume,
                    row_number() OVER (PARTITION BY symbol ORDER BY date) AS position,
                    count(*) OVER (PARTITION BY symbol) AS sessions
                FROM read_parquet($paths)
                WHERE "adjClose" IS NOT NULL
            ),
            with_returns AS (
                SELECT
                    *,
                    close / lag(close) OVER (PARTITION BY symbol ORDER BY date) - 1
                        AS daily_return
                FROM base
            ),
            indicators AS (
                SELECT
                    symbol, date, close, volume, sessions, position,
                    avg(close) OVER fifty AS sma_50,
                    avg(close) OVER two_hundred AS sma_200,
                    max(close) OVER year AS high_52w,
                    min(close) OVER year AS low_52w,
                    -- Excludes today: a day is only a spike against the days
                    -- before it, and including it dampens what it measures.
                    median(volume) OVER sixty_before AS median_volume_60,
                    avg(greatest(daily_return, 0)) OVER fortnight AS avg_gain,
                    avg(greatest(-daily_return, 0)) OVER fortnight AS avg_loss,
                    lag(close, 21) OVER (PARTITION BY symbol ORDER BY date) AS close_1m,
                    lag(close, 63) OVER (PARTITION BY symbol ORDER BY date) AS close_3m,
                    lag(close, 252) OVER (PARTITION BY symbol ORDER BY date) AS close_12m
                FROM with_returns
                WINDOW
                    fifty AS (PARTITION BY symbol ORDER BY date ROWS 49 PRECEDING),
                    two_hundred AS (PARTITION BY symbol ORDER BY date ROWS 199 PRECEDING),
                    year AS (PARTITION BY symbol ORDER BY date ROWS 251 PRECEDING),
                    sixty_before AS (
                        PARTITION BY symbol ORDER BY date
                        ROWS BETWEEN 60 PRECEDING AND 1 PRECEDING
                    ),
                    fortnight AS (PARTITION BY symbol ORDER BY date ROWS 13 PRECEDING)
            )
            SELECT
                symbol,
                date,
                close,
                sessions,
                CASE WHEN sessions >= 50 AND sma_50 > 0
                     THEN close / sma_50 - 1 END AS sma_50_gap,
                CASE WHEN sessions >= 200 AND sma_200 > 0
                     THEN close / sma_200 - 1 END AS sma_200_gap,
                CASE WHEN sessions >= 252 AND high_52w > 0
                     THEN close / high_52w - 1 END AS drawdown_from_52w_high,
                CASE WHEN sessions >= 252 AND low_52w > 0
                     THEN close / low_52w - 1 END AS gain_from_52w_low,
                CASE WHEN sessions >= 60 AND median_volume_60 > 0
                     THEN volume / median_volume_60 END AS volume_ratio_60d,
                -- Cutler's RSI: a simple average of the last fourteen days
                -- rather than Wilder's smoothing. The two disagree by a few
                -- points, so the name matters more than the difference.
                CASE
                    WHEN sessions >= 15 AND avg_gain + avg_loss > 0
                    THEN 100 * avg_gain / (avg_gain + avg_loss)
                END AS rsi_14,
                CASE WHEN close_1m > 0 THEN close / close_1m - 1 END AS return_1m,
                CASE WHEN close_3m > 0 THEN close / close_3m - 1 END AS return_3m,
                CASE WHEN close_12m > 0 THEN close / close_12m - 1 END AS return_12m
            FROM indicators
            WHERE position = sessions
            """,
            {"paths": [str(path) for path in paths]},
        )
        columns = [description[0] for description in rows.description]
        return {
            str(row[0]): dict(zip(columns, row, strict=True))
            for row in rows.fetchall()
        }
    finally:
        connection.close()


def metrics_s3_root(settings: Settings) -> str:
    return f"{corpus_s3_root(settings)}/fundamentals_metrics"


def compute(facts: list[Path], sectors: list[Path] | Path) -> Any:
    duckdb, _ = _modules()
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET threads = 1")
        connection.execute("SET memory_limit = '256MB'")
        connection.from_parquet(
            [str(path) for path in facts], hive_partitioning=False
        ).create_view("fundamentals_input")
        sector_paths = [sectors] if isinstance(sectors, Path) else list(sectors)
        # Read each universe separately and tag it, rather than reading them as
        # one set of files: which file a symbol was registered in is the only
        # authority on its market, and it is not written inside the file.
        # Columns are named rather than starred so an extra column in one
        # universe cannot misalign the union.
        connection.execute(
            "CREATE OR REPLACE TEMP VIEW sectors_input AS "
            + " UNION ALL ".join(
                "SELECT symbol, subsector, '{}' AS market FROM read_parquet('{}')".format(
                    SECTOR_MARKETS.get(path.name, "US"), str(path).replace("'", "''")
                )
                for path in sector_paths
            )
        )
        sql = (
            resources.files("usstocks.corpus")
            .joinpath("sql/fundamentals_metrics.sql")
            .read_text(encoding="utf-8")
        )
        # Split on statement terminators only. A bare split(";") also cuts at
        # semicolons inside comments, which turns prose into a parse error.
        for number, statement in enumerate(re.split(r";\s*\n", sql), start=1):
            if not statement.strip():
                continue
            try:
                connection.execute(statement)
            except duckdb.Error as exc:
                raise CorpusError(f"metrics statement {number} failed: {exc}") from exc
        return connection.execute(
            "SELECT * FROM fundamentals_metrics ORDER BY symbol, period_type, period_end"
        ).to_arrow_table()
    finally:
        connection.close()


DIRECTION_METRICS = (
    ("revenue_yoy_change", 1),
    ("gross_margin_yoy_change", 1),
    # A shorter cycle is the healthy direction, so improvement is a fall.
    ("inventory_days_yoy_change", -1),
    ("operating_margin_yoy_change", 1),
)

HISTORY_YEARS = 5
# Three years of quarters: enough to watch a cycle turn without a chart so
# dense the points merge.
HISTORY_QUARTERS = 12


def _ratio(numerator: Any, denominator: Any) -> float | None:
    if numerator is None or not denominator:
        return None
    return numerator / denominator


def trailing_twelve_months(quarters: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The last four quarters summed, when they actually form a year.

    An annual-only table is up to twelve months stale for anyone who files
    quarterly, which is what made TSM's 2024 figures sit beside NVDA's 2026
    ones. Sums are taken over the raw components -- margins are not additive,
    so averaging four quarterly ratios would be a different number entirely.
    """
    if len(quarters) < 4:
        return None
    window = quarters[-4:]
    span = (window[-1]["period_end"] - window[0]["period_start"]).days + 1
    # Four quarters that do not span a year mean a gap or an overlap, and
    # summing them would silently under- or over-count.
    if not 330 <= span <= 400:
        return None
    units = {row.get("revenue_unit") for row in window if row.get("revenue_unit")}
    if len(units) > 1:
        return None

    def total(name: str) -> float | None:
        values = [row.get(name) for row in window]
        return None if any(value is None for value in values) else sum(values)

    revenue = total("revenue")
    if revenue is None:
        return None
    cost = total("cost_of_revenue")
    gross = total("gross_profit")
    latest = window[-1]
    operating_cash_flow = total("operating_cash_flow")
    capex = total("capex")
    return {
        "symbol": latest["symbol"],
        "subsector": latest.get("subsector"),
        "market": latest.get("market"),
        "entity_name": latest.get("entity_name"),
        "period_type": "ttm",
        "period_start": window[0]["period_start"],
        "period_end": latest["period_end"],
        "revenue": revenue,
        "revenue_unit": latest.get("revenue_unit"),
        "gross_margin": (
            _ratio(gross, revenue) if gross is not None else
            (None if cost is None else _ratio(revenue - cost, revenue))
        ),
        "operating_margin": _ratio(total("operating_income"), revenue),
        # Inventory is a balance, not a flow: the closing figure is the level,
        # and the cost of sales it is divided by is the whole year's.
        "inventory_days": (
            None if cost in (None, 0) or latest.get("inventory") is None
            else latest["inventory"] / cost * span
        ),
        "net_income": total("net_income"),
        "free_cash_flow": (
            None if operating_cash_flow is None or capex is None
            else operating_cash_flow - capex
        ),
        # Balances, so the closing figure stands rather than a sum.
        "equity": latest.get("equity"),
        "shares_outstanding": latest.get("shares_outstanding"),
        "foreign_private_issuer": latest.get("foreign_private_issuer"),
        "capex_intensity": _ratio(capex, revenue),
        "rd_intensity": _ratio(total("research_development"), revenue),
        "free_cash_flow_margin": (
            None if operating_cash_flow is None or capex is None
            else _ratio(operating_cash_flow - capex, revenue)
        ),
        "equity_ratio": latest.get("equity_ratio"),
    }


def valuation(row: dict[str, Any], price: dict[str, Any] | None) -> dict[str, Any]:
    """Price-based ratios, or nothing at all.

    Withheld entirely for foreign private issuers. Their US listing is an ADR
    representing some number of ordinary shares -- five to one for TSM -- and
    the corpus records no ratio, so multiplying the ADR price by the ordinary
    share count is wrong by that factor. A price/earnings ratio five times too
    low reads as a bargain rather than as an error, which is the worst way for
    a number to be wrong.

    Withheld too where the filer does not report in USD: the price is in USD
    and dividing it by a figure in another currency produces a plausible
    number that means nothing.
    """
    blank = {
        "market_cap": None, "pe_ratio": None, "ps_ratio": None,
        "pb_ratio": None, "fcf_yield": None, "valuation_withheld": None,
    }
    if row.get("foreign_private_issuer"):
        return blank | {"valuation_withheld": "adr_share_ratio_unknown"}
    if (row.get("revenue_unit") or "USD") != "USD":
        return blank | {"valuation_withheld": "reporting_currency_not_usd"}
    shares = row.get("shares_outstanding")
    if price is None or not shares:
        return blank | {"valuation_withheld": "no_price_or_share_count"}
    close = float(price["close"])
    market_cap = close * float(shares)
    net_income = row.get("net_income")
    equity = row.get("equity")
    free_cash_flow = row.get("free_cash_flow")
    return {
        "market_cap": market_cap,
        # A negative denominator is not a cheap multiple, it is a loss. Left
        # empty rather than printed as a negative ratio nobody reads as one.
        "pe_ratio": (
            market_cap / net_income
            if net_income is not None and net_income > 0 else None
        ),
        "ps_ratio": _ratio(market_cap, row.get("revenue")),
        "pb_ratio": market_cap / equity if equity is not None and equity > 0 else None,
        "fcf_yield": free_cash_flow / market_cap if free_cash_flow is not None else None,
        "valuation_withheld": None,
    }


TECHNICAL_FIELDS = (
    "sma_50_gap",
    "sma_200_gap",
    "drawdown_from_52w_high",
    "gain_from_52w_low",
    "volume_ratio_60d",
    "rsi_14",
    "return_1m",
    "return_3m",
    "return_12m",
)


def technicals(price: dict[str, Any] | None) -> dict[str, Any]:
    """The price state, as it is -- no currency guard.

    Unlike the valuation ratios, these are all price against its own price.
    An ADR's 50-day average is its own average and a receipt ratio cancels
    out, so the columns withheld above are readable here.

    The close lives here rather than with the valuation for the same reason.
    An ADR has a price; what it does not have is a share count that can be
    multiplied by it. Risk figures need the price and never the share count,
    so withholding it there would have taken those symbols out of the
    portfolio for no reason.
    """
    state = price or {}
    date = state.get("date")
    return {name: state.get(name) for name in TECHNICAL_FIELDS} | {
        "price": state.get("close"),
        "price_date": date.isoformat() if date is not None else None,
    }


def build_summary(
    table: Any, *, generated_at: datetime, prices: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The cross-sectional view, prepared here so the API only serves a file.

    Putting DuckDB in the API process would spend memory the 1GB instance does
    not have, and the metrics job already holds everything this needs.
    """
    annual: dict[str, list[dict[str, Any]]] = {}
    quarterly: dict[str, list[dict[str, Any]]] = {}
    for row in table.to_pylist():
        if row.get("revenue") is None:
            continue
        if row.get("period_type") == "annual":
            annual.setdefault(str(row["symbol"]), []).append(row)
        elif row.get("period_type") == "quarter":
            quarterly.setdefault(str(row["symbol"]), []).append(row)

    symbols: list[dict[str, Any]] = []
    for symbol, rows in sorted(annual.items()):
        rows.sort(key=lambda item: item["period_end"])
        latest = rows[-1]

        # Prefer trailing twelve months when it is genuinely newer than the
        # last annual report; a 20-F filer has no quarters and keeps the annual.
        quarters = sorted(
            quarterly.get(symbol, []), key=lambda item: item["period_end"]
        )
        ttm = trailing_twelve_months(quarters)
        basis = "annual"
        if ttm is not None and ttm["period_end"] > latest["period_end"]:
            for name in (
                "revenue_yoy",
                "revenue_yoy_change",
                "gross_margin_yoy_change",
                "operating_margin_yoy_change",
                "inventory_days_yoy_change",
            ):
                ttm[name] = None
            # The four quarters before this window, or -- when the filer has
            # not published eight yet -- the annual report a year earlier,
            # which covers the same twelve months.
            prior = trailing_twelve_months(
                [row for row in quarters if row["period_end"] < ttm["period_start"]]
            )
            if prior is None:
                year_before = [
                    row
                    for row in rows
                    if 330 <= (ttm["period_end"] - row["period_end"]).days <= 400
                ]
                prior = year_before[-1] if year_before else None
            if prior is not None and prior.get("revenue"):
                ttm["revenue_yoy"] = ttm["revenue"] / prior["revenue"] - 1
                if latest.get("revenue_yoy") is not None:
                    ttm["revenue_yoy_change"] = ttm["revenue_yoy"] - latest["revenue_yoy"]
                for name in ("gross_margin", "operating_margin", "inventory_days"):
                    if ttm.get(name) is not None and prior.get(name) is not None:
                        ttm[f"{name}_yoy_change"] = ttm[name] - prior[name]
            latest = ttm
            basis = "ttm"
        # An equal-weight count, not a score: there is no defensible basis for
        # weighting these against each other, and a weighted number would look
        # more authoritative than it is.
        improving = 0
        measured = 0
        for name, better in DIRECTION_METRICS:
            value = latest.get(name)
            if value is None:
                continue
            measured += 1
            if value * better > 0:
                improving += 1
        def as_history(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                {
                    "period_end": item["period_end"].isoformat(),
                    "revenue": item.get("revenue"),
                    "gross_margin": item.get("gross_margin"),
                    "operating_margin": item.get("operating_margin"),
                    "inventory_days": item.get("inventory_days"),
                    "capex_intensity": item.get("capex_intensity"),
                    "rd_intensity": item.get("rd_intensity"),
                    "free_cash_flow_margin": item.get("free_cash_flow_margin"),
                    "revenue_yoy": item.get("revenue_yoy"),
                }
                for item in items
            ]

        history = as_history(rows[-HISTORY_YEARS:])
        # A year hides the turn: a margin that peaked mid-year reads as a flat
        # annual figure. Empty for filers with no quarterly report, which the
        # page states rather than leaving as a blank chart.
        quarterly_history = as_history(quarters[-HISTORY_QUARTERS:])
        symbols.append(
            {
                "symbol": symbol,
                # As the filer wrote it (EDGAR) or as the universe file lists
                # it (EDINET). A 4-digit Japanese code is unreadable without
                # it, and so are half the US tickers.
                "name": latest.get("entity_name"),
                "subsector": latest.get("subsector"),
                # US or JP. A symbol missing from both universe files keeps a
                # null here and the page files it under "その他" rather than
                # dropping it, since a metric row exists for it either way.
                "market": latest.get("market"),
                # Which basis produced these figures, so the reader is never
                # left guessing whether a date is the fiscal year or a window.
                "basis": basis,
                "period_end": latest["period_end"].isoformat(),
                "annual_period_end": rows[-1]["period_end"].isoformat(),
                "currency": latest.get("revenue_unit"),
                "revenue": latest.get("revenue"),
                "revenue_yoy": latest.get("revenue_yoy"),
                "revenue_yoy_change": latest.get("revenue_yoy_change"),
                "gross_margin": latest.get("gross_margin"),
                "gross_margin_yoy_change": latest.get("gross_margin_yoy_change"),
                "operating_margin": latest.get("operating_margin"),
                "operating_margin_yoy_change": latest.get("operating_margin_yoy_change"),
                "inventory_days": latest.get("inventory_days"),
                "inventory_days_yoy_change": latest.get("inventory_days_yoy_change"),
                "capex_intensity": latest.get("capex_intensity"),
                "rd_intensity": latest.get("rd_intensity"),
                "free_cash_flow_margin": latest.get("free_cash_flow_margin"),
                "equity_ratio": latest.get("equity_ratio"),
                "improving": improving,
                "improving_measured": measured,
                **valuation(latest, (prices or {}).get(symbol)),
                **technicals((prices or {}).get(symbol)),
                "history": history,
                "quarterly_history": quarterly_history,
            }
        )
    return {
        "version": 1,
        "generated_at": generated_at.isoformat(),
        "history_years": HISTORY_YEARS,
        "history_quarters": HISTORY_QUARTERS,
        # Named so the page can say what the count is, rather than implying a
        # weighting nobody chose.
        "direction_metrics": [name for name, _ in DIRECTION_METRICS],
        "symbols": symbols,
    }


def write_summary(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def write_metrics(path: Path, table: Any) -> str:
    _, pq = _modules()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    pq.write_table(table, temporary, compression="zstd")
    digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
    temporary.replace(path)
    return digest


def run(settings: Settings, *, uploader: Uploader = aws_upload) -> int:
    local_root = settings.corpus_local_dir
    facts, sectors = discover_inputs(local_root)
    table = compute(facts, sectors)

    path = local_root / "fundamentals_metrics" / "part.parquet"
    digest = write_metrics(path, table)

    summary = build_summary(
        table, generated_at=datetime.now(tz=UTC), prices=price_state(local_root)
    )
    summary_path = local_root / "fundamentals" / "summary.json"
    write_summary(summary_path, summary)

    # The reading guide is generated where Ollama runs, which is not this host.
    # Publishing the summary to the exchange is how it gets there.
    try:
        exchange = analysis_exchange_s3_root(settings)
    except CorpusError:
        exchange = ""

    state_path = local_root / "fundamentals-metrics-state.json"
    state = load_state(state_path)
    if state.get("digest") != digest:
        uploader(path, f"{metrics_s3_root(settings)}/part.parquet")
        state["digest"] = digest

    # Hashed over the figures alone, not the file: generated_at moves every run,
    # and the Mac skips a report whose bytes it has already seen. Publishing an
    # identical summary under a new timestamp would re-run Qwen across every
    # symbol for nothing.
    content_digest = hashlib.sha256(
        json.dumps(summary["symbols"], ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    if exchange and state.get("summary_digest") != content_digest:
        uploader(summary_path, f"{exchange}/input/latest/fundamentals.json")
        state["summary_digest"] = content_digest
    state["row_count"] = table.num_rows
    state["last_success_utc"] = datetime.now(tz=UTC).isoformat()
    save_state(state_path, state)

    # Counting what survived each guard is the only way to notice that a
    # provider change quietly emptied a column.
    columns = table.column_names
    covered = {
        name: table.num_rows - table.column(name).null_count
        for name in ("revenue", "gross_margin", "inventory_days", "revenue_yoy")
        if name in columns
    }
    log.info(
        "fundamentals metrics complete: %d row(s) from %d symbol(s), %d in the summary; "
        "populated %s",
        table.num_rows,
        len(facts),
        len(summary["symbols"]),
        ", ".join(f"{name}={count}" for name, count in covered.items()),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        return run(settings)
    except CorpusError as exc:
        log.error("%s", exc)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
