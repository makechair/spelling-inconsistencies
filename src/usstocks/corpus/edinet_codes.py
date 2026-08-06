"""Mirror EDINET's list of filers so companies can be searched by name.

EDINET publishes every registered filer as a single ZIP of CSV -- a static
download, no subscription key -- and that is the only complete answer to "what
companies exist?". The document API answers by submission date, so without
this list a company could only be found by already knowing its code.

Only filers with a securities code are kept. The list holds roughly twenty
thousand entities, most of them funds and unlisted issuers with no share
price and no comparable statements; keeping them would bury the few thousand
listed companies under names nobody here can chart.

The file is written as JSON rather than Parquet because the API process
serves searches out of it, and that process deliberately has no DuckDB or
pyarrow in it.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings, get_settings
from ..logging_setup import configure_logging
from .daily import CorpusError

log = logging.getLogger(__name__)

# The header row EDINET writes, in the encoding it writes it in. The first
# line of the file is a title, so the header is found by content rather than
# by position -- a leading blank line has appeared and gone before.
CODE_COLUMN = "ＥＤＩＮＥＴコード"
NAME_COLUMN = "提出者名"
SEC_CODE_COLUMN = "証券コード"
INDUSTRY_COLUMN = "提出者業種"
# Shift_JIS in name; cp932 in fact, which decodes the vendor characters that
# appear in company names where plain shift_jis raises.
ENCODING = "cp932"


def parse_code_list(payload: bytes) -> list[dict[str, str]]:
    """Pull the listed companies out of the downloaded ZIP."""
    try:
        bundle = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise CorpusError("EDINET code list is not a ZIP archive") from exc
    names = [name for name in bundle.namelist() if name.lower().endswith(".csv")]
    if not names:
        raise CorpusError("EDINET code list ZIP holds no CSV")

    rows: list[dict[str, str]] = []
    with bundle.open(names[0]) as handle:
        text = handle.read().decode(ENCODING, errors="replace")
    reader = csv.reader(io.StringIO(text))
    header: list[str] | None = None
    for record in reader:
        if header is None:
            if CODE_COLUMN in record:
                header = record
            continue
        row = dict(zip(header, record, strict=False))
        code = _four_digit(row.get(SEC_CODE_COLUMN, ""))
        if code is None:
            continue
        name = (row.get(NAME_COLUMN) or "").strip()
        if not name:
            continue
        rows.append(
            {
                "code": code,
                "name": name,
                "edinet_code": (row.get(CODE_COLUMN) or "").strip(),
                "industry": (row.get(INDUSTRY_COLUMN) or "").strip(),
            }
        )
    if header is None:
        raise CorpusError(f"EDINET code list has no {CODE_COLUMN} header row")
    if not rows:
        raise CorpusError("EDINET code list yielded no listed companies")
    # A company can hold several EDINET codes over time; the securities code
    # is what everything downstream joins on, so it has to be unique here.
    unique: dict[str, dict[str, str]] = {}
    for row in rows:
        unique.setdefault(row["code"], row)
    return sorted(unique.values(), key=lambda row: row["code"])


def _four_digit(raw: str) -> str | None:
    """EDINET writes a five-character securities code with a trailing filler.

    The same narrowing the document index does. Anything else -- blank for an
    unlisted filer, or a length this does not recognise -- is not a code this
    project can use.
    """
    text = str(raw or "").strip().upper()
    if len(text) == 5 and text[3].isalnum():
        text = text[:4]
    if len(text) != 4 or not text[:3].isdigit():
        return None
    return text


def fetch(settings: Settings, *, client: httpx.Client | None = None) -> bytes:
    owned = client is None
    session = client or httpx.Client(timeout=settings.edinet_timeout_seconds)
    try:
        response = session.get(settings.edinet_code_list_url)
        response.raise_for_status()
        return response.content
    except httpx.HTTPError as exc:
        # The URL carries no secret, but every other EDINET call in this
        # project hides its request details and consistency is worth more than
        # the detail here.
        raise CorpusError(f"EDINET code list download failed: {type(exc).__name__}") from exc
    finally:
        if owned:
            session.close()


def output_path(settings: Settings) -> Path:
    return settings.corpus_local_dir / "edinet_codes" / "companies.json"


def write_companies(path: Path, companies: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "version": 1,
                "generated_at": datetime.now(tz=UTC).isoformat(),
                "companies": companies,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def run(settings: Settings, *, client: httpx.Client | None = None) -> int:
    companies = parse_code_list(fetch(settings, client=client))
    path = output_path(settings)
    write_companies(path, companies)
    log.info("EDINET code list refreshed: %d listed company(ies) -> %s", len(companies), path)
    return 0


def search(companies: list[dict[str, Any]], query: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """Match on the securities code or anywhere in the name.

    Ranked so an exact code comes first and a name that starts with the query
    beats one that merely contains it -- typing "6501" should not return a
    company whose name happens to contain those digits ahead of the company
    that is 6501.
    """
    wanted = str(query or "").strip()
    if not wanted:
        return []
    folded = wanted.casefold()
    scored: list[tuple[int, str, dict[str, Any]]] = []
    for company in companies:
        code = str(company.get("code") or "")
        name = str(company.get("name") or "")
        lowered = name.casefold()
        if code == wanted.upper():
            rank = 0
        elif code.startswith(wanted.upper()) and wanted.isdigit():
            rank = 1
        elif lowered.startswith(folded):
            rank = 2
        elif folded in lowered:
            rank = 3
        else:
            continue
        scored.append((rank, code, company))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [company for _, _, company in scored[:limit]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        return run(settings)
    except CorpusError as exc:
        log.error("%s", exc)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
