"""EDGAR company-facts normalization.

sec.gov is unreachable from the development container, so these fixtures are
built from the documented companyfacts shape rather than a captured response.
They pin the decisions that survive whatever the real payload looks like:
which tag wins, what counts as a period, and what happens when a concept is
missing entirely.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx

from usstocks.config import Settings
from usstocks.corpus.daily import CorpusError, read_parquet_rows
from usstocks.corpus.fundamentals import (
    fetch_cik_map,
    normalize_company_facts,
    run,
    write_fundamentals_parquet,
)


def make_settings(tmp_path: Path, **updates) -> Settings:
    values = {
        "db_path": tmp_path / "market.db",
        "live_db_path": tmp_path / "live.db",
        "corpus_local_dir": tmp_path / "corpus",
        "backup_s3_uri": "s3://example-bucket",
        "sec_user_agent": "usstocks-test contact@example.com",
        "auth_mode": "disabled",
    }
    values.update(updates)
    return Settings(**values)


def usd_fact(**overrides) -> dict:
    fact = {
        "start": "2026-01-01",
        "end": "2026-03-31",
        "val": 1000.0,
        "fy": 2026,
        "fp": "Q1",
        "form": "10-Q",
        "accn": "0000000000-26-000001",
        "filed": "2026-04-20",
    }
    fact.update(overrides)
    return fact


def company_facts(**concept_units) -> dict:
    return {
        "cik": 1045810,
        "entityName": "Example Semiconductor",
        "facts": {"us-gaap": concept_units},
    }


def test_the_more_specific_revenue_tag_wins_and_is_recorded():
    """Filers often report several revenue tags. Which one was taken has to be
    on the row, otherwise a later outlier cannot be traced back to its source.
    """
    payload = company_facts(
        Revenues={"units": {"USD": [usd_fact(val=900.0)]}},
        RevenueFromContractWithCustomerExcludingAssessedTax={
            "units": {"USD": [usd_fact(val=1000.0)]}
        },
    )
    rows = normalize_company_facts("NVDA", payload)
    revenue = [row for row in rows if row["concept"] == "revenue"]
    assert len(revenue) == 1
    assert revenue[0]["value"] == 1000.0
    assert revenue[0]["xbrl_tag"] == "RevenueFromContractWithCustomerExcludingAssessedTax"


def test_balance_sheet_and_income_facts_are_not_mixed():
    """Inventory is a stock and revenue is a flow. XBRL separates them by
    whether the fact has a start date; comparing them would be meaningless."""
    payload = company_facts(
        InventoryNet={
            "units": {
                "USD": [
                    usd_fact(start=None, val=500.0) | {"start": None},
                    usd_fact(val=999.0),  # a duration-shaped inventory fact
                ]
            }
        },
        Revenues={"units": {"USD": [usd_fact(val=1000.0), usd_fact(start=None) | {"start": None}]}},
    )
    payload["facts"]["us-gaap"]["InventoryNet"]["units"]["USD"][0].pop("start")
    payload["facts"]["us-gaap"]["Revenues"]["units"]["USD"][1].pop("start")

    rows = normalize_company_facts("MU", payload)
    inventory = [row for row in rows if row["concept"] == "inventory"]
    revenue = [row for row in rows if row["concept"] == "revenue"]

    assert [row["value"] for row in inventory] == [500.0]
    assert [row["value"] for row in revenue] == [1000.0]
    assert inventory[0]["period_start"] is None
    assert revenue[0]["period_start"] == date(2026, 1, 1)


def test_a_concept_with_no_matching_tag_is_absent_not_zero():
    payload = company_facts(Revenues={"units": {"USD": [usd_fact()]}})
    rows = normalize_company_facts("ARM", payload)
    assert {row["concept"] for row in rows} == {"revenue"}


def test_amendments_are_kept_as_their_own_rows():
    """A restated figure must not silently overwrite the one an earlier report
    may already have cited."""
    payload = company_facts(
        Revenues={
            "units": {
                "USD": [
                    usd_fact(val=1000.0, accn="0000000000-26-000001", form="10-Q"),
                    usd_fact(val=1100.0, accn="0000000000-26-000009", form="10-Q/A"),
                ]
            }
        }
    )
    rows = normalize_company_facts("AMD", payload)
    assert sorted(row["value"] for row in rows) == [1000.0, 1100.0]
    assert {row["form"] for row in rows} == {"10-Q", "10-Q/A"}


def test_cik_map_is_zero_padded_to_ten_digits():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"0": {"cik_str": 1045810, "ticker": "nvda", "title": "NVIDIA"}}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        mapping = fetch_cik_map(client, make_settings(Path("/tmp")))
    assert mapping["NVDA"] == "0001045810"


def test_run_requires_a_contact_in_the_user_agent(tmp_path: Path):
    """SEC answers anonymous traffic with 403, which would otherwise look like
    an outage rather than a missing setting."""
    settings = make_settings(tmp_path, sec_user_agent=None)
    try:
        run(settings, force=True, symbols=["NVDA"], client=httpx.Client())
    except CorpusError as exc:
        assert "USSTOCKS_SEC_USER_AGENT" in str(exc)
    else:
        raise AssertionError("an empty User-Agent must stop the run")


def test_run_writes_one_partition_per_symbol_and_uploads_on_change(tmp_path: Path):
    settings = make_settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if "company_tickers" in str(request.url):
            return httpx.Response(
                200, json={"0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA"}}
            )
        return httpx.Response(
            200, json=company_facts(Revenues={"units": {"USD": [usd_fact()]}})
        )

    uploads: list[tuple[Path, str]] = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert run(
            settings,
            force=True,
            symbols=["NVDA"],
            uploader=lambda p, d: uploads.append((p, d)),
            client=client,
            sleeper=lambda _seconds: None,
        ) == 0
        # Unchanged facts must not re-upload: EDGAR returns the full history
        # every time, so without the digest check every run would resend it.
        assert run(
            settings,
            force=True,
            symbols=["NVDA"],
            uploader=lambda p, d: uploads.append((p, d)),
            client=client,
            sleeper=lambda _seconds: None,
        ) == 0

    assert [destination for _, destination in uploads] == [
        "s3://example-bucket/corpus/fundamentals/symbol=NVDA/part.parquet"
    ]
    rows = read_parquet_rows(tmp_path / "corpus" / "fundamentals" / "symbol=NVDA" / "part.parquet")
    assert [row["concept"] for row in rows] == ["revenue"]
    assert rows[0]["xbrl_tag"] == "Revenues"


def test_run_stops_at_the_first_sec_error_instead_of_hammering(tmp_path: Path):
    settings = make_settings(tmp_path)
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if "company_tickers" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA"},
                    "1": {"cik_str": 2488, "ticker": "AMD", "title": "AMD"},
                },
            )
        return httpx.Response(429, json={})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert run(
            settings,
            force=True,
            symbols=["NVDA", "AMD"],
            uploader=lambda p, d: None,
            client=client,
            sleeper=lambda _seconds: None,
        ) == 2

    assert len([url for url in requests if "companyfacts" in url]) == 1


def test_symbols_outside_the_universe_are_refused(tmp_path: Path):
    try:
        run(make_settings(tmp_path), force=True, symbols=["FAKE"], client=httpx.Client())
    except CorpusError as exc:
        assert "not in universe" in str(exc)
    else:
        raise AssertionError("an unknown symbol must stop the run")


def test_symbols_without_an_sec_filing_are_skipped_not_fatal(tmp_path: Path):
    """20-F filers are in EDGAR, but a universe symbol with no CIK at all is a
    fact about the listing, not a reason to abandon the other 52."""
    settings = make_settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if "company_tickers" in str(request.url):
            return httpx.Response(
                200, json={"0": {"cik_str": 2488, "ticker": "AMD", "title": "AMD"}}
            )
        return httpx.Response(200, json=company_facts(Revenues={"units": {"USD": [usd_fact()]}}))

    uploads: list[str] = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert run(
            settings,
            force=True,
            symbols=["UMC", "AMD"],
            uploader=lambda p, d: uploads.append(d),
            client=client,
            sleeper=lambda _seconds: None,
        ) == 0

    assert uploads == ["s3://example-bucket/corpus/fundamentals/symbol=AMD/part.parquet"]


def test_write_is_atomic_and_sorted(tmp_path: Path):
    path = tmp_path / "symbol=NVDA" / "part.parquet"
    rows = normalize_company_facts(
        "NVDA",
        company_facts(
            Revenues={"units": {"USD": [usd_fact(end="2026-06-30"), usd_fact(end="2026-03-31")]}}
        ),
    )
    digest = write_fundamentals_parquet(path, rows)
    assert path.exists()
    assert not path.with_suffix(".tmp").exists()
    stored = read_parquet_rows(path)
    assert [row["period_end"] for row in stored] == [date(2026, 3, 31), date(2026, 6, 30)]
    assert write_fundamentals_parquet(path, rows) == digest


def ifrs_facts(**concept_units) -> dict:
    """A 20-F filer: ifrs-full tags, and figures in the reporting currency."""
    return {
        "cik": 1046179,
        "entityName": "Taiwan Semiconductor Manufacturing",
        "facts": {"ifrs-full": concept_units},
    }


def test_ifrs_filers_are_read_not_silently_empty():
    """TSM returned nothing on the first production run because only us-gaap
    was searched. Foreign private issuers file 20-F under ifrs-full."""
    payload = ifrs_facts(
        Revenue={"units": {"TWD": [usd_fact(val=1_000_000.0)]}},
        Inventories={"units": {"TWD": [usd_fact(val=250_000.0) | {"start": None}]}},
    )
    payload["facts"]["ifrs-full"]["Inventories"]["units"]["TWD"][0].pop("start")

    rows = normalize_company_facts("TSM", payload)
    by_concept = {row["concept"]: row for row in rows}
    assert by_concept["revenue"]["value"] == 1_000_000.0
    assert by_concept["revenue"]["xbrl_tag"] == "Revenue"
    assert by_concept["inventory"]["value"] == 250_000.0


def test_the_reporting_currency_travels_on_the_row():
    """No FX source exists here, so figures stay in the filer's currency. The
    ratios this corpus is for are currency-neutral; the unit lets a consumer
    tell a TWD level from a USD one instead of comparing them by accident."""
    twd = normalize_company_facts(
        "TSM", ifrs_facts(Revenue={"units": {"TWD": [usd_fact(val=1_000_000.0)]}})
    )
    usd = normalize_company_facts(
        "MU", company_facts(Revenues={"units": {"USD": [usd_fact(val=1000.0)]}})
    )
    assert twd[0]["unit"] == "TWD"
    assert usd[0]["unit"] == "USD"


def test_usd_wins_when_a_filer_reports_more_than_one_currency():
    payload = ifrs_facts(
        Revenue={
            "units": {
                "TWD": [usd_fact(val=1_000_000.0)],
                "USD": [usd_fact(val=32_000.0)],
            }
        }
    )
    rows = normalize_company_facts("TSM", payload)
    assert rows[0]["unit"] == "USD"
    assert rows[0]["value"] == 32_000.0


def test_per_share_units_are_not_mistaken_for_levels():
    payload = company_facts(Revenues={"units": {"USD/shares": [usd_fact(val=2.5)]}})
    assert normalize_company_facts("MU", payload) == []


def test_unmatched_payloads_report_what_the_filer_does_have():
    """Development cannot reach sec.gov, so a bare failure costs a round trip
    to whoever can run it. The warning names the taxonomies instead."""
    from usstocks.corpus.fundamentals import describe_available_facts

    described = describe_available_facts(
        {"facts": {"ifrs-full": {"Revenue": {}, "Inventories": {}}}}
    )
    assert "ifrs-full (2 tags)" in described
    assert "Revenue" in described
