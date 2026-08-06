"""Sector decomposition, and the per-symbol news coverage table.

Subtracting the peer average outright assumes every member moves one for one
with its sector. A symbol that habitually moves twice as far would then be
reported as beating its sector on every up day and lagging it on every down
day -- an artefact of the assumption, not news. These tests build exactly that
symbol and check the artefact is gone.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from usstocks.config import Settings
from usstocks.corpus.daily import UniverseEntry, write_daily_parquet, write_universe_parquet
from usstocks.corpus.event_study import run
from usstocks.corpus.news import write_partition

pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

# A repeating factor, so the series has variance and every figure below is
# reproducible without a random seed.
FACTOR = [0.01, -0.008, 0.005, -0.003, 0.012, -0.011, 0.004, -0.002]
BETA = 2.0
SESSIONS = 200
# Far enough in that the 120-day estimation window is full, and clear of the
# 20-day forward horizon at the end.
EVENT_INDEX = 170


def sessions(start: date, count: int) -> list[date]:
    days: list[date] = []
    current = start
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def closes(multiplier: float) -> list[float]:
    price = 100.0
    series = [price]
    for index in range(SESSIONS - 1):
        price *= 1 + FACTOR[index % len(FACTOR)] * multiplier
        series.append(price)
    return series


def daily_rows(symbol: str, days: list[date], prices: list[float]) -> list[dict[str, object]]:
    return [
        {
            "symbol": symbol,
            "date": day,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 1000,
            "adjOpen": close,
            "adjHigh": close,
            "adjLow": close,
            "adjClose": close,
            "adjVolume": 1000.0,
            "divCash": 0.0,
            "splitFactor": 1.0,
        }
        for day, close in zip(days, prices, strict=True)
    ]


def news_row(page_id: str, ticker: str, event_date: date) -> dict[str, object]:
    return {
        "page_id": page_id,
        "event_date": event_date,
        "published_at": f"{event_date.isoformat()}T13:00:00+00:00",
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
        "event_type": "product",
        "sentiment": "positive",
        "confidence": 0.9,
        "notion_url": f"https://notion.so/{page_id}",
    }


@pytest.fixture
def report(tmp_path: Path) -> dict:
    corpus = tmp_path / "corpus"
    days = sessions(date(2025, 1, 2), SESSIONS)
    entries = [
        UniverseEntry("HIGH", "logic_compute"),
        UniverseEntry("PEER1", "logic_compute"),
        UniverseEntry("PEER2", "logic_compute"),
        UniverseEntry("PEER3", "logic_compute"),
        # Priced, in the universe, and never written about.
        UniverseEntry("QUIET", "logic_compute"),
    ]
    write_universe_parquet(corpus / "universe" / "sectors.parquet", entries)
    for entry in entries:
        # The peers are the factor itself, so their average is the factor and
        # the subject's beta against it is exactly BETA.
        multiplier = BETA if entry.symbol == "HIGH" else 1.0
        write_daily_parquet(
            corpus / "daily" / f"symbol={entry.symbol}" / "part.parquet",
            daily_rows(entry.symbol, days, closes(multiplier)),
        )

    event_day = days[EVENT_INDEX]
    write_partition(
        corpus / "news" / f"date={event_day.isoformat()}" / "part.parquet",
        [
            news_row("page-high", "HIGH", event_day),
            # A ticker with articles and no price series at all.
            news_row("page-ghost", "ZZZZ", event_day),
        ],
    )

    settings = Settings(
        db_path=tmp_path / "market.db",
        live_db_path=tmp_path / "live.db",
        corpus_local_dir=corpus,
        backup_s3_uri="s3://example-bucket",
        auth_mode="disabled",
    )
    run(
        settings,
        uploader=lambda path, destination: None,
        now=datetime(2026, 1, 5, 8, 0, tzinfo=UTC),
    )
    return json.loads(
        (corpus / "analysis" / "latest" / "report.json").read_text(encoding="utf-8")
    )


def event_of(report: dict, symbol: str) -> dict:
    for case in report["case_studies"]:
        if case["symbol"] == symbol:
            return case
    raise AssertionError(f"no case study for {symbol}")


def test_the_estimated_beta_is_the_ratio_the_prices_show(report: dict):
    case = event_of(report, "HIGH")
    assert case["beta"] == pytest.approx(BETA, abs=0.01)
    assert case["beta_source"] == "estimated"
    assert case["beta_observations"] >= 60


def test_a_move_the_beta_explains_is_not_reported_as_abnormal(report: dict):
    """The whole point. HIGH moved twice as far as its sector because it
    always does, and nothing about the day was unusual."""
    case = event_of(report, "HIGH")
    assert case["abnormal_return_0d"] == pytest.approx(0.0, abs=1e-9)


def test_multi_day_windows_keep_only_the_compounding_remainder(report: dict):
    """A beta estimated on daily returns is applied to a compounded window,
    so a second-order term survives: twice a two-day return is not the
    two-day return of twice the moves. It is an order of magnitude below the
    artefact it replaces, and it does not grow with the sector's direction."""
    case = event_of(report, "HIGH")
    assert abs(case["abnormal_return_1d"]) < 0.0002
    assert abs(case["abnormal_return_1d"]) < abs(case["equal_weight_abnormal_return_1d"]) / 10


def test_the_previous_definition_is_kept_and_shows_the_artefact(report: dict):
    """Same event, old arithmetic: a full sector move reported as company
    news. Kept so a changed number can be traced to the change."""
    case = event_of(report, "HIGH")
    assert abs(case["equal_weight_abnormal_return_1d"]) > 0.001
    assert case["equal_weight_abnormal_return_1d"] == pytest.approx(
        case["raw_return_1d"] - case["exploratory_benchmark_return_1d"], abs=1e-9
    )


def test_the_sector_share_of_the_move_is_reported(report: dict):
    """The decomposition has to add up, or it is two numbers rather than a
    split of one."""
    case = event_of(report, "HIGH")
    assert case["sector_attributed_return_1d"] + case["abnormal_return_1d"] == pytest.approx(
        case["raw_return_1d"], abs=1e-9
    )


def test_a_symbol_nobody_writes_about_is_named_as_such(report: dict):
    coverage = {row["symbol"]: row for row in report["symbol_news_coverage"]}
    assert coverage["QUIET"]["events"] == 0
    assert coverage["QUIET"]["diagnosis"] == "no_articles"


def test_articles_that_never_reach_a_price_series_are_counted_apart(report: dict):
    """A ticker with news and no bars is a different problem from a ticker
    with no news, and summing them into one coverage number hides both."""
    coverage = {row["symbol"]: row for row in report["symbol_news_coverage"]}
    ghost = coverage["ZZZZ"]
    assert ghost["events"] == 1
    assert ghost["matched_events"] == 0
    assert ghost["no_price_series"] == 1
    assert ghost["diagnosis"] == "no_price_series"
    assert ghost["attach_rate"] == 0


def test_a_short_price_history_is_reported_as_the_reason(report: dict):
    """Every article connected, and the conditional surfaces will still be
    mostly blank: with under a year of sessions the overlap correction
    divides an already small sample by the horizon. A recently listed or
    recently spun-off symbol needs time, not more news, and the table has to
    say which."""
    coverage = {row["symbol"]: row for row in report["symbol_news_coverage"]}
    assert coverage["HIGH"]["attach_rate"] == pytest.approx(1.0)
    assert coverage["HIGH"]["sessions"] == SESSIONS
    assert coverage["HIGH"]["diagnosis"] == "short_price_history"
