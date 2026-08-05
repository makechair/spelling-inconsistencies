"""Metric computation over archived XBRL facts.

Each test here corresponds to something the first production pull actually
contained (docs/earnings-spec.md 9), not to a hypothetical.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from usstocks.config import Settings
from usstocks.corpus.daily import CorpusError, read_parquet_rows
from usstocks.corpus.fundamentals_metrics import build_summary, compute, run

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")


def fact(concept, value, *, unit="USD", start="2025-01-01", end="2025-12-31",
         form="10-K", filed="2026-02-01", accession="a-1", symbol="MU"):
    return {
        "symbol": symbol,
        "cik": "1",
        "entity_name": "Example",
        "concept": concept,
        "xbrl_tag": "Tag",
        "unit": unit,
        "period_start": date.fromisoformat(start) if start else None,
        "period_end": date.fromisoformat(end),
        "fiscal_year": 2025,
        "fiscal_period": "FY",
        "form": form,
        "accession": accession,
        "filed": date.fromisoformat(filed),
        "value": float(value),
    }


FACT_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()), ("cik", pa.string()), ("entity_name", pa.string()),
        ("concept", pa.string()), ("xbrl_tag", pa.string()), ("unit", pa.string()),
        ("period_start", pa.date32()), ("period_end", pa.date32()),
        ("fiscal_year", pa.int64()), ("fiscal_period", pa.string()),
        ("form", pa.string()), ("accession", pa.string()), ("filed", pa.date32()),
        ("value", pa.float64()),
    ]
)


def build(tmp_path: Path, facts, sectors=(("MU", "memory_storage"),)):
    facts_dir = tmp_path / "fundamentals" / "symbol=MU"
    facts_dir.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(list(facts), schema=FACT_SCHEMA),
                   facts_dir / "part.parquet")
    sectors_path = tmp_path / "universe" / "sectors.parquet"
    sectors_path.parent.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist(
            [{"symbol": s, "subsector": sub} for s, sub in sectors],
            schema=pa.schema([("symbol", pa.string()), ("subsector", pa.string())]),
        ),
        sectors_path,
    )
    return [facts_dir / "part.parquet"], sectors_path


def rows_of(table):
    return table.to_pylist()


def test_margins_and_inventory_days_come_out_of_a_clean_annual_period(tmp_path: Path):
    facts, sectors = build(tmp_path, [
        fact("revenue", 1000),
        fact("cost_of_revenue", 600),
        fact("operating_income", 250),
        fact("net_income", 200),
        fact("capex", 150),
        fact("research_development", 100),
        fact("operating_cash_flow", 400),
        fact("inventory", 300, start=None),
    ])
    row = rows_of(compute(facts, sectors))[0]
    assert row["period_type"] == "annual"
    assert row["gross_margin"] == pytest.approx(0.4)
    assert row["operating_margin"] == pytest.approx(0.25)
    assert row["net_margin"] == pytest.approx(0.2)
    assert row["capex_intensity"] == pytest.approx(0.15)
    assert row["rd_intensity"] == pytest.approx(0.1)
    assert row["free_cash_flow"] == pytest.approx(250)
    # 300 / 600 * 365 days
    assert row["inventory_days"] == pytest.approx(300 / 600 * 365)
    assert row["subsector"] == "memory_storage"


def test_a_ratio_across_two_currencies_is_refused(tmp_path: Path):
    """TSM files revenue in TWD and other figures in USD. Dividing one by the
    other yields a number that looks fine and means nothing.

    The guard is per ratio, on its own two operands: a margin mixing TWD
    revenue with USD cost is refused, while inventory days computed from two
    USD figures is still sound and is kept.
    """
    facts, sectors = build(tmp_path, [
        fact("revenue", 1000, unit="TWD"),
        fact("cost_of_revenue", 600, unit="USD"),
        fact("inventory", 300, unit="USD", start=None),
    ])
    row = rows_of(compute(facts, sectors))[0]
    assert row["revenue"] == 1000
    assert row["gross_margin"] is None
    assert row["inventory_days"] == pytest.approx(300 / 600 * 365)


def test_inventory_days_is_refused_when_its_own_operands_disagree(tmp_path: Path):
    facts, sectors = build(tmp_path, [
        fact("revenue", 1000, unit="USD"),
        fact("cost_of_revenue", 600, unit="USD"),
        fact("inventory", 300, unit="TWD", start=None),
    ])
    row = rows_of(compute(facts, sectors))[0]
    assert row["gross_margin"] == pytest.approx(0.4)
    assert row["inventory_days"] is None


def test_the_newest_filing_wins_for_a_repeated_period(tmp_path: Path):
    """A 10-K restates the prior year, so the same period arrives twice."""
    facts, sectors = build(tmp_path, [
        fact("revenue", 1000, filed="2026-02-01", accession="original"),
        fact("revenue", 1100, filed="2027-02-01", accession="restated"),
    ])
    rows = rows_of(compute(facts, sectors))
    assert len(rows) == 1
    assert rows[0]["revenue"] == 1100


def test_proxy_statements_and_earnings_releases_are_excluded(tmp_path: Path):
    """DEF 14A share counts are for voting; an 8-K repeats the quarter."""
    facts, sectors = build(tmp_path, [
        fact("revenue", 1000, form="10-K"),
        fact("revenue", 9999, form="8-K", accession="eightk", filed="2026-01-05"),
        fact("shares_outstanding", 42, form="DEF 14A", start=None, accession="proxy"),
    ])
    rows = rows_of(compute(facts, sectors))
    assert len(rows) == 1
    assert rows[0]["revenue"] == 1000
    assert rows[0]["shares_outstanding"] is None


def test_six_k_quarters_are_kept(tmp_path: Path):
    """336 of ARM's 444 rows were 6-K. Dropping it deletes every quarter the
    foreign filers have."""
    facts, sectors = build(tmp_path, [
        fact("revenue", 250, form="6-K", start="2025-01-01", end="2025-03-31"),
    ])
    rows = rows_of(compute(facts, sectors))
    assert [row["period_type"] for row in rows] == ["quarter"]


def test_year_on_year_and_its_acceleration(tmp_path: Path):
    facts, sectors = build(tmp_path, [
        fact("revenue", 800, start="2023-01-01", end="2023-12-31",
             filed="2024-02-01", accession="fy23"),
        fact("revenue", 1000, start="2024-01-01", end="2024-12-31",
             filed="2025-02-01", accession="fy24"),
        fact("revenue", 1150, start="2025-01-01", end="2025-12-31",
             filed="2026-02-01", accession="fy25"),
    ])
    by_end = {row["period_end"]: row for row in rows_of(compute(facts, sectors))}
    assert by_end[date(2024, 12, 31)]["revenue_yoy"] == pytest.approx(0.25)
    assert by_end[date(2025, 12, 31)]["revenue_yoy"] == pytest.approx(0.15)
    # Growth decelerating: 15% after 25%.
    assert by_end[date(2025, 12, 31)]["revenue_yoy_change"] == pytest.approx(-0.10)


def test_a_concept_the_filer_never_tagged_stays_null(tmp_path: Path):
    """ARM reports no inventory because an IP licensor has none. Zero would
    read as a real measurement."""
    facts, sectors = build(tmp_path, [
        fact("revenue", 1000),
        fact("cost_of_revenue", 600),
    ])
    row = rows_of(compute(facts, sectors))[0]
    assert row["inventory_days"] is None
    assert row["gross_margin"] == pytest.approx(0.4)


def test_periods_of_an_unrecognisable_length_are_dropped(tmp_path: Path):
    facts, sectors = build(tmp_path, [
        fact("revenue", 50, start="2025-01-01", end="2025-02-05"),
    ])
    assert rows_of(compute(facts, sectors)) == []


def test_run_writes_and_uploads_once_then_stays_quiet(tmp_path: Path):
    build(tmp_path, [fact("revenue", 1000), fact("cost_of_revenue", 600)])
    settings = Settings(
        db_path=tmp_path / "market.db",
        live_db_path=tmp_path / "live.db",
        corpus_local_dir=tmp_path,
        backup_s3_uri="s3://example-bucket",
        auth_mode="disabled",
    )
    uploads: list[str] = []
    assert run(settings, uploader=lambda p, d: uploads.append(d)) == 0
    assert run(settings, uploader=lambda p, d: uploads.append(d)) == 0
    assert uploads == ["s3://example-bucket/corpus/fundamentals_metrics/part.parquet"]
    stored = read_parquet_rows(tmp_path / "fundamentals_metrics" / "part.parquet")
    assert stored[0]["gross_margin"] == pytest.approx(0.4)


def test_run_refuses_to_guess_when_the_facts_are_missing(tmp_path: Path):
    settings = Settings(
        db_path=tmp_path / "market.db",
        live_db_path=tmp_path / "live.db",
        corpus_local_dir=tmp_path,
        backup_s3_uri="s3://example-bucket",
        auth_mode="disabled",
    )
    with pytest.raises(CorpusError, match="no fundamentals Parquet"):
        run(settings, uploader=lambda p, d: None)


def test_summary_counts_improving_metrics_without_weighting_them(tmp_path: Path):
    """An equal-weight count is defensible; a weighted score would imply a
    judgement about which indicator matters more that nothing supports."""
    facts, sectors = build(tmp_path, [
        fact("revenue", 800, start="2023-01-01", end="2023-12-31",
             filed="2024-02-01", accession="fy23"),
        fact("cost_of_revenue", 500, start="2023-01-01", end="2023-12-31",
             filed="2024-02-01", accession="fy23"),
        fact("revenue", 1000, start="2024-01-01", end="2024-12-31",
             filed="2025-02-01", accession="fy24"),
        fact("cost_of_revenue", 600, start="2024-01-01", end="2024-12-31",
             filed="2025-02-01", accession="fy24"),
        fact("revenue", 1150, start="2025-01-01", end="2025-12-31",
             filed="2026-02-01", accession="fy25"),
        fact("cost_of_revenue", 700, start="2025-01-01", end="2025-12-31",
             filed="2026-02-01", accession="fy25"),
    ])
    summary = build_summary(compute(facts, sectors), generated_at=datetime.now(tz=UTC))
    entry = summary["symbols"][0]
    assert entry["symbol"] == "MU"
    assert entry["period_end"] == "2025-12-31"
    # Growth decelerated and the margin slipped, so neither counts as improving.
    assert entry["improving"] == 0
    assert entry["improving_measured"] == 2
    assert [point["period_end"] for point in entry["history"]] == [
        "2023-12-31", "2024-12-31", "2025-12-31"
    ]


def test_summary_skips_periods_with_no_revenue(tmp_path: Path):
    """A period the filer only partly tagged would otherwise become a row with
    an empty headline figure."""
    facts, sectors = build(tmp_path, [fact("capex", 100)])
    summary = build_summary(compute(facts, sectors), generated_at=datetime.now(tz=UTC))
    assert summary["symbols"] == []


def quarter(concept, value, *, start, end, accession, **kwargs):
    return fact(concept, value, start=start, end=end, form="10-Q",
                accession=accession, **kwargs)


def test_trailing_twelve_months_replaces_a_stale_annual(tmp_path: Path):
    """An annual-only table sat TSM's 2024 figures beside NVDA's 2026 ones.
    Where four quarters exist past the last annual, they are the current year."""
    facts = [
        fact("revenue", 1000, start="2024-01-01", end="2024-12-31",
             filed="2025-02-01", accession="fy24"),
        fact("cost_of_revenue", 600, start="2024-01-01", end="2024-12-31",
             filed="2025-02-01", accession="fy24"),
    ]
    ends = [("2025-01-01", "2025-03-31"), ("2025-04-01", "2025-06-30"),
            ("2025-07-01", "2025-09-30"), ("2025-10-01", "2025-12-31")]
    for index, (start, end) in enumerate(ends):
        facts.append(quarter("revenue", 300, start=start, end=end,
                             accession=f"q{index}", filed="2026-01-15"))
        facts.append(quarter("cost_of_revenue", 150, start=start, end=end,
                             accession=f"q{index}", filed="2026-01-15"))
    built, sectors = build(tmp_path, facts)
    entry = build_summary(compute(built, sectors), generated_at=datetime.now(tz=UTC))["symbols"][0]

    assert entry["basis"] == "ttm"
    assert entry["period_end"] == "2025-12-31"
    assert entry["annual_period_end"] == "2024-12-31"
    assert entry["revenue"] == pytest.approx(1200)
    # Summed components, not averaged ratios: 1 - 600/1200.
    assert entry["gross_margin"] == pytest.approx(0.5)
    assert entry["revenue_yoy"] == pytest.approx(0.2)


def test_a_filer_without_quarters_keeps_its_annual(tmp_path: Path):
    """20-F filers have no quarterly report at all."""
    built, sectors = build(tmp_path, [
        fact("revenue", 1000, start="2024-01-01", end="2024-12-31",
             filed="2025-02-01", accession="fy24"),
    ])
    entry = build_summary(compute(built, sectors), generated_at=datetime.now(tz=UTC))["symbols"][0]
    assert entry["basis"] == "annual"
    assert entry["period_end"] == "2024-12-31"


def test_quarters_that_do_not_span_a_year_are_not_summed(tmp_path: Path):
    """A gap in the filings would otherwise be reported as a full year that
    happens to be short."""
    facts = [
        fact("revenue", 1000, start="2024-01-01", end="2024-12-31",
             filed="2025-02-01", accession="fy24"),
    ]
    # Q2 is missing, so the four newest quarters reach back eighteen months.
    for index, (start, end) in enumerate([
        ("2024-07-01", "2024-09-30"), ("2024-10-01", "2024-12-31"),
        ("2025-01-01", "2025-03-31"), ("2025-10-01", "2025-12-31"),
    ]):
        facts.append(quarter("revenue", 300, start=start, end=end,
                             accession=f"q{index}", filed="2026-01-15"))
    built, sectors = build(tmp_path, facts)
    entry = build_summary(compute(built, sectors), generated_at=datetime.now(tz=UTC))["symbols"][0]
    assert entry["basis"] == "annual"
