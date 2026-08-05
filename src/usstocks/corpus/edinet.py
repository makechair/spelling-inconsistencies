"""Archive EDINET filings for the Japanese universe: PDF, XBRL, and an index.

JP-A of docs/earnings-spec.md. Discovery and archival only; turning the XBRL
into concept rows comes after, and reads from what this stores.

EDINET differs from EDGAR in the two ways that shape this module:

* There is no per-company "all facts" endpoint. The list API answers by
  submission date, so finding a company's filings means walking days and
  filtering. Everything already seen is skipped by docID.
* Documents arrive as a ZIP of raw XBRL instances and a separate PDF, not as
  parsed JSON. Both are archived verbatim so the parser can be rewritten
  later without re-fetching -- the same reason the news corpus keeps article
  snapshots.

The API key travels as a query parameter because that is what EDINET accepts.
It must therefore never reach a log: request URLs are not logged, and httpx
exception text is replaced with the exception type alone.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings, get_settings
from ..logging_setup import configure_logging
from .daily import (
    CorpusError,
    ProviderResponseError,
    Uploader,
    aws_upload,
    corpus_s3_root,
    save_state,
)

log = logging.getLogger(__name__)

LIST_URL = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
DOCUMENT_URL = "https://api.edinet-fsa.go.jp/api/v2/documents/{doc_id}"

# EDINET publishes no hard rate limit. One request a second is unhurried
# enough to be a good citizen and still walks a year of days in twenty minutes.
REQUEST_INTERVAL_SECONDS = 1.0

# 120 有価証券報告書 / 130 訂正 / 140 四半期報告書 / 150 訂正 /
# 160 半期報告書 / 170 訂正。Quarterly reports were abolished during 2024 in
# favour of semi-annual ones, so both kinds appear depending on the year.
PERIODIC_DOC_TYPES = frozenset({"120", "130", "140", "150", "160", "170"})

DOCUMENT_TYPE_XBRL = 1
DOCUMENT_TYPE_PDF = 2


@dataclass(frozen=True)
class JapaneseEntry:
    code: str
    name: str
    subsector: str


def load_universe_jp(path: Path) -> list[JapaneseEntry]:
    import csv

    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["code", "name", "subsector"]:
            raise CorpusError("universe_jp CSV must have exactly: code,name,subsector")
        entries: list[JapaneseEntry] = []
        seen: set[str] = set()
        for line_number, row in enumerate(reader, start=2):
            code = (row.get("code") or "").strip()
            name = (row.get("name") or "").strip()
            subsector = (row.get("subsector") or "").strip()
            if not code or not name or not subsector:
                raise CorpusError(f"universe_jp CSV line {line_number} has an empty field")
            if not code.isdigit() or len(code) != 4:
                raise CorpusError(f"universe_jp CSV line {line_number} has a non-4-digit code")
            if code in seen:
                raise CorpusError(f"duplicate universe_jp code: {code}")
            seen.add(code)
            entries.append(JapaneseEntry(code, name, subsector))
    if not entries:
        raise CorpusError("universe_jp CSV is empty")
    return entries


def edinet_s3_root(settings: Settings) -> str:
    return f"{corpus_s3_root(settings)}/edinet"


def load_edinet_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "documents": {}, "scanned_dates": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorpusError(f"cannot read EDINET state: {type(exc).__name__}") from exc
    if payload.get("version") != 1 or not isinstance(payload.get("documents"), dict):
        raise CorpusError("unsupported EDINET state format")
    payload.setdefault("scanned_dates", [])
    return payload


def _api_key(settings: Settings) -> str:
    key = (settings.edinet_api_key or "").strip()
    if not key:
        raise CorpusError(
            "USSTOCKS_EDINET_API_KEY is not set; EDINET v2 requires a subscription key"
        )
    return key


def _normalize_sec_code(raw: object) -> str | None:
    """EDINET reports a five-character securities code: 8035 arrives as 80350."""
    text = str(raw or "").strip()
    if len(text) == 5 and text.isdigit():
        return text[:4]
    if len(text) == 4 and text.isdigit():
        return text
    return None


def fetch_document_list(
    client: httpx.Client, settings: Settings, day: date
) -> list[dict[str, Any]]:
    try:
        response = client.get(
            LIST_URL,
            params={
                "date": day.isoformat(),
                "type": 2,
                "Subscription-Key": _api_key(settings),
            },
        )
    except httpx.HTTPError as exc:
        # The URL carries the subscription key, and httpx puts it in the message.
        raise CorpusError(f"EDINET list request failed for {day}: {type(exc).__name__}") from exc
    if response.status_code >= 400:
        raise ProviderResponseError(f"list {day}", response.status_code)
    try:
        payload = response.json()
    except ValueError as exc:
        raise CorpusError(f"EDINET returned invalid JSON for {day}") from exc
    results = payload.get("results")
    if results is None:
        # A non-business day answers with metadata and no results list.
        return []
    if not isinstance(results, list):
        raise CorpusError(f"EDINET results for {day} is not a list")
    return [row for row in results if isinstance(row, dict)]


def select_documents(
    rows: Sequence[dict[str, Any]], wanted_codes: set[str]
) -> list[dict[str, Any]]:
    """Periodic reports filed by the universe, ignoring everything else.

    EDINET carries every filing by every issuer, most of which -- large
    shareholding reports, extraordinary reports -- have no financial statements
    in them.
    """
    selected: list[dict[str, Any]] = []
    for row in rows:
        code = _normalize_sec_code(row.get("secCode"))
        if code is None or code not in wanted_codes:
            continue
        if str(row.get("docTypeCode") or "") not in PERIODIC_DOC_TYPES:
            continue
        if not str(row.get("docID") or "").strip():
            continue
        selected.append(row)
    return selected


def fetch_document(
    client: httpx.Client, settings: Settings, doc_id: str, document_type: int
) -> bytes | None:
    """One document, or None when EDINET has no file of that type for it.

    Not every filing carries both a PDF and an XBRL archive, and a missing one
    is ordinary rather than an error worth stopping the run for.
    """
    try:
        response = client.get(
            DOCUMENT_URL.format(doc_id=doc_id),
            params={"type": document_type, "Subscription-Key": _api_key(settings)},
        )
    except httpx.HTTPError as exc:
        raise CorpusError(
            f"EDINET document request failed for {doc_id}: {type(exc).__name__}"
        ) from exc
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise ProviderResponseError(f"document {doc_id}", response.status_code)
    content = response.content
    # EDINET answers a missing file with a JSON error body and a 200.
    if content[:1] == b"{":
        return None
    return content or None


def _daterange(start: date, end: date) -> Iterator[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _parquet_modules() -> tuple[Any, Any]:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise CorpusError("EDINET corpus requires the 'parquet' package extra") from exc
    return pa, pq


def _index_schema() -> Any:
    pa, _ = _parquet_modules()
    return pa.schema(
        [
            ("code", pa.string()),
            ("name", pa.string()),
            ("subsector", pa.string()),
            ("edinet_code", pa.string()),
            ("filer_name", pa.string()),
            ("doc_id", pa.string()),
            ("doc_type_code", pa.string()),
            ("doc_description", pa.string()),
            ("period_start", pa.string()),
            ("period_end", pa.string()),
            ("submit_datetime", pa.string()),
            ("xbrl_s3_key", pa.string()),
            ("pdf_s3_key", pa.string()),
        ]
    )


def write_index_parquet(path: Path, rows: Sequence[dict[str, object]]) -> str:
    pa, pq = _parquet_modules()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    ordered = sorted(rows, key=lambda row: (str(row["code"]), str(row["doc_id"])))
    pq.write_table(
        pa.Table.from_pylist(ordered, schema=_index_schema()), temporary, compression="zstd"
    )
    digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
    temporary.replace(path)
    return digest


def run(
    settings: Settings,
    *,
    since: date | None = None,
    until: date | None = None,
    uploader: Uploader = aws_upload,
    client: httpx.Client | None = None,
    now: datetime | None = None,
    sleeper: Any = time.sleep,
) -> int:
    entries = load_universe_jp(settings.universe_jp_path)
    by_code = {entry.code: entry for entry in entries}
    moment = (now or datetime.now(tz=UTC)).astimezone(UTC)
    end = until or moment.date()
    start = since or (end - timedelta(days=settings.edinet_lookback_days))
    if start > end:
        raise CorpusError("since must not be after until")

    local_root = settings.corpus_local_dir
    state_path = local_root / "edinet-state.json"
    state = load_edinet_state(state_path)
    documents: dict[str, Any] = state["documents"]
    scanned: set[str] = set(state.get("scanned_dates", []))
    s3_root = edinet_s3_root(settings)

    own_client = client is None
    http_client = client or httpx.Client(
        timeout=settings.edinet_timeout_seconds, follow_redirects=True
    )
    stored = 0
    try:
        for day in _daterange(start, end):
            key = day.isoformat()
            # Today's list can still grow, so it is never marked done.
            if key in scanned and day < moment.date():
                continue
            sleeper(REQUEST_INTERVAL_SECONDS)
            rows = select_documents(fetch_document_list(http_client, settings, day), set(by_code))
            for row in rows:
                doc_id = str(row["docID"])
                if doc_id in documents:
                    continue
                code = _normalize_sec_code(row.get("secCode"))
                if code is None:
                    continue
                entry = by_code[code]
                record: dict[str, object] = {
                    "code": code,
                    "name": entry.name,
                    "subsector": entry.subsector,
                    "edinet_code": str(row.get("edinetCode") or ""),
                    "filer_name": str(row.get("filerName") or ""),
                    "doc_id": doc_id,
                    "doc_type_code": str(row.get("docTypeCode") or ""),
                    "doc_description": str(row.get("docDescription") or ""),
                    "period_start": str(row.get("periodStart") or ""),
                    "period_end": str(row.get("periodEnd") or ""),
                    "submit_datetime": str(row.get("submitDateTime") or ""),
                    "xbrl_s3_key": "",
                    "pdf_s3_key": "",
                }
                for label, doc_type, suffix in (
                    ("xbrl", DOCUMENT_TYPE_XBRL, "xbrl.zip"),
                    ("pdf", DOCUMENT_TYPE_PDF, "document.pdf"),
                ):
                    sleeper(REQUEST_INTERVAL_SECONDS)
                    payload = fetch_document(http_client, settings, doc_id, doc_type)
                    if payload is None:
                        log.info("no %s for %s (%s)", label, doc_id, entry.name)
                        continue
                    local = local_root / "edinet" / f"code={code}" / doc_id / suffix
                    local.parent.mkdir(parents=True, exist_ok=True)
                    temporary = local.with_suffix(local.suffix + ".tmp")
                    temporary.write_bytes(payload)
                    temporary.replace(local)
                    destination = f"{s3_root}/code={code}/{doc_id}/{suffix}"
                    uploader(local, destination)
                    record[f"{label}_s3_key"] = destination
                documents[doc_id] = record
                stored += 1
                save_state(state_path, state)
            scanned.add(key)
            state["scanned_dates"] = sorted(scanned)
            save_state(state_path, state)
    except ProviderResponseError as exc:
        state["scanned_dates"] = sorted(scanned)
        save_state(state_path, state)
        log.error("EDINET responded with an error; stopping after %d document(s): %s", stored, exc)
        return 2
    finally:
        if own_client:
            http_client.close()

    index_path = local_root / "edinet_index" / "part.parquet"
    digest = write_index_parquet(index_path, list(documents.values()))
    if state.get("index_digest") != digest:
        uploader(index_path, f"{s3_root}_index/part.parquet")
        state["index_digest"] = digest
    state["document_count"] = len(documents)
    state["last_success_utc"] = datetime.now(tz=UTC).isoformat()
    save_state(state_path, state)
    log.info(
        "EDINET run complete: %s..%s, %d new document(s), %d known",
        start,
        end,
        stored,
        len(documents),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="first submission date to scan (YYYY-MM-DD)")
    parser.add_argument("--until", help="last submission date to scan (YYYY-MM-DD)")
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        return run(
            settings,
            since=date.fromisoformat(args.since) if args.since else None,
            until=date.fromisoformat(args.until) if args.until else None,
        )
    except CorpusError as exc:
        log.error("%s", exc)
        return 2
    except ValueError as exc:
        log.error("invalid date: %s", exc)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
