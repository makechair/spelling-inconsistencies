"""User-managed company lists, and the Japanese companies they pull in.

Two things live in one file because they are edited together and have to stay
consistent. A list is a named set of symbols; a registered company is a
Japanese filer the user added from the EDINET code list, which the ingestion
jobs then treat as part of the universe. Removing a company has to remove it
from every list as well, or the page would keep showing a symbol nothing is
fetching any more.

The universe CSVs in the repository stay authoritative for the symbols the
project ships with. This file only ever adds to them: a release can change
the shipped universe without the user's additions being rewritten, and the
user's additions survive a redeploy because they live under the durable state
directory rather than in the release.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Twenty is the cap the lists themselves are under. It is not a technical
# limit -- the file would hold thousands -- but a tab strip stops being
# navigable long before that, and an unbounded count invites a runaway client.
MAX_LISTS = 20
MAX_NAME_LENGTH = 40
# A US ticker or a Japanese securities code. Japanese codes have allowed a
# letter in the fourth position since 2024 (130A and the like).
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")
JP_CODE_PATTERN = re.compile(r"^\d{3}[0-9A-Z]$")
# Assigned to companies the user adds: the EDINET code list carries an
# industry, but not this project's subsector taxonomy, and inventing a mapping
# would file companies under headings they were never assessed against.
UNCLASSIFIED = "unclassified"


class WatchlistError(Exception):
    """A request that the store refuses. Always the caller's input."""


@dataclass(frozen=True)
class Company:
    code: str
    name: str
    subsector: str


def empty_store() -> dict[str, Any]:
    return {"version": 1, "lists": [], "companies": []}


def load(path: Path) -> dict[str, Any]:
    """Read the store, treating absence as empty and damage as fatal.

    A missing file is the ordinary first-run state. A malformed one is not:
    starting over from empty would silently discard lists the user built, so
    it fails and leaves the file for inspection.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return empty_store()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WatchlistError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("lists"), list):
        raise WatchlistError(f"{path} is not a watchlist store")
    payload.setdefault("companies", [])
    payload.setdefault("version", 1)
    return payload


def save(path: Path, store: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # Replaced rather than written in place: a crash mid-write would otherwise
    # leave the file unparseable, which the loader refuses to recover from.
    temporary.replace(path)


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _clean_name(name: str) -> str:
    cleaned = " ".join(str(name).split())
    if not cleaned:
        raise WatchlistError("a list needs a name")
    if len(cleaned) > MAX_NAME_LENGTH:
        raise WatchlistError(f"a list name is at most {MAX_NAME_LENGTH} characters")
    return cleaned


def normalize_symbol(symbol: str) -> str:
    cleaned = str(symbol).strip().upper()
    if not SYMBOL_PATTERN.match(cleaned):
        raise WatchlistError(f"not a symbol: {symbol!r}")
    return cleaned


def _find_list(store: dict[str, Any], list_id: str) -> dict[str, Any]:
    for entry in store["lists"]:
        if entry.get("id") == list_id:
            return entry
    raise WatchlistError(f"no such list: {list_id}")


def _next_id(store: dict[str, Any]) -> str:
    # Sequential rather than random: the id shows up in URLs the user may
    # bookmark, and a readable one is easier to reason about in the file.
    used = {entry.get("id") for entry in store["lists"]}
    index = 1
    while f"list-{index}" in used:
        index += 1
    return f"list-{index}"


def create_list(store: dict[str, Any], name: str) -> dict[str, Any]:
    cleaned = _clean_name(name)
    if len(store["lists"]) >= MAX_LISTS:
        raise WatchlistError(f"at most {MAX_LISTS} lists; delete one first")
    if any(entry.get("name") == cleaned for entry in store["lists"]):
        raise WatchlistError(f"a list named {cleaned!r} already exists")
    entry = {"id": _next_id(store), "name": cleaned, "symbols": [], "created_at": _now()}
    store["lists"].append(entry)
    return entry


def rename_list(store: dict[str, Any], list_id: str, name: str) -> dict[str, Any]:
    cleaned = _clean_name(name)
    entry = _find_list(store, list_id)
    if any(
        other.get("name") == cleaned and other.get("id") != list_id
        for other in store["lists"]
    ):
        raise WatchlistError(f"a list named {cleaned!r} already exists")
    entry["name"] = cleaned
    return entry


def delete_list(store: dict[str, Any], list_id: str) -> None:
    entry = _find_list(store, list_id)
    store["lists"].remove(entry)


def add_symbol(store: dict[str, Any], list_id: str, symbol: str) -> dict[str, Any]:
    entry = _find_list(store, list_id)
    cleaned = normalize_symbol(symbol)
    if cleaned not in entry["symbols"]:
        entry["symbols"].append(cleaned)
    return entry


def remove_symbol(store: dict[str, Any], list_id: str, symbol: str) -> dict[str, Any]:
    entry = _find_list(store, list_id)
    cleaned = normalize_symbol(symbol)
    entry["symbols"] = [held for held in entry["symbols"] if held != cleaned]
    return entry


def register_company(
    store: dict[str, Any], *, code: str, name: str, subsector: str = UNCLASSIFIED
) -> dict[str, Any]:
    """Add a Japanese filer to the universe the ingestion jobs walk.

    The name is stored rather than looked up later because the EDINET code
    list is refreshed on its own timer: a company registered today must stay
    readable even if the next refresh fails.
    """
    cleaned_code = str(code).strip().upper()
    if not JP_CODE_PATTERN.match(cleaned_code):
        raise WatchlistError(f"not a Japanese securities code: {code!r}")
    cleaned_name = " ".join(str(name).split())
    if not cleaned_name:
        raise WatchlistError("a company needs a name")
    subsector = (subsector or UNCLASSIFIED).strip() or UNCLASSIFIED
    for held in store["companies"]:
        if held.get("code") == cleaned_code:
            held["name"] = cleaned_name
            held["subsector"] = subsector
            return held
    entry = {
        "code": cleaned_code,
        "name": cleaned_name,
        "subsector": subsector,
        "added_at": _now(),
    }
    store["companies"].append(entry)
    return entry


def unregister_company(store: dict[str, Any], code: str) -> None:
    """Stop fetching a company, and take it out of every list.

    Leaving it in a list would show a symbol whose figures nothing refreshes,
    which is worse than not showing it: the numbers would look current.
    Archived filings stay on disk; only the universe shrinks.
    """
    cleaned = str(code).strip().upper()
    before = len(store["companies"])
    store["companies"] = [
        held for held in store["companies"] if held.get("code") != cleaned
    ]
    if len(store["companies"]) == before:
        raise WatchlistError(f"no such company: {code}")
    for entry in store["lists"]:
        entry["symbols"] = [held for held in entry["symbols"] if held != cleaned]


def companies(store: dict[str, Any]) -> list[Company]:
    return [
        Company(
            code=str(entry.get("code") or ""),
            name=str(entry.get("name") or ""),
            subsector=str(entry.get("subsector") or UNCLASSIFIED),
        )
        for entry in store.get("companies", [])
        if entry.get("code")
    ]
