"""EDINET discovery and archival.

api.edinet-fsa.go.jp is unreachable from the development container, so these
are fixtures built from the documented v2 response shape. They pin the
decisions that hold whatever the real payload looks like: which filings are
ours, how the securities code lines up, and what is skipped on a second run.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx

from usstocks.config import Settings
from usstocks.corpus.daily import CorpusError, read_parquet_rows
from usstocks.corpus.edinet import (
    fetch_document,
    load_universe_jp,
    run,
    select_documents,
)

UNIVERSE_JP = Path(__file__).resolve().parents[1] / "data" / "universe_jp.csv"


def make_settings(tmp_path: Path, **updates) -> Settings:
    values = {
        "db_path": tmp_path / "market.db",
        "live_db_path": tmp_path / "live.db",
        "corpus_local_dir": tmp_path / "corpus",
        "backup_s3_uri": "s3://example-bucket",
        "edinet_api_key": "test-key",
        "auth_mode": "disabled",
    }
    values.update(updates)
    return Settings(**values)


def listing(**overrides) -> dict:
    row = {
        "docID": "S100ABCD",
        "edinetCode": "E01234",
        "secCode": "80350",
        "filerName": "東京エレクトロン株式会社",
        "docTypeCode": "120",
        "docDescription": "有価証券報告書",
        "periodStart": "2025-04-01",
        "periodEnd": "2026-03-31",
        "submitDateTime": "2026-06-25 15:00",
    }
    row.update(overrides)
    return row


def test_repository_universe_jp_is_unique_four_digit_codes():
    entries = load_universe_jp(UNIVERSE_JP)
    codes = {entry.code for entry in entries}
    assert len(codes) == len(entries)
    assert all(len(entry.code) == 4 and entry.code.isdigit() for entry in entries)
    # The eight names asked for on top of the semiconductor core.
    assert {"3110", "5801", "4062", "6981", "2802", "5803", "6525", "5016"} <= codes


def test_the_five_character_securities_code_maps_to_our_four_digit_one():
    """EDINET writes 8035 as 80350. Comparing raw strings matches nothing."""
    selected = select_documents([listing(secCode="80350")], {"8035"})
    assert [row["docID"] for row in selected] == ["S100ABCD"]


def test_only_periodic_reports_from_the_universe_are_taken():
    rows = [
        listing(docID="ours", secCode="80350", docTypeCode="120"),
        listing(docID="semiannual", secCode="69810", docTypeCode="160"),
        # Filed by someone outside the universe.
        listing(docID="stranger", secCode="99990", docTypeCode="120"),
        # A large-shareholding report carries no financial statements.
        listing(docID="shareholding", secCode="80350", docTypeCode="350"),
        # Funds and other filers have no securities code at all.
        listing(docID="nocode", secCode=None, docTypeCode="120"),
    ]
    selected = select_documents(rows, {"8035", "6981"})
    assert {row["docID"] for row in selected} == {"ours", "semiannual"}


def test_a_json_error_body_is_treated_as_a_missing_file():
    """EDINET answers "no document of that type" with 200 and a JSON body.
    Writing that to S3 would archive an error message as if it were a filing."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"metadata": {"status": "404"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert fetch_document(client, make_settings(Path("/tmp")), "S100ABCD", 2) is None


def test_run_archives_pdf_and_xbrl_and_indexes_them(tmp_path: Path):
    settings = make_settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/documents.json"):
            if request.url.params.get("date") == "2026-06-25":
                return httpx.Response(200, json={"results": [listing()]})
            return httpx.Response(200, json={"results": []})
        if request.url.params.get("type") == "1":
            return httpx.Response(200, content=b"PK\x03\x04 zip bytes")
        return httpx.Response(200, content=b"%PDF-1.7 pdf bytes")

    uploads: list[tuple[Path, str]] = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert run(
            settings,
            since=date(2026, 6, 24),
            until=date(2026, 6, 26),
            uploader=lambda p, d: uploads.append((p, d)),
            client=client,
            now=None,
            sleeper=lambda _s: None,
        ) == 0

    destinations = [destination for _, destination in uploads]
    assert "s3://example-bucket/corpus/edinet/code=8035/S100ABCD/xbrl.zip" in destinations
    assert "s3://example-bucket/corpus/edinet/code=8035/S100ABCD/document.pdf" in destinations
    assert "s3://example-bucket/corpus/edinet_index/part.parquet" in destinations

    pdf = tmp_path / "corpus" / "edinet" / "code=8035" / "S100ABCD" / "document.pdf"
    assert pdf.read_bytes().startswith(b"%PDF")

    index = read_parquet_rows(tmp_path / "corpus" / "edinet_index" / "part.parquet")
    assert len(index) == 1
    assert index[0]["code"] == "8035"
    assert index[0]["name"] == "東京エレクトロン"
    assert index[0]["doc_type_code"] == "120"
    assert index[0]["period_end"] == "2026-03-31"
    assert index[0]["pdf_s3_key"].endswith("document.pdf")


def test_a_second_run_refetches_nothing(tmp_path: Path):
    """EDINET charges nothing but a document is immutable once filed, and the
    archive is the point -- re-downloading it every day is pure waste."""
    settings = make_settings(tmp_path)
    document_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/documents.json"):
            return httpx.Response(200, json={"results": [listing()]})
        document_requests.append(str(request.url.path))
        return httpx.Response(200, content=b"%PDF-1.7")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        for _ in range(2):
            run(
                settings,
                since=date(2026, 6, 25),
                until=date(2026, 6, 25),
                uploader=lambda p, d: None,
                client=client,
                now=None,
                sleeper=lambda _s: None,
            )

    assert len(document_requests) == 2  # one XBRL and one PDF, fetched once


def test_run_stops_at_the_first_edinet_error(tmp_path: Path):
    settings = make_settings(tmp_path)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url.path))
        return httpx.Response(429, json={})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert run(
            settings,
            since=date(2026, 6, 1),
            until=date(2026, 6, 30),
            uploader=lambda p, d: None,
            client=client,
            now=None,
            sleeper=lambda _s: None,
        ) == 2

    assert len(calls) == 1


def test_run_requires_a_subscription_key(tmp_path: Path):
    settings = make_settings(tmp_path, edinet_api_key=None)
    try:
        run(
            settings,
            since=date(2026, 6, 25),
            until=date(2026, 6, 25),
            client=httpx.Client(),
            sleeper=lambda _s: None,
        )
    except CorpusError as exc:
        assert "USSTOCKS_EDINET_API_KEY" in str(exc)
    else:
        raise AssertionError("a missing subscription key must stop the run")


def test_a_non_business_day_returns_no_results_without_failing(tmp_path: Path):
    settings = make_settings(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        # EDINET answers a holiday with metadata and no results key at all.
        return httpx.Response(200, json={"metadata": {"resultset": {"count": 0}}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert run(
            settings,
            since=date(2026, 1, 1),
            until=date(2026, 1, 3),
            uploader=lambda p, d: None,
            client=client,
            sleeper=lambda _s: None,
        ) == 0
