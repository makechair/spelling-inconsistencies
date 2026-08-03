"""Notion-to-Parquet news corpus normalization and incremental uploads."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx

from usstocks.config import Settings
from usstocks.corpus.daily import CorpusError, read_parquet_rows
from usstocks.corpus.news import (
    NotionCredentials,
    check,
    fetch_pages,
    load_credentials,
    normalize_page,
    run,
)


def notion_page(
    page_id: str,
    day: str,
    *,
    headline: str = "Micron、HBM4増産へ",
    summary: str = "MicronがHBM4の増産計画を発表した。",
) -> dict:
    plain = lambda value: [{"plain_text": value}]  # noqa: E731
    return {
        "id": page_id,
        "created_time": f"{day}T03:00:00.000Z",
        "last_edited_time": f"{day}T04:00:00.000Z",
        "url": f"https://notion.so/{page_id}",
        "properties": {
            "summary_ja": {"title": plain(headline)},
            "Status": {"select": {"name": "new"}},
            "Importance": {"number": 5},
            "Category": {"select": {"name": "HBM"}},
            "Source": {"rich_text": plain("Micron IR")},
            "URL": {"url": "https://example.com/primary"},
            "Sources": {
                "rich_text": plain("https://example.com/primary https://example.com/secondary")
            },
            "XPost": {
                "rich_text": plain(f"{summary}\n\n■見立て\n供給制約の緩和時期が焦点になる。")
            },
            "ImageURL": {"url": "https://example.com/image.jpg"},
            "PublishedAt": {"date": {"start": f"{day}T02:00:00+00:00"}},
            "Tickers": {"multi_select": [{"name": "mu"}, {"name": "NVDA"}]},
            "EventType": {"select": {"name": "supply_chain"}},
            "Sentiment": {"select": {"name": "positive"}},
            "Confidence": {"number": 0.85},
        },
    }


def make_settings(tmp_path: Path, **updates) -> Settings:
    values = {
        "db_path": tmp_path / "market.db",
        "live_db_path": tmp_path / "live.db",
        "corpus_local_dir": tmp_path / "corpus",
        "backup_s3_uri": "s3://example-bucket",
        "notion_token": "secret-test",
        "notion_db_id": "db-test",
        "auth_mode": "disabled",
    }
    values.update(updates)
    return Settings(**values)


def test_credentials_use_direct_values_without_ssm(tmp_path: Path):
    settings = make_settings(tmp_path)
    called: list[str] = []
    credentials = load_credentials(
        settings, parameter_loader=lambda name: called.append(name) or "unexpected"
    )
    assert credentials == NotionCredentials("secret-test", "db-test")
    assert called == []


def test_credentials_load_only_missing_values_from_exact_prefix(tmp_path: Path):
    settings = make_settings(tmp_path, notion_token=None)
    called: list[str] = []

    def load(name: str) -> str:
        called.append(name)
        return "from-ssm"

    credentials = load_credentials(settings, parameter_loader=load)
    assert credentials == NotionCredentials("from-ssm", "db-test")
    assert called == ["/teiten/notion-token"]


def test_normalize_page_extracts_analysis_fields():
    row = normalize_page(notion_page("page-1", "2026-07-30"))
    assert row["event_date"] == date(2026, 7, 30)
    assert row["headline"] == "Micron、HBM4増産へ"
    assert row["summary_ja"] == "MicronがHBM4の増産計画を発表した。"
    assert row["my_take"] == "供給制約の緩和時期が焦点になる。"
    assert row["tickers"] == ["MU", "NVDA"]
    assert row["event_type"] == "supply_chain"
    assert row["sentiment"] == "positive"
    assert row["confidence"] == 0.85
    assert row["sources"] == [
        "https://example.com/primary",
        "https://example.com/secondary",
    ]


def test_normalize_rejects_schema_drift():
    page = notion_page("page-1", "2026-07-30")
    page["properties"]["EventType"] = {"select": {"name": "unknown-type"}}
    try:
        normalize_page(page)
    except CorpusError as exc:
        assert "invalid EventType" in str(exc)
    else:
        raise AssertionError("unknown EventType should fail the sync")


def test_fetch_pages_paginates_and_sets_notion_version(tmp_path: Path):
    settings = make_settings(tmp_path)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "results": [notion_page("page-1", "2026-07-30")],
                    "has_more": True,
                    "next_cursor": "cursor-1",
                },
            )
        assert request.read().decode().find('"start_cursor":"cursor-1"') >= 0
        return httpx.Response(
            200,
            json={
                "results": [notion_page("page-2", "2026-07-31")],
                "has_more": False,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        pages = fetch_pages(
            client,
            settings,
            NotionCredentials("secret-test", "db-test"),
        )

    assert [page["id"] for page in pages] == ["page-1", "page-2"]
    assert requests[0].url.path == "/v1/databases/db-test/query"
    assert requests[0].headers["notion-version"] == "2022-06-28"
    assert requests[0].headers["authorization"] == "Bearer secret-test"


def test_run_uploads_only_changed_date_partitions(tmp_path: Path):
    settings = make_settings(tmp_path)
    pages = [
        notion_page("page-1", "2026-07-30"),
        notion_page("page-2", "2026-07-31"),
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": pages, "has_more": False})

    uploads: list[tuple[Path, str]] = []

    def uploader(path: Path, destination: str) -> None:
        assert path.exists()
        uploads.append((path, destination))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert run(settings, uploader=uploader, client=client) == 0
        assert run(settings, uploader=uploader, client=client) == 0

    assert [destination for _, destination in uploads] == [
        "s3://example-bucket/corpus/news/date=2026-07-30/part.parquet",
        "s3://example-bucket/corpus/news/date=2026-07-31/part.parquet",
    ]
    partition = tmp_path / "corpus" / "news" / "date=2026-07-30" / "part.parquet"
    rows = read_parquet_rows(partition)
    assert len(rows) == 1
    assert rows[0]["page_id"] == "page-1"
    assert rows[0]["tickers"] == ["MU", "NVDA"]


def test_archived_last_page_rewrites_partition_as_empty(tmp_path: Path):
    settings = make_settings(tmp_path)
    responses = [[notion_page("page-1", "2026-07-30")], []]
    uploads: list[tuple[Path, str]] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": responses.pop(0), "has_more": False})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        run(settings, uploader=lambda p, d: uploads.append((p, d)), client=client)
        run(settings, uploader=lambda p, d: uploads.append((p, d)), client=client)

    assert len(uploads) == 2
    assert read_parquet_rows(uploads[-1][0]) == []


def test_check_reports_every_bad_page_instead_of_stopping_at_the_first(
    tmp_path: Path, caplog
):
    """The reason --check exists: run() dies on page two and never sees page three.

    Fixing the writing side needs the whole list, not one example at a time.
    """
    settings = make_settings(tmp_path)
    good = notion_page("page-1", "2026-07-30")
    bad_type = notion_page("page-2", "2026-07-30")
    bad_type["properties"]["EventType"] = {"select": {"name": "決算"}}
    bad_confidence = notion_page("page-3", "2026-07-30")
    bad_confidence["properties"]["Confidence"] = {"number": 85}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"results": [good, bad_type, bad_confidence], "has_more": False},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with caplog.at_level("INFO"):
            exit_code = check(settings, client=client)

    assert exit_code == 1
    assert "3 page(s), 1 accepted, 2 rejected" in caplog.text
    assert "page-2" in caplog.text and "invalid EventType" in caplog.text
    assert "page-3" in caplog.text and "Confidence outside 0..1" in caplog.text
    # Nothing was written: a check must be safe to run against production.
    assert not (tmp_path / "corpus" / "news").exists()


def test_check_flags_tickers_that_will_never_join_a_price_series(tmp_path: Path, caplog):
    settings = make_settings(tmp_path)
    page = notion_page("page-1", "2026-07-30")
    page["properties"]["Tickers"] = {"multi_select": [{"name": "MU"}, {"name": "GOOG"}]}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [page], "has_more": False})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with caplog.at_level("INFO"):
            exit_code = check(settings, client=client)

    # GOOG parses as a ticker but the universe holds GOOGL, so it is a silent
    # miss rather than a rejection.
    assert exit_code == 0
    assert "GOOG=1" in caplog.text
    assert "outside universe.csv" in caplog.text


def test_check_still_validates_when_the_universe_file_is_absent(tmp_path: Path, caplog):
    """The news unit does not set USSTOCKS_CORPUS_UNIVERSE_PATH, so on a release
    the default points at a path that does not exist. Losing one report is fine;
    losing the check is not."""
    settings = make_settings(tmp_path, corpus_universe_path=tmp_path / "absent.csv")
    bad = notion_page("page-1", "2026-07-30")
    bad["properties"]["Sentiment"] = {"select": {"name": "強気"}}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [bad], "has_more": False})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with caplog.at_level("INFO"):
            exit_code = check(settings, client=client)

    assert exit_code == 1
    assert "invalid Sentiment" in caplog.text
    assert "skipping the out-of-universe ticker report" in caplog.text
