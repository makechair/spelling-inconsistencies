"""API surface: routes, auth gate, SSE framing."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from usstocks.adapters.mock import MockAdapter
from usstocks.api.app import create_app
from usstocks.config import Settings
from usstocks.db.live_store import LiveStore
from usstocks.db.repository import Repository
from usstocks.models import Bar, CollectorStatus, LiveSnapshot, Session, SymbolInfo

BASE = datetime(2026, 7, 27, 14, 30, tzinfo=UTC)


@pytest.fixture
def client(settings: Settings):
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def seed_bars(repo: Repository, count: int = 5, symbol: str = "AAPL") -> None:
    repo.upsert_symbol(SymbolInfo(symbol=symbol, name="Apple Inc.", is_watched=True))
    for minute in range(count):
        repo.upsert_bar(
            Bar(
                symbol=symbol,
                timestamp=BASE + timedelta(minutes=minute),
                session=Session.REGULAR,
                open=100.0 + minute,
                high=101.0 + minute,
                low=99.0 + minute,
                close=100.5 + minute,
                volume=1000 + minute,
                vwap=100.4 + minute,
                trade_count=12,
                source="tiingo",
                is_final=True,
                received_at=BASE,
            )
        )


# ------------------------------------------------------------------- routes
def test_health_reports_collector_absence(client: TestClient):
    payload = client.get("/api/health").json()
    assert payload["status"] in {"degraded", "down"}
    assert "collector_status_stale" in payload["problems"]
    assert payload["collector"]["alive"] is False
    # The budget surfaces that the spec never tracked (spec-review A-1/A-2).
    assert "month_to_date_bytes" in payload["bandwidth"]
    assert payload["rest_budget"]["hourly_limit"] == 50


def test_health_reports_a_live_collector(client: TestClient, settings: Settings):
    with LiveStore(settings.live_db_path) as store:
        store.write_status(
            CollectorStatus(
                source="mock",
                connected=True,
                last_trade_at=datetime.now(tz=UTC),
                subscribed_symbols=["AAPL"],
            )
        )
    payload = client.get("/api/health").json()
    assert payload["collector"]["alive"] is True
    assert payload["collector"]["connected"] is True
    assert payload["collector"]["subscribed_symbols"] == ["AAPL"]


def test_bars_endpoint(client: TestClient, settings: Settings):
    with Repository(settings.db_path) as repo:
        seed_bars(repo)

    payload = client.get(
        "/api/bars/AAPL",
        params={"start": BASE.isoformat(), "end": (BASE + timedelta(minutes=5)).isoformat()},
    ).json()

    assert payload["count"] == 5
    assert payload["truncated"] is False
    first = payload["bars"][0]
    assert first["time"] == int(BASE.timestamp())
    assert first["source"] == "tiingo"
    assert first["session"] == "regular"


def test_truncated_bars_keep_the_newest_edge(settings: Settings):
    limited = settings.model_copy(update={"max_bars_per_request": 3})
    with Repository(limited.db_path) as repo:
        seed_bars(repo, count=5)

    with TestClient(create_app(limited)) as test_client:
        payload = test_client.get(
            "/api/bars/AAPL",
            params={
                "start": BASE.isoformat(),
                "end": (BASE + timedelta(minutes=5)).isoformat(),
            },
        ).json()

    assert payload["truncated"] is True
    assert [bar["time"] for bar in payload["bars"]] == [
        int((BASE + timedelta(minutes=minute)).timestamp()) for minute in (2, 3, 4)
    ]


def test_bars_report_the_last_fetch_even_when_the_range_is_empty(
    client: TestClient, settings: Settings
):
    """The case the field exists for: a completed fetch that found nothing.

    An after-hours session with no prints and a collector that died at the
    close both leave the newest bar frozen. Only the fetch time separates them,
    so it has to survive a response carrying zero bars.
    """
    checked = BASE + timedelta(hours=3)
    with Repository(settings.db_path) as repo:
        seed_bars(repo)
        # Recorded under the configured primary source: the standby provider's
        # state would say nothing about whether the screen is being kept fresh.
        repo.record_state("AAPL", settings.primary_source, last_backfill=checked)

    payload = client.get(
        "/api/bars/AAPL",
        params={
            "start": (BASE + timedelta(days=1)).isoformat(),
            "end": (BASE + timedelta(days=2)).isoformat(),
        },
    ).json()

    assert payload["count"] == 0
    assert payload["checked_at"] is not None
    assert datetime.fromisoformat(payload["checked_at"]) == checked


def test_bars_report_no_fetch_before_the_collector_has_run(
    client: TestClient, settings: Settings
):
    with Repository(settings.db_path) as repo:
        seed_bars(repo)

    payload = client.get("/api/bars/AAPL", params={"days": 1}).json()
    assert payload["checked_at"] is None


def test_bars_rejects_inverted_range(client: TestClient):
    response = client.get(
        "/api/bars/AAPL",
        params={"start": BASE.isoformat(), "end": (BASE - timedelta(days=1)).isoformat()},
    )
    assert response.status_code == 400


def test_symbol_lifecycle(client: TestClient):
    created = client.put(
        "/api/symbols/nvda",
        json={"symbol": "NVDA", "name": "NVIDIA Corporation", "is_watched": True},
    )
    assert created.status_code == 200
    assert created.json()["symbol"] == "NVDA"

    watched = client.get("/api/symbols", params={"watched_only": True}).json()
    assert [entry["symbol"] for entry in watched] == ["NVDA"]

    removed = client.delete("/api/symbols/NVDA")
    assert removed.status_code == 200
    assert removed.json()["is_watched"] is False
    assert client.get("/api/symbols", params={"watched_only": True}).json() == []


def test_symbol_rejects_garbage_tickers(client: TestClient):
    response = client.put("/api/symbols/..%2F..", json={"symbol": "../.."})
    assert response.status_code in {400, 404}


def test_search_falls_back_to_the_provider(client: TestClient):
    results = client.get("/api/symbols/search", params={"q": "NVDA"}).json()
    assert any(entry["symbol"] == "NVDA" for entry in results)


def test_search_deduplicates_provider_results(client: TestClient, monkeypatch):
    """Tiingo returns one row per listing, so a cross-listed ticker repeats.

    The mock adapter's universe is a dict and cannot produce duplicates, which
    is why the picker showed MU twice in production while this suite stayed
    green.
    """
    entry = {
        "symbol": "MU",
        "name": "Micron Technology Inc",
        "exchange": "NASDAQ",
        "asset_type": "Stock",
    }

    async def duplicated(self, query: str, limit: int = 20) -> list[dict[str, str]]:
        return [dict(entry), dict(entry)]

    from usstocks.api.routes import symbols as symbols_route

    symbols_route._search_cache.clear()

    monkeypatch.setattr(MockAdapter, "search_symbols", duplicated)

    results = client.get("/api/symbols/search", params={"q": "MU"}).json()
    assert [item["symbol"] for item in results].count("MU") == 1


def test_export_csv_streams_header_and_rows(client: TestClient, settings: Settings):
    with Repository(settings.db_path) as repo:
        seed_bars(repo, count=3)

    response = client.get(
        "/api/export/csv",
        params={"symbols": "AAPL", "start": BASE.isoformat(),
                "end": (BASE + timedelta(minutes=5)).isoformat()},
    )
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    lines = [line for line in response.text.splitlines() if line]
    assert lines[0].startswith("symbol,timestamp_utc,session")
    assert len(lines) == 4


def test_export_requires_symbols(client: TestClient):
    assert client.get("/api/export/csv", params={"symbols": " "}).status_code == 400


# ---------------------------------------------------------------------- SSE
def test_live_snapshot_and_stream(client: TestClient, settings: Settings):
    with LiveStore(settings.live_db_path) as store:
        store.publish(
            [
                LiveSnapshot(
                    symbol="AAPL",
                    last_price=101.5,
                    last_trade_at=BASE,
                    session=Session.REGULAR,
                    previous_close=100.0,
                    source="mock",
                    current_bar=Bar(
                        symbol="AAPL",
                        timestamp=BASE,
                        session=Session.REGULAR,
                        open=100.0, high=102.0, low=99.5, close=101.5,
                        volume=4200, source="mock", is_final=False,
                    ),
                )
            ]
        )

    payload = client.get("/api/live/snapshot", params={"symbols": "AAPL"}).json()
    entry = payload["symbols"][0]
    assert entry["last_price"] == 101.5
    assert entry["change"] == pytest.approx(1.5)
    assert entry["change_pct"] == pytest.approx(1.5)
    assert entry["bar"]["volume"] == 4200
    assert entry["staleness_seconds"] is not None

    # A short-lived stream so the test reads it to completion rather than
    # relying on disconnect detection.
    brief = settings.model_copy(
        update={"sse_max_stream_seconds": 0.3, "sse_interval_seconds": 0.05}
    )
    with TestClient(create_app(brief)) as streaming_client:
        with streaming_client.stream("GET", "/api/live", params={"symbols": "AAPL"}) as response:
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["x-accel-buffering"] == "no"
            assert "no-cache" in response.headers["cache-control"]
            lines = list(response.iter_lines())

    events = [line[len("event: "):] for line in lines if line.startswith("event: ")]
    assert events[0] == "snapshot"
    assert events[-1] == "cycle"

    payloads = [json.loads(line[len("data:"):]) for line in lines if line.startswith("data:")]
    assert payloads[0]["symbols"][0]["symbol"] == "AAPL"
    assert payloads[0]["symbols"][0]["bar"]["volume"] == 4200
    # Nothing changed in the live store during the stream, so no update event
    # was emitted -- the UI must never see invented movement (spec 3.3).
    assert "update" not in events


# --------------------------------------------------------------------- auth
def test_every_route_is_gated_when_access_is_enabled(settings: Settings):
    """Spec 3.5: HTML, API and SSE must all be unreachable unauthenticated.
    Verifying the Access assertion in-app is the second lock (spec-review A-4).
    """
    guarded = settings.model_copy(
        update={
            "auth_mode": "cloudflare_access",
            "cf_access_team_domain": "example.cloudflareaccess.com",
            "cf_access_aud": "aud-tag",
            "allowed_emails": ["owner@example.com"],
        }
    )
    app = create_app(guarded)
    with TestClient(app) as test_client:
        for path in ("/", "/api/health", "/api/symbols", "/api/live", "/static/app.js"):
            response = test_client.get(path)
            assert response.status_code in {401, 403, 503}, path

        # The local supervisor probe stays open; it leaks nothing.
        assert test_client.get("/api/livez").status_code == 200


def test_disabled_auth_is_refused_on_a_public_interface(settings: Settings):
    exposed = settings.model_copy(update={"auth_mode": "disabled", "api_host": "0.0.0.0"})
    with pytest.raises(RuntimeError, match="loopback"):
        exposed.validate_for_serving()


def test_access_config_must_be_complete(settings: Settings):
    incomplete = settings.model_copy(
        update={"auth_mode": "cloudflare_access", "cf_access_team_domain": None}
    )
    with pytest.raises(RuntimeError, match="cf_access_team_domain"):
        incomplete.validate_for_serving()


def test_security_headers_are_present(client: TestClient):
    response = client.get("/api/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'self'" in response.headers["content-security-policy"]


def test_index_is_served(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    # The chart module, not the attribution text: the visible credit is a
    # presentation choice, while this is the page actually loading its chart.
    assert "lightweight-charts.standalone.production.js" in response.text
    assert 'id="chart-grid"' in response.text
    assert response.text.count("data-chart-days=") == 4
    assert 'data-chart-days="7"' in response.text


def test_repeated_search_does_not_hit_the_provider_twice(client: TestClient, monkeypatch):
    """Autocomplete must not be what spends the hourly allowance.

    A debounced box issues one request per prefix, and the same prefixes recur
    every time it is used. Against 50 calls/hour shared with backfill, that is
    the whole budget.
    """
    from usstocks.api.routes import symbols as symbols_route

    symbols_route._search_cache.clear()
    calls: list[str] = []

    async def counting(self, query: str, limit: int = 20) -> list[dict[str, str]]:
        calls.append(query)
        return [{"symbol": "MU", "name": "Micron Technology Inc",
                 "exchange": "NASDAQ", "asset_type": "Stock"}]

    monkeypatch.setattr(MockAdapter, "search_symbols", counting)

    first = client.get("/api/symbols/search", params={"q": "MU"}).json()
    second = client.get("/api/symbols/search", params={"q": "MU"}).json()

    assert calls == ["MU"]
    assert [item["symbol"] for item in first] == ["MU"]
    assert second == first


def test_search_degrades_to_local_when_the_budget_is_gone(
    client: TestClient, settings: Settings, monkeypatch
):
    """A search the user can retry is a better thing to lose than a gap that is
    never backfilled, so an exhausted allowance returns local matches instead of
    an error."""
    from usstocks.api.routes import symbols as symbols_route

    symbols_route._search_cache.clear()
    with Repository(settings.db_path) as repo:
        repo.upsert_symbol(SymbolInfo(symbol="AAPL", name="Apple Inc."))

    called = False

    async def should_not_run(self, query: str, limit: int = 20) -> list[dict[str, str]]:
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(MockAdapter, "search_symbols", should_not_run)

    async def exhausted(self, tokens: int = 1, timeout: float | None = None) -> bool:
        return False

    from usstocks.collector.ratelimit import RestBudget

    monkeypatch.setattr(RestBudget, "acquire", exhausted)

    results = client.get("/api/symbols/search", params={"q": "AAPL"}).json()

    assert called is False
    assert [item["symbol"] for item in results] == ["AAPL"]


def test_requesting_bars_marks_the_symbol_as_viewed(client: TestClient, settings: Settings):
    """Fetching a chart is how the collector learns where to spend its calls.

    With neither free tier streaming (spec-review A-6), REST is the live path and
    the hourly allowance only stretches to one symbol at a useful rate. The API
    is the only process that knows which symbol a browser is showing.
    """
    with Repository(settings.db_path) as repo:
        repo.upsert_symbol(SymbolInfo(symbol="AAPL", is_watched=True))
        assert repo.recently_viewed(timedelta(minutes=5)) == []

    client.get("/api/bars/AAPL", params={"days": 1})

    with Repository(settings.db_path) as repo:
        assert repo.recently_viewed(timedelta(minutes=5)) == ["AAPL"]
