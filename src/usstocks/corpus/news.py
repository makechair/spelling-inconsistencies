"""Mirror teiten's Notion events to partitioned Parquet for DuckDB analysis.

The teiten pipeline remains the sole writer and source of truth. This oneshot
reads all current pages, normalizes the properties needed for event studies,
and uploads only date partitions whose content changed. A full logical sync is
intentional: at the current few-hundred-page scale it costs a handful of
Notion requests and correctly reflects edits or archives without maintaining a
second fragile cursor protocol.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings, get_settings
from ..logging_setup import configure_logging
from .daily import (
    CorpusError,
    Uploader,
    aws_upload,
    corpus_s3_root,
    load_universe,
    save_state,
)

log = logging.getLogger(__name__)

NOTION_VERSION = "2022-06-28"
XPOST_SEPARATOR = "\n\n■見立て\n"
TICKER_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")
EVENT_TYPES = frozenset(
    {
        "earnings",
        "guidance",
        "product",
        "mna",
        "regulatory",
        "supply_chain",
        "macro",
        "other",
    }
)
SENTIMENTS = frozenset({"positive", "neutral", "negative"})


@dataclass(frozen=True)
class NotionCredentials:
    token: str
    db_id: str


CredentialLoader = Callable[[str], str]


def _ssm_parameter(name: str) -> str:
    try:
        import boto3
    except ImportError as exc:
        raise CorpusError("news corpus requires the 'parquet' package extra") from exc
    try:
        response = boto3.client("ssm").get_parameter(Name=name, WithDecryption=True)
        return str(response["Parameter"]["Value"])
    except Exception as exc:
        # Never put an SDK message in the public error: it can include request
        # metadata and the parameter name is already known from configuration.
        raise CorpusError(f"cannot read Notion credential from SSM: {type(exc).__name__}") from exc


def load_credentials(
    settings: Settings,
    *,
    parameter_loader: CredentialLoader = _ssm_parameter,
) -> NotionCredentials:
    token = (settings.notion_token or "").strip()
    db_id = (settings.notion_db_id or "").strip()
    prefix = settings.notion_ssm_prefix.strip().rstrip("/")
    if not prefix.startswith("/"):
        raise CorpusError("USSTOCKS_NOTION_SSM_PREFIX must start with /")
    if not token:
        token = parameter_loader(f"{prefix}/notion-token").strip()
    if not db_id:
        db_id = parameter_loader(f"{prefix}/notion-db-id").strip()
    if not token or not db_id:
        raise CorpusError("Notion credentials are empty")
    return NotionCredentials(token=token, db_id=db_id)


def news_s3_root(settings: Settings) -> str:
    explicit = (settings.news_s3_uri or "").strip().rstrip("/")
    return explicit or f"{corpus_s3_root(settings)}/news"


def news_rejected_s3_root(settings: Settings) -> str:
    """Sibling of the news prefix, so the existing corpus/* IAM grant covers it.

    Kept out of news/ itself: everything under that prefix is a date partition
    the event study reads, and a rejects file there would join the analysis.
    """
    return f"{news_s3_root(settings)}_rejected"


def fetch_pages(
    client: httpx.Client,
    settings: Settings,
    credentials: NotionCredentials,
) -> list[dict[str, Any]]:
    headers = {
        "Authorization": f"Bearer {credentials.token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    endpoint = f"{settings.notion_api_base.rstrip('/')}/databases/{credentials.db_id}/query"
    pages: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        payload: dict[str, object] = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        response: httpx.Response | None = None
        for attempt in range(3):
            try:
                response = client.post(endpoint, headers=headers, json=payload)
            except httpx.HTTPError as exc:
                raise CorpusError(f"Notion request failed: {type(exc).__name__}") from exc
            if response.status_code not in {429, 500, 502, 503, 504}:
                break
            if attempt == 2:
                break
            try:
                retry_after = min(float(response.headers.get("Retry-After", "1")), 10.0)
            except ValueError:
                retry_after = 1.0
            log.warning(
                "Notion query returned HTTP %d; retrying in %.1fs",
                response.status_code,
                retry_after,
            )
            import time

            time.sleep(retry_after)
        assert response is not None
        if response.status_code >= 400:
            raise CorpusError(f"Notion returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise CorpusError("Notion returned invalid JSON") from exc
        result = body.get("results")
        if not isinstance(result, list):
            raise CorpusError("Notion response has no results list")
        for page in result:
            if not isinstance(page, dict):
                raise CorpusError("Notion result contains a non-object page")
            pages.append(page)
        if len(pages) > settings.news_max_pages:
            raise CorpusError(f"Notion page count exceeds safety limit {settings.news_max_pages}")
        if not body.get("has_more"):
            return pages
        cursor_value = body.get("next_cursor")
        if not isinstance(cursor_value, str) or not cursor_value:
            raise CorpusError("Notion pagination cursor is missing")
        cursor = cursor_value


def _rich_text(properties: dict[str, Any], name: str, kind: str = "rich_text") -> str:
    fragments = properties.get(name, {}).get(kind, [])
    if not isinstance(fragments, list):
        return ""
    return "".join(
        str(fragment.get("plain_text", "")) for fragment in fragments if isinstance(fragment, dict)
    )


def _select(properties: dict[str, Any], name: str) -> str | None:
    selected = properties.get(name, {}).get("select")
    if not isinstance(selected, dict):
        return None
    value = selected.get("name")
    return str(value) if value is not None else None


def _property_date(properties: dict[str, Any], name: str) -> str | None:
    selected = properties.get(name, {}).get("date")
    if not isinstance(selected, dict):
        return None
    value = selected.get("start")
    return str(value) if value else None


def _event_date(page: dict[str, Any], properties: dict[str, Any]) -> date:
    candidate = _property_date(properties, "PublishedAt") or page.get("created_time")
    if not isinstance(candidate, str) or len(candidate) < 10:
        raise CorpusError(f"Notion page {page.get('id', '<unknown>')} has no event date")
    try:
        return date.fromisoformat(candidate[:10])
    except ValueError as exc:
        raise CorpusError(
            f"Notion page {page.get('id', '<unknown>')} has an invalid event date"
        ) from exc


def normalize_page(page: dict[str, Any]) -> dict[str, object]:
    page_id = page.get("id")
    properties = page.get("properties")
    if not isinstance(page_id, str) or not isinstance(properties, dict):
        raise CorpusError("Notion page is missing id or properties")

    xpost = _rich_text(properties, "XPost")
    summary, separator, my_take = xpost.partition(XPOST_SEPARATOR)
    if not separator:
        summary, my_take = xpost, ""

    tickers_property = properties.get("Tickers", {}).get("multi_select", [])
    if not isinstance(tickers_property, list):
        raise CorpusError(f"Notion page {page_id} has invalid Tickers")
    tickers: list[str] = []
    for option in tickers_property:
        ticker = str(option.get("name", "")).strip().upper() if isinstance(option, dict) else ""
        if ticker and not TICKER_PATTERN.fullmatch(ticker):
            raise CorpusError(f"Notion page {page_id} has invalid ticker {ticker!r}")
        if ticker and ticker not in tickers:
            tickers.append(ticker)

    event_type = _select(properties, "EventType")
    sentiment = _select(properties, "Sentiment")
    if event_type is not None and event_type not in EVENT_TYPES:
        raise CorpusError(f"Notion page {page_id} has invalid EventType")
    if sentiment is not None and sentiment not in SENTIMENTS:
        raise CorpusError(f"Notion page {page_id} has invalid Sentiment")

    confidence = properties.get("Confidence", {}).get("number")
    if confidence is not None:
        try:
            confidence = float(confidence)
        except (TypeError, ValueError) as exc:
            raise CorpusError(f"Notion page {page_id} has invalid Confidence") from exc
        if not 0.0 <= confidence <= 1.0:
            raise CorpusError(f"Notion page {page_id} has Confidence outside 0..1")

    importance = properties.get("Importance", {}).get("number")
    sources_text = _rich_text(properties, "Sources")
    return {
        "page_id": page_id,
        "event_date": _event_date(page, properties),
        "published_at": _property_date(properties, "PublishedAt"),
        "created_at": str(page.get("created_time") or ""),
        "last_edited_at": str(page.get("last_edited_time") or ""),
        "headline": _rich_text(properties, "summary_ja", "title"),
        "summary_ja": summary.strip(),
        "my_take": my_take.strip(),
        "status": _select(properties, "Status"),
        "importance": int(importance) if importance is not None else None,
        "category": _select(properties, "Category"),
        "source": _rich_text(properties, "Source"),
        "url": properties.get("URL", {}).get("url"),
        "sources": [value for value in sources_text.split() if value],
        "image_url": properties.get("ImageURL", {}).get("url"),
        "tickers": tickers,
        "event_type": event_type,
        "sentiment": sentiment,
        "confidence": confidence,
        "notion_url": str(page.get("url") or ""),
    }


def _parquet_modules() -> tuple[Any, Any]:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise CorpusError("news corpus requires the 'parquet' package extra") from exc
    return pa, pq


def _schema() -> Any:
    pa, _ = _parquet_modules()
    return pa.schema(
        [
            ("page_id", pa.string()),
            ("event_date", pa.date32()),
            ("published_at", pa.string()),
            ("created_at", pa.string()),
            ("last_edited_at", pa.string()),
            ("headline", pa.string()),
            ("summary_ja", pa.string()),
            ("my_take", pa.string()),
            ("status", pa.string()),
            ("importance", pa.int64()),
            ("category", pa.string()),
            ("source", pa.string()),
            ("url", pa.string()),
            ("sources", pa.list_(pa.string())),
            ("image_url", pa.string()),
            ("tickers", pa.list_(pa.string())),
            ("event_type", pa.string()),
            ("sentiment", pa.string()),
            ("confidence", pa.float64()),
            ("notion_url", pa.string()),
        ]
    )


def _rejected_schema() -> Any:
    pa, _ = _parquet_modules()
    return pa.schema(
        [
            ("page_id", pa.string()),
            ("notion_url", pa.string()),
            ("reason", pa.string()),
            ("detected_at", pa.string()),
        ]
    )


def write_rejected(path: Path, rows: Sequence[dict[str, object]]) -> str:
    pa, pq = _parquet_modules()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    ordered = sorted(rows, key=lambda row: str(row["page_id"]))
    pq.write_table(
        pa.Table.from_pylist(ordered, schema=_rejected_schema()),
        temporary,
        compression="zstd",
    )
    digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
    temporary.replace(path)
    return digest


def write_partition(path: Path, rows: Sequence[dict[str, object]]) -> str:
    pa, pq = _parquet_modules()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    ordered = sorted(rows, key=lambda row: str(row["page_id"]))
    pq.write_table(
        pa.Table.from_pylist(ordered, schema=_schema()),
        temporary,
        compression="zstd",
    )
    digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
    temporary.replace(path)
    return digest


def load_news_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "partitions": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorpusError(f"cannot read news corpus state: {type(exc).__name__}") from exc
    if state.get("version") != 1 or not isinstance(state.get("partitions"), dict):
        raise CorpusError("unsupported news corpus state format")
    return state


def run(
    settings: Settings,
    *,
    uploader: Uploader = aws_upload,
    client: httpx.Client | None = None,
    parameter_loader: CredentialLoader = _ssm_parameter,
) -> int:
    credentials = load_credentials(settings, parameter_loader=parameter_loader)
    own_client = client is None
    http_client = client or httpx.Client(timeout=settings.notion_timeout_seconds)
    try:
        pages = fetch_pages(http_client, settings, credentials)
    finally:
        if own_client:
            http_client.close()

    # One page with an out-of-enum value used to take the whole day down with
    # it. Isolating the offenders keeps the intent -- nothing malformed reaches
    # the analysis data -- without letting a single stray page hide the other
    # four hundred. A majority failing is different in kind: that is a schema
    # change or a bad deploy, and continuing would quietly gut the corpus.
    normalized: list[dict[str, object]] = []
    rejected: list[dict[str, object]] = []
    detected_at = datetime.now(tz=UTC).isoformat()
    for page in pages:
        try:
            normalized.append(normalize_page(page))
        except CorpusError as exc:
            rejected.append(
                {
                    "page_id": str(page.get("id", "<unknown>")),
                    "notion_url": str(page.get("url", "")),
                    "reason": str(exc),
                    "detected_at": detected_at,
                }
            )
    if rejected and len(rejected) > len(normalized):
        raise CorpusError(
            f"{len(rejected)} of {len(pages)} Notion pages failed validation; "
            "refusing to rewrite the corpus from the minority that passed"
        )
    for row in rejected:
        log.error("rejected %s %s: %s", row["page_id"], row["notion_url"], row["reason"])

    if len({str(row["page_id"]) for row in normalized}) != len(normalized):
        raise CorpusError("Notion response contains duplicate page ids")
    by_date: dict[str, list[dict[str, object]]] = {}
    for row in normalized:
        key = row["event_date"].isoformat()  # type: ignore[union-attr]
        by_date.setdefault(key, []).append(row)

    local_root = settings.corpus_local_dir
    state_path = local_root / "news-state.json"
    state = load_news_state(state_path)
    previous = state["partitions"]
    changed = 0
    s3_root = news_s3_root(settings)
    for day in sorted(set(by_date) | set(previous)):
        path = local_root / "news" / f"date={day}" / "part.parquet"
        digest = write_partition(path, by_date.get(day, []))
        if previous.get(day) == digest:
            continue
        uploader(path, f"{s3_root}/date={day}/part.parquet")
        previous[day] = digest
        state["last_success_utc"] = datetime.now(tz=UTC).isoformat()
        save_state(state_path, state)
        changed += 1
    # Written every run, including when it goes back to empty, so the object
    # states what is wrong now rather than accumulating pages already fixed.
    rejected_path = local_root / "news_rejected" / "part.parquet"
    rejected_digest = write_rejected(rejected_path, rejected)
    if state.get("rejected_digest") != rejected_digest:
        uploader(rejected_path, f"{news_rejected_s3_root(settings)}/part.parquet")
        state["rejected_digest"] = rejected_digest
        changed += 1

    state["page_count"] = len(normalized)
    state["rejected_count"] = len(rejected)
    state["last_success_utc"] = datetime.now(tz=UTC).isoformat()
    save_state(state_path, state)
    log.info(
        "news corpus sync complete: %d page(s), %d rejected, %d partition(s), %d uploaded",
        len(normalized),
        len(rejected),
        len(by_date),
        changed,
    )
    return 0


def check(
    settings: Settings,
    *,
    client: httpx.Client | None = None,
    parameter_loader: CredentialLoader = _ssm_parameter,
) -> int:
    """Report what a sync would reject, without writing Parquet or touching S3.

    `run()` aborts the whole day on the first offending page, which is the right
    default for a corpus but useless for finding out how many pages are wrong.
    This reads the same pages through the same validation and keeps going, so a
    change on the writing side can be verified before the next timer fires.
    """
    credentials = load_credentials(settings, parameter_loader=parameter_loader)
    own_client = client is None
    http_client = client or httpx.Client(timeout=settings.notion_timeout_seconds)
    try:
        pages = fetch_pages(http_client, settings, credentials)
    finally:
        if own_client:
            http_client.close()

    # The news unit has no reason to carry USSTOCKS_CORPUS_UNIVERSE_PATH, so the
    # default resolves next to the source tree and is absent from an installed
    # release. That costs one of three reports, not the validation itself.
    try:
        universe: set[str] | None = {
            entry.symbol for entry in load_universe(settings.corpus_universe_path)
        }
    except (OSError, CorpusError) as exc:
        universe = None
        log.warning(
            "universe unavailable (%s: %s); skipping the out-of-universe ticker report",
            settings.corpus_universe_path,
            type(exc).__name__,
        )
    rejected: list[tuple[str, str, str]] = []
    event_types: dict[str, int] = {}
    sentiments: dict[str, int] = {}
    outside_universe: dict[str, int] = {}
    tickerless = 0

    for page in pages:
        try:
            row = normalize_page(page)
        except CorpusError as exc:
            rejected.append(
                (
                    str(page.get("id", "<unknown>")),
                    str(page.get("url", "")),
                    str(exc),
                )
            )
            continue
        event_types[str(row["event_type"])] = event_types.get(str(row["event_type"]), 0) + 1
        sentiments[str(row["sentiment"])] = sentiments.get(str(row["sentiment"]), 0) + 1
        tickers = row["tickers"]
        assert isinstance(tickers, list)
        if not tickers:
            tickerless += 1
        if universe is not None:
            for ticker in tickers:
                if ticker not in universe:
                    outside_universe[ticker] = outside_universe.get(ticker, 0) + 1

    accepted = len(pages) - len(rejected)
    log.info(
        "news corpus check: %d page(s), %d accepted, %d rejected",
        len(pages),
        accepted,
        len(rejected),
    )
    for page_id, url, reason in rejected:
        log.error("rejected %s %s: %s", page_id, url, reason)

    # A page that parses can still be useless: an event with no in-universe
    # ticker never joins a price series, and a run that is nearly all
    # other/neutral means the model is filling required fields, not classifying.
    if accepted:
        log.info("no ticker: %d of %d accepted page(s)", tickerless, accepted)
        for label, counts in (("event_type", event_types), ("sentiment", sentiments)):
            ranked = sorted(counts.items(), key=lambda item: item[1], reverse=True)
            log.info(
                "%s: %s",
                label,
                ", ".join(f"{name}={count}" for name, count in ranked) or "none",
            )
    if outside_universe:
        ranked = sorted(outside_universe.items(), key=lambda item: item[1], reverse=True)
        log.warning(
            "tickers outside universe.csv (kept, but they never match a price series): %s",
            ", ".join(f"{name}={count}" for name, count in ranked),
        )
    return 1 if rejected else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate every Notion page and report, without writing Parquet or S3",
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        return check(settings) if args.check else run(settings)
    except CorpusError as exc:
        log.error("%s", exc)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
