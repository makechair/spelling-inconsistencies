"""Adjusted daily corpus: selection, correction handling and Parquet output."""

from __future__ import annotations

import csv
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx

from usstocks.config import Settings
from usstocks.corpus.daily import (
    CorpusError,
    UniverseEntry,
    _parse_s3_destination,
    aws_upload,
    in_closed_market_window,
    load_state,
    load_universe,
    merge_rows,
    needs_full_refresh,
    read_parquet_rows,
    run,
    select_entries,
    select_target_entries,
)


def daily_row(day: str, *, close: float, dividend: float = 0.0, split: float = 1.0):
    return {
        "date": f"{day}T00:00:00.000Z",
        "open": close - 1,
        "high": close + 1,
        "low": close - 2,
        "close": close,
        "volume": 1000,
        "adjOpen": close - 1,
        "adjHigh": close + 1,
        "adjLow": close - 2,
        "adjClose": close,
        "adjVolume": 1000,
        "divCash": dividend,
        "splitFactor": split,
    }


def write_universe(path: Path, entries: list[tuple[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["symbol", "subsector"])
        writer.writerows(entries)


def test_repository_universe_has_50_unique_symbols():
    path = Path(__file__).resolve().parents[1] / "data" / "universe.csv"
    entries = load_universe(path)
    assert len(entries) == 50
    assert len({entry.symbol for entry in entries}) == 50
    assert "SKHY" not in {entry.symbol for entry in entries}


def test_closed_market_window_is_09_to_17_jst():
    assert in_closed_market_window(datetime(2026, 7, 31, 0, 0, tzinfo=UTC))
    assert in_closed_market_window(datetime(2026, 7, 31, 7, 59, tzinfo=UTC))
    assert not in_closed_market_window(datetime(2026, 7, 31, 8, 0, tzinfo=UTC))


def test_new_symbols_are_capped_and_existing_symbols_rotate(tmp_path: Path):
    entries = [
        UniverseEntry("NEW1", "logic"),
        UniverseEntry("NEW2", "logic"),
        UniverseEntry("OLD1", "memory"),
        UniverseEntry("OLD2", "memory"),
    ]
    for symbol in ("OLD1", "OLD2"):
        path = tmp_path / "daily" / f"symbol={symbol}" / "part.parquet"
        path.parent.mkdir(parents=True)
        path.touch()
    now = datetime(2026, 7, 31, 3, 0, tzinfo=UTC)
    state = {
        "version": 1,
        "symbols": {
            "OLD1": {"last_success_utc": (now - timedelta(days=1)).isoformat()},
            "OLD2": {"last_success_utc": (now - timedelta(days=2)).isoformat()},
        },
    }

    selected = select_entries(
        entries,
        state,
        tmp_path,
        now=now,
        max_symbols=3,
        max_new_symbols=1,
        refresh_hours=20,
    )

    assert [entry.symbol for entry in selected] == ["NEW1", "OLD2", "OLD1"]


def test_target_symbols_preserve_request_order_and_require_universe_membership():
    entries = [
        UniverseEntry("NVDA", "logic"),
        UniverseEntry("ARM", "eda_ip"),
    ]

    selected = select_target_entries(
        entries,
        ["arm", "ARM"],
        max_symbols=1,
        max_new_symbols=1,
        existing_symbols=set(),
    )

    assert selected == [UniverseEntry("ARM", "eda_ip")]
    for symbols, expected in ((["TSM"], "not in universe"), (["ARM", "NVDA"], "exceeds")):
        try:
            select_target_entries(
                entries,
                symbols,
                max_symbols=1,
                max_new_symbols=1,
                existing_symbols=set(),
            )
        except CorpusError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError("invalid explicit target should fail")
    try:
        select_target_entries(
            entries,
            ["ARM", "NVDA"],
            max_symbols=2,
            max_new_symbols=1,
            existing_symbols=set(),
        )
    except CorpusError as exc:
        assert "max new symbols" in str(exc)
    else:
        raise AssertionError("explicit targets must keep the new-symbol cap")


def test_new_corporate_action_requires_full_adjustment_refresh():
    existing = [
        {
            "date": date(2026, 7, 29),
            "splitFactor": 1.0,
            "divCash": 0.0,
        }
    ]
    ordinary = [
        {
            "date": date(2026, 7, 30),
            "splitFactor": 1.0,
            "divCash": 0.0,
        }
    ]
    dividend = [
        {
            "date": date(2026, 7, 30),
            "splitFactor": 1.0,
            "divCash": 0.25,
        }
    ]

    assert needs_full_refresh(existing, ordinary) is False
    assert needs_full_refresh(existing, dividend) is True
    assert needs_full_refresh(existing + dividend, dividend) is False


def test_merge_replaces_overlapping_dates():
    existing = [{"date": date(2026, 7, 29), "close": 1.0}]
    incoming = [
        {"date": date(2026, 7, 29), "close": 2.0},
        {"date": date(2026, 7, 30), "close": 3.0},
    ]
    assert merge_rows(existing, incoming) == incoming


def test_s3_destination_requires_bucket_and_key():
    assert _parse_s3_destination("s3://example/corpus/a.parquet") == (
        "example",
        "corpus/a.parquet",
    )
    for invalid in ("https://example/corpus/a.parquet", "s3://example", "s3:///a.parquet"):
        try:
            _parse_s3_destination(invalid)
        except CorpusError:
            pass
        else:
            raise AssertionError(f"destination should fail: {invalid}")


def test_aws_upload_uses_boto3_and_sse(tmp_path: Path, monkeypatch):
    local_path = tmp_path / "part.parquet"
    local_path.write_bytes(b"parquet")
    calls: list[dict[str, object]] = []

    class FakeS3:
        def put_object(self, **kwargs):
            kwargs["Body"] = kwargs["Body"].read()
            calls.append(kwargs)

    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=lambda service: FakeS3()))
    aws_upload(local_path, "s3://example/corpus/daily/symbol=NVDA/part.parquet")

    assert calls == [
        {
            "Bucket": "example",
            "Key": "corpus/daily/symbol=NVDA/part.parquet",
            "Body": b"parquet",
            "ServerSideEncryption": "AES256",
        }
    ]


