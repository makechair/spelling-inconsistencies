"""The user's lists and the companies they add to the universe.

These are the only records in the project the user edits directly, so the
tests are about refusing bad input and never losing good input.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from usstocks import watchlists


def test_a_missing_store_is_an_empty_one(tmp_path: Path):
    """First run, not an error."""
    assert watchlists.load(tmp_path / "absent.json") == watchlists.empty_store()


def test_a_damaged_store_refuses_to_start_over(tmp_path: Path):
    """Returning an empty store would silently discard every list the file
    still holds; the user cannot recover what nothing reported losing."""
    path = tmp_path / "watchlists.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(watchlists.WatchlistError):
        watchlists.load(path)


def test_lists_are_capped(tmp_path: Path):
    store = watchlists.empty_store()
    for index in range(watchlists.MAX_LISTS):
        watchlists.create_list(store, f"list {index}")
    with pytest.raises(watchlists.WatchlistError, match="at most"):
        watchlists.create_list(store, "one too many")


def test_two_lists_cannot_share_a_name():
    """The tab strip shows names, not ids."""
    store = watchlists.empty_store()
    watchlists.create_list(store, "半導体")
    with pytest.raises(watchlists.WatchlistError, match="already exists"):
        watchlists.create_list(store, "半導体")


def test_a_name_is_trimmed_and_bounded():
    store = watchlists.empty_store()
    assert watchlists.create_list(store, "  主力   銘柄 ")["name"] == "主力 銘柄"
    with pytest.raises(watchlists.WatchlistError):
        watchlists.create_list(store, "   ")
    with pytest.raises(watchlists.WatchlistError):
        watchlists.create_list(store, "x" * (watchlists.MAX_NAME_LENGTH + 1))


def test_symbols_are_normalised_and_deduplicated():
    store = watchlists.empty_store()
    entry = watchlists.create_list(store, "main")
    watchlists.add_symbol(store, entry["id"], "nvda")
    watchlists.add_symbol(store, entry["id"], "NVDA")
    assert entry["symbols"] == ["NVDA"]
    with pytest.raises(watchlists.WatchlistError):
        watchlists.add_symbol(store, entry["id"], "not a symbol!")


def test_a_japanese_code_may_end_in_a_letter():
    """Codes issued since 2024 look like 130A. Rejecting them would make the
    newest listings unaddable."""
    store = watchlists.empty_store()
    watchlists.register_company(store, code="130A", name="新規上場")
    assert watchlists.companies(store)[0].code == "130A"
    with pytest.raises(watchlists.WatchlistError):
        watchlists.register_company(store, code="NVDA", name="Nvidia")


def test_removing_a_company_takes_it_out_of_every_list():
    """A list holding a symbol nothing fetches any more would keep showing
    figures that look current and are not."""
    store = watchlists.empty_store()
    first = watchlists.create_list(store, "one")
    second = watchlists.create_list(store, "two")
    watchlists.register_company(store, code="6501", name="日立製作所")
    watchlists.add_symbol(store, first["id"], "6501")
    watchlists.add_symbol(store, second["id"], "6501")
    watchlists.add_symbol(store, second["id"], "NVDA")

    watchlists.unregister_company(store, "6501")
    assert first["symbols"] == []
    assert second["symbols"] == ["NVDA"]
    assert watchlists.companies(store) == []


def test_registering_twice_updates_rather_than_duplicates():
    store = watchlists.empty_store()
    watchlists.register_company(store, code="6501", name="日立製作所")
    watchlists.register_company(
        store, code="6501", name="日立製作所", subsector="diversified"
    )
    held = watchlists.companies(store)
    assert len(held) == 1
    assert held[0].subsector == "diversified"


def test_deleting_a_list_leaves_the_companies_alone():
    """The list is a grouping over symbols that exist elsewhere."""
    store = watchlists.empty_store()
    entry = watchlists.create_list(store, "one")
    watchlists.register_company(store, code="6501", name="日立製作所")
    watchlists.add_symbol(store, entry["id"], "6501")
    watchlists.delete_list(store, entry["id"])
    assert [company.code for company in watchlists.companies(store)] == ["6501"]


def test_a_saved_store_reads_back_identically(tmp_path: Path):
    path = tmp_path / "watchlists.json"
    store = watchlists.empty_store()
    entry = watchlists.create_list(store, "主力")
    watchlists.add_symbol(store, entry["id"], "MU")
    watchlists.register_company(store, code="6501", name="日立製作所")
    watchlists.save(path, store)
    assert watchlists.load(path) == store


def test_a_quantity_needs_the_symbol_to_be_in_the_list():
    store = watchlists.empty_store()
    entry = watchlists.create_list(store, "main")
    with pytest.raises(watchlists.WatchlistError, match="not in this list"):
        watchlists.set_quantity(store, entry["id"], "NVDA", 100)
    watchlists.add_symbol(store, entry["id"], "NVDA")
    watchlists.set_quantity(store, entry["id"], "NVDA", 100)
    assert entry["quantities"] == {"NVDA": 100.0}


def test_clearing_a_quantity_is_not_the_same_as_setting_zero():
    """Zero is a position that was closed; absent is one never sized. Only
    the second should drop out of the risk figures entirely."""
    store = watchlists.empty_store()
    entry = watchlists.create_list(store, "main")
    watchlists.add_symbol(store, entry["id"], "NVDA")
    watchlists.set_quantity(store, entry["id"], "NVDA", 0)
    assert entry["quantities"] == {"NVDA": 0.0}
    watchlists.set_quantity(store, entry["id"], "NVDA", None)
    assert entry["quantities"] == {}


def test_a_negative_quantity_is_refused():
    store = watchlists.empty_store()
    entry = watchlists.create_list(store, "main")
    watchlists.add_symbol(store, entry["id"], "NVDA")
    with pytest.raises(watchlists.WatchlistError):
        watchlists.set_quantity(store, entry["id"], "NVDA", -10)


def test_removing_a_symbol_takes_its_quantity_with_it():
    """A share count for a symbol the list no longer holds would keep sizing
    a position nothing shows."""
    store = watchlists.empty_store()
    entry = watchlists.create_list(store, "main")
    watchlists.add_symbol(store, entry["id"], "NVDA")
    watchlists.set_quantity(store, entry["id"], "NVDA", 100)
    watchlists.remove_symbol(store, entry["id"], "NVDA")
    assert entry["quantities"] == {}
