"""The only write endpoints in the API.

Everything else serves a file some job prepared; these edit a store, so the
tests cover what happens when the input is wrong and what the ingestion jobs
see afterwards.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from usstocks import watchlists
from usstocks.api.app import create_app
from usstocks.config import Settings


@pytest.fixture
def client(settings: Settings):
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def mirror(settings: Settings, companies=None) -> None:
    """Stand in for the EDINET code-list job."""
    path = settings.corpus_local_dir / "edinet_codes" / "companies.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "companies": companies
                or [
                    {"code": "6501", "name": "株式会社日立製作所", "industry": "電気機器"},
                    {"code": "6146", "name": "株式会社ディスコ", "industry": "機械"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_lists_start_empty(client: TestClient):
    payload = client.get("/api/watchlists").json()
    assert payload["lists"] == []
    assert payload["max_lists"] == watchlists.MAX_LISTS


def test_a_list_can_be_created_filled_and_deleted(client: TestClient):
    created = client.post("/api/watchlists", json={"name": "主力"})
    assert created.status_code == 201
    list_id = created.json()["id"]

    client.post(f"/api/watchlists/{list_id}/symbols", json={"symbol": "mu"})
    held = client.get("/api/watchlists").json()["lists"][0]
    assert held["symbols"] == ["MU"]

    client.delete(f"/api/watchlists/{list_id}/symbols/MU")
    assert client.get("/api/watchlists").json()["lists"][0]["symbols"] == []

    assert client.delete(f"/api/watchlists/{list_id}").status_code == 204
    assert client.get("/api/watchlists").json()["lists"] == []


def test_a_rejected_edit_says_why(client: TestClient):
    """The page shows the detail, so it has to be a sentence rather than a
    validation dump."""
    client.post("/api/watchlists", json={"name": "主力"})
    duplicate = client.post("/api/watchlists", json={"name": "主力"})
    assert duplicate.status_code == 400
    assert "already exists" in duplicate.json()["detail"]


def test_searching_before_the_mirror_exists_is_not_an_empty_result(client: TestClient):
    """No results and no list are different problems; reporting the second as
    the first would send the user looking for a company that is there."""
    response = client.get("/api/companies/search", params={"q": "日立"})
    assert response.status_code == 503


def test_a_company_is_found_by_name_or_by_code(client: TestClient, settings: Settings):
    mirror(settings)
    by_name = client.get("/api/companies/search", params={"q": "日立"}).json()["results"]
    by_code = client.get("/api/companies/search", params={"q": "6501"}).json()["results"]
    assert by_name[0]["code"] == "6501"
    assert by_code[0]["name"] == "株式会社日立製作所"
    assert by_name[0]["added"] is False


def test_registering_uses_the_name_edinet_holds(client: TestClient, settings: Settings):
    """Not the caller's: a label the filings never used would make the row
    impossible to match against its source."""
    mirror(settings)
    created = client.post("/api/companies", json={"code": "6501", "subsector": "diversified"})
    assert created.status_code == 201
    assert created.json()["name"] == "株式会社日立製作所"
    assert client.get("/api/companies/search", params={"q": "6501"}).json()[
        "results"
    ][0]["added"] is True


def test_a_company_edinet_does_not_list_is_refused(client: TestClient, settings: Settings):
    mirror(settings)
    assert client.post("/api/companies", json={"code": "9999"}).status_code == 404


def test_registering_can_fill_a_list_in_the_same_call(client: TestClient, settings: Settings):
    mirror(settings)
    list_id = client.post("/api/watchlists", json={"name": "日本株"}).json()["id"]
    client.post("/api/companies", json={"code": "6146", "list_id": list_id})
    assert client.get("/api/watchlists").json()["lists"][0]["symbols"] == ["6146"]


def test_unregistering_clears_the_lists_too(client: TestClient, settings: Settings):
    mirror(settings)
    list_id = client.post("/api/watchlists", json={"name": "日本株"}).json()["id"]
    client.post("/api/companies", json={"code": "6146", "list_id": list_id})
    assert client.delete("/api/companies/6146").status_code == 204
    payload = client.get("/api/watchlists").json()
    assert payload["companies"] == []
    assert payload["lists"][0]["symbols"] == []


def test_a_registered_company_joins_the_ingestion_universe(
    client: TestClient, settings: Settings, tmp_path: Path
):
    """The point of adding one: the EDINET jobs have to walk it. Without this
    the page would list a company nothing ever fetches."""
    from usstocks.corpus.edinet import japanese_universe

    universe = tmp_path / "universe_jp.csv"
    universe.write_text("code,name,subsector\n8035,東京エレクトロン,equipment\n", encoding="utf-8")
    settings = settings.model_copy(update={"universe_jp_path": universe})

    mirror(settings)
    client.post("/api/companies", json={"code": "6501", "subsector": "diversified"})

    codes = {entry.code: entry for entry in japanese_universe(settings)}
    assert set(codes) == {"8035", "6501"}
    assert codes["6501"].name == "株式会社日立製作所"
    assert codes["6501"].subsector == "diversified"


def test_the_shipped_universe_wins_a_collision(settings: Settings, tmp_path: Path):
    """The CSV carries a reviewed subsector; an added company carries whatever
    was picked in a dialog."""
    from usstocks.corpus.edinet import japanese_universe

    universe = tmp_path / "universe_jp.csv"
    universe.write_text("code,name,subsector\n8035,東京エレクトロン,equipment\n", encoding="utf-8")
    settings = settings.model_copy(update={"universe_jp_path": universe})

    store = watchlists.empty_store()
    watchlists.register_company(store, code="8035", name="別名", subsector="unclassified")
    watchlists.save(settings.watchlists_path, store)

    entries = japanese_universe(settings)
    assert len(entries) == 1
    assert entries[0].subsector == "equipment"