def test_run_writes_partition_and_uploads_without_real_network(tmp_path: Path):
    universe_path = tmp_path / "universe.csv"
    write_universe(universe_path, [("NVDA", "logic"), ("AMD", "logic")])
    settings = Settings(
        db_path=tmp_path / "market.db",
        live_db_path=tmp_path / "live.db",
        tiingo_api_key="test-key",
        corpus_universe_path=universe_path,
        corpus_local_dir=tmp_path / "corpus",
        backup_s3_uri="s3://example-bucket",
        auth_mode="disabled",
    )
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(
            200,
            json=[
                daily_row("2026-07-29", close=100.0),
                daily_row("2026-07-30", close=102.0),
            ],
        )

    uploads: list[tuple[Path, str]] = []

    def uploader(path: Path, destination: str) -> None:
        assert path.exists()
        uploads.append((path, destination))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run(
            settings,
            now=datetime(2026, 7, 31, 3, 0, tzinfo=UTC),
            max_symbols=1,
            max_new_symbols=1,
            uploader=uploader,
            client=client,
        )

    partition = tmp_path / "corpus" / "daily" / "symbol=NVDA" / "part.parquet"
    assert result == 0
    assert requested == ["/tiingo/daily/nvda/prices"]
    assert len(read_parquet_rows(partition)) == 2
    assert [destination for _, destination in uploads] == [
        "s3://example-bucket/corpus/universe/sectors.parquet",
        "s3://example-bucket/corpus/daily/symbol=NVDA/part.parquet",
    ]
    state = load_state(tmp_path / "corpus" / "state.json")
    assert state["symbols"]["NVDA"]["pending_upload"] is False
    assert state["symbols"]["NVDA"]["last_bar_date"] == "2026-07-30"


def test_run_rejects_invalid_symbol_limits(tmp_path: Path):
    universe_path = tmp_path / "universe.csv"
    write_universe(universe_path, [("NVDA", "logic")])
    settings = Settings(
        db_path=tmp_path / "market.db",
        live_db_path=tmp_path / "live.db",
        tiingo_api_key="test-key",
        corpus_universe_path=universe_path,
        corpus_local_dir=tmp_path / "corpus",
        backup_s3_uri="s3://example-bucket",
        auth_mode="disabled",
    )

    for max_symbols, max_new_symbols in ((0, 0), (1, -1), (1, 2)):
        try:
            run(
                settings,
                now=datetime(2026, 7, 31, 3, 0, tzinfo=UTC),
                max_symbols=max_symbols,
                max_new_symbols=max_new_symbols,
            )
        except CorpusError:
            pass
        else:
            raise AssertionError(f"limits should fail: max={max_symbols}, new={max_new_symbols}")


def test_run_stops_fetching_after_daily_upload_failure(tmp_path: Path):
    universe_path = tmp_path / "universe.csv"
    write_universe(universe_path, [("NVDA", "logic"), ("AMD", "logic")])
    settings = Settings(
        db_path=tmp_path / "market.db",
        live_db_path=tmp_path / "live.db",
        tiingo_api_key="test-key",
        corpus_universe_path=universe_path,
        corpus_local_dir=tmp_path / "corpus",
        backup_s3_uri="s3://example-bucket",
        auth_mode="disabled",
    )
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(200, json=[daily_row("2026-07-30", close=102.0)])

    def uploader(_path: Path, destination: str) -> None:
        if "/daily/" in destination:
            raise CorpusError("S3 unavailable")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run(
            settings,
            now=datetime(2026, 7, 31, 3, 0, tzinfo=UTC),
            max_symbols=2,
            max_new_symbols=2,
            uploader=uploader,
            client=client,
        )

    assert result == 1
    assert requested == ["/tiingo/daily/nvda/prices"]
    state = load_state(tmp_path / "corpus" / "state.json")
    assert state["symbols"]["NVDA"]["pending_upload"] is True
