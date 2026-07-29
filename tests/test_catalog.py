"""Local ticker catalog import and search (spec-review A-2)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import httpx
import pytest

from usstocks import catalog
from usstocks.config import Settings
from usstocks.db.repository import Repository

HEADER = "ticker,exchange,assetType,priceCurrency,startDate,endDate,name"


def make_archive(rows: list[str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("supported_tickers.csv", "\n".join([HEADER, *rows]) + "\n")
    return buffer.getvalue()


def serve(payload: bytes, monkeypatch) -> None:
    """Replace the download with a mock transport carrying `payload`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    original = httpx.stream

    def fake_stream(method, url, **kwargs):
        kwargs.pop("timeout", None)
        kwargs.pop("follow_redirects", None)
        client = httpx.Client(transport=httpx.MockTransport(handler))
        return client.stream(method, url, **kwargs)

    monkeypatch.setattr(httpx, "stream", fake_stream)
    assert original is not httpx.stream


ACTIVE = "AAPL,NASDAQ,Stock,USD,1980-12-12,2099-01-01,Apple Inc"
ETF = "SPY,NYSE ARCA,ETF,USD,1993-01-29,2099-01-01,SPDR S&P 500"
DELISTED = "OLDCO,NASDAQ,Stock,USD,1990-01-01,1999-12-31,Defunct Corp"
FOREIGN = "SAP,XETR,Stock,EUR,2000-01-01,2099-01-01,SAP SE"
FUND = "MUAIX,NASDAQ,Mutual Fund,USD,2000-01-01,2099-01-01,Ultra Short Income"


def test_import_keeps_tradeable_us_listings(settings: Settings, monkeypatch):
    serve(make_archive([ACTIVE, ETF, DELISTED, FOREIGN, FUND]), monkeypatch)

    kept = catalog.import_catalog(settings)

    assert kept == 2
    with Repository(settings.db_path) as repo:
        symbols = {info.symbol for info in repo.search_catalog("", limit=50)} or {
            info.symbol for info in repo.search_catalog("A", limit=50)
        }
    # Delisted, non-USD and fund rows are dropped: they cannot be charted here
    # and would bury live symbols in the picker.
    assert "OLDCO" not in symbols
    assert "SAP" not in symbols
    assert "MUAIX" not in symbols


def test_import_deletes_the_archive(settings: Settings, monkeypatch, tmp_path: Path):
    serve(make_archive([ACTIVE]), monkeypatch)
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))

    catalog.import_catalog(settings)

    # Disk is the scarce resource on the target host, and this is the largest
    # thing the process touches.
    assert list(tmp_path.glob("usstocks-catalog-*.zip")) == []


def test_a_failed_import_leaves_the_archive_behind_nowhere(
    settings: Settings, monkeypatch, tmp_path: Path
):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    serve(make_archive([DELISTED]), monkeypatch)

    # Every row filtered out: treated as a bad file rather than an empty
    # universe, so the previous catalog survives.
    with pytest.raises(RuntimeError):
        catalog.import_catalog(settings)

    assert list(tmp_path.glob("usstocks-catalog-*.zip")) == []


def test_an_empty_import_does_not_wipe_a_working_catalog(settings: Settings, monkeypatch):
    serve(make_archive([ACTIVE, ETF]), monkeypatch)
    catalog.import_catalog(settings)

    serve(make_archive([]), monkeypatch)
    with pytest.raises(RuntimeError):
        catalog.import_catalog(settings)

    with Repository(settings.db_path) as repo:
        assert repo.catalog_size() == 2


def test_import_prunes_symbols_that_left_the_universe(settings: Settings, monkeypatch):
    serve(make_archive([ACTIVE, ETF]), monkeypatch)
    catalog.import_catalog(settings)

    serve(make_archive([ACTIVE]), monkeypatch)
    catalog.import_catalog(settings)

    with Repository(settings.db_path) as repo:
        assert repo.catalog_size() == 1
        assert [info.symbol for info in repo.search_catalog("SPY")] == []


def test_search_ranks_an_exact_ticker_first(settings: Settings, monkeypatch):
    rows = [
        ACTIVE,
        "MU,NASDAQ,Stock,USD,1984-01-01,2099-01-01,Micron Technology Inc",
        "MUA,NYSE,Stock,USD,2000-01-01,2099-01-01,BlackRock MuniAssets Fund Inc",
        "MUSA,NYSE,Stock,USD,2013-01-01,2099-01-01,Murphy USA Inc",
    ]
    serve(make_archive(rows), monkeypatch)
    catalog.import_catalog(settings)

    with Repository(settings.db_path) as repo:
        results = [info.symbol for info in repo.search_catalog("MU", limit=10)]

    # Typing a full ticker must not bury it under longer ones that merely start
    # with the same letters -- what the provider's own ordering did.
    assert results[0] == "MU"
