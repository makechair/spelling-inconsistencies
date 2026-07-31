"""Phase 3 event-time alignment, DuckDB returns and report outputs."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from usstocks.config import Settings
from usstocks.corpus.daily import (
    CorpusError,
    UniverseEntry,
    read_parquet_rows,
    write_daily_parquet,
    write_universe_parquet,
)
from usstocks.corpus.event_study import classify_event_time, run
from usstocks.corpus.news import write_partition


def _sessions(start: date, count: int) -> list[date]:
    sessions: list[date] = []
    current = start
    while len(sessions) < count:
        if current.weekday() < 5:
            sessions.append(current)
        current += timedelta(days=1)
    return sessions


def _daily_rows(symbol: str, sessions: list[date], step: float) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index, day in enumerate(sessions):
        close = 100.0 + step * index
        rows.append(
            {
                "symbol": symbol,
                "date": day,
                "open": close - 0.5,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 1000,
                "adjOpen": close - 0.5,
                "adjHigh": close + 1.0,
                "adjLow": close - 1.0,
                "adjClose": close,
                "adjVolume": 1000.0,
                "divCash": 0.0,
                "splitFactor": 1.0,
            }
        )
    return rows


def _news_row(
    page_id: str,
    ticker: str,
    *,
    event_date: date,
    published_at: str | None,
    event_type: str,
) -> dict[str, object]:
    return {
        "page_id": page_id,
        "event_date": event_date,
        "published_at": published_at,
        "created_at": f"{event_date.isoformat()}T12:00:00+00:00",
        "last_edited_at": f"{event_date.isoformat()}T13:00:00+00:00",
        "headline": f"{ticker} event",
        "summary_ja": "事実要約",
        "my_take": "見立て",
        "status": "new",
        "importance": 4,
        "category": "logic",
        "source": "IR",
        "url": f"https://example.com/{page_id}",
        "sources": [f"https://example.com/{page_id}"],
        "image_url": None,
        "tickers": [ticker],
        "event_type": event_type,
        "sentiment": "positive",
        "confidence": 0.9,
        "notion_url": f"https://notion.so/{page_id}",
    }


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "market.db",
        live_db_path=tmp_path / "live.db",
        corpus_local_dir=tmp_path / "corpus",
        backup_s3_uri="s3://example-bucket",
        auth_mode="disabled",
    )


def _write_inputs(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    sessions = _sessions(date(2026, 7, 9), 28)
    entries = [
        UniverseEntry("NVDA", "logic_compute"),
        UniverseEntry("AMD", "logic_compute"),
        UniverseEntry("INTC", "logic_compute"),
        UniverseEntry("AVGO", "logic_compute"),
    ]
    write_universe_parquet(corpus / "universe" / "sectors.parquet", entries)
    for entry in entries:
        step = 2.0 if entry.symbol == "NVDA" else 1.0
        write_daily_parquet(
            corpus / "daily" / f"symbol={entry.symbol}" / "part.parquet",
            _daily_rows(entry.symbol, sessions, step),
        )

    friday = date(2026, 7, 10)
    saturday = date(2026, 7, 11)
    write_partition(
        corpus / "news" / f"date={friday.isoformat()}" / "part.parquet",
        [
            _news_row(
                "page-nvda-1",
                "NVDA",
                event_date=friday,
                published_at="2026-07-10T20:30:00+00:00",
                event_type="product",
            ),
            _news_row(
                "page-amd-1",
                "AMD",
                event_date=friday,
                published_at="2026-07-10T20:30:00+00:00",
                event_type="earnings",
            ),
            _news_row(
                "page-nvda-3",
                "NVDA",
                event_date=date(2026, 7, 15),
                published_at="2026-07-15T12:00:00+00:00",
                event_type="guidance",
            ),
            _news_row(
                "page-unknown",
                "ZZZZ",
                event_date=friday,
                published_at="2026-07-10T20:30:00+00:00",
                event_type="other",
            ),
        ],
    )
    write_partition(
        corpus / "news" / f"date={saturday.isoformat()}" / "part.parquet",
        [
            _news_row(
                "page-nvda-2",
                "NVDA",
                event_date=saturday,
                published_at=None,
                event_type="product",
            )
        ],
    )


def test_event_time_uses_new_york_close_boundary():
    assert classify_event_time(
        "2026-07-13T13:00:00+00:00",
        date(2026, 7, 13),
    ) == (date(2026, 7, 13), "timestamped", "pre_market")
    assert classify_event_time(
        "2026-07-13T19:59:59+00:00",
        date(2026, 7, 13),
    ) == (date(2026, 7, 13), "timestamped", "regular")
    assert classify_event_time(
        "2026-07-13T20:00:00+00:00",
        date(2026, 7, 13),
    ) == (date(2026, 7, 14), "timestamped", "after_hours")
    assert classify_event_time(None, date(2026, 7, 11)) == (
        date(2026, 7, 11),
        "date_only",
        "date_only",
    )
    assert classify_event_time(
        "2026-07-13T14:00:00",
        date(2026, 7, 13),
    ) == (date(2026, 7, 13), "date_only", "date_only")


def test_event_study_writes_returns_summary_unmatched_and_reports(tmp_path: Path):
    _write_inputs(tmp_path)
    settings = _settings(tmp_path)
    uploads: list[tuple[Path, str]] = []

    assert (
        run(
            settings,
            uploader=lambda path, destination: uploads.append((path, destination)),
            now=datetime(2026, 7, 31, 8, 0, tzinfo=UTC),
        )
        == 0
    )

    output = tmp_path / "corpus" / "analysis" / "latest"
    returns = read_parquet_rows(output / "event_returns.parquet")
    summary = read_parquet_rows(output / "event_summary.parquet")
    unmatched = read_parquet_rows(output / "event_unmatched.parquet")

    assert len(returns) == 4
    nvda = [row for row in returns if row["symbol"] == "NVDA"]
    product = [row for row in nvda if row["event_type"] == "product"]
    guidance = next(row for row in nvda if row["event_type"] == "guidance")
    assert {row["reaction_date"] for row in product} == {date(2026, 7, 13)}
    assert {row["event_group_size"] for row in product} == {2}
    assert {row["event_weight"] for row in product} == {0.5}
    assert {row["overlap_count"] for row in product} == {1}
    assert guidance["overlap_count"] == 1
    assert all(row["peer_count_0d"] == 3 for row in nvda)
    assert all(row["raw_return_0d"] is not None for row in nvda)
    assert all(row["abnormal_return_0d"] is not None for row in nvda)

    amd = next(row for row in returns if row["symbol"] == "AMD")
    assert amd["overlap_count"] == 0
    assert any(
        row["sample"] == "non_overlapping"
        and row["dimension"] == "event_type"
        and row["group_value"] == "earnings"
        and row["metric"] == "abnormal_return"
        for row in summary
    )
    overall_raw_0d = next(
        row
        for row in summary
        if row["sample"] == "all"
        and row["dimension"] == "all"
        and row["metric"] == "raw_return"
        and row["horizon"] == 0
    )
    assert overall_raw_0d["events"] == 4
    assert overall_raw_0d["effective_events"] == 3.0

    assert len(unmatched) == 1
    assert unmatched[0]["symbol"] == "ZZZZ"
    assert unmatched[0]["reason"] == "symbol_not_in_daily_corpus"
    assert "Notionイベント × 株価変動" in (output / "report.md").read_text()
    assert "<!doctype html>" in (output / "report.html").read_text()
    assert (output / "manifest.json").exists()
    assert uploads[-1][1].endswith("/analysis/latest/manifest.json")

    upload_count = len(uploads)
    run(
        settings,
        uploader=lambda path, destination: uploads.append((path, destination)),
        now=datetime(2026, 7, 31, 9, 0, tzinfo=UTC),
    )
    assert len(uploads) == upload_count


def test_event_study_requires_all_three_input_families(tmp_path: Path):
    try:
        run(_settings(tmp_path), upload=False)
    except CorpusError as exc:
        assert "event study input is missing" in str(exc)
    else:
        raise AssertionError("missing corpus inputs should fail")
