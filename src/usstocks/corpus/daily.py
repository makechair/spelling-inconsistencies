"""Incrementally build adjusted Tiingo daily bars as partitioned Parquet.

The provider's monthly unique-symbol cap is unknown, so the unattended timer
adds only a few new symbols per run. Existing symbols are refreshed oldest
first. All HTTP calls spend from the collector's persistent ``RestBudget``;
the timer cannot silently create a second allowance.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from ..collector.ratelimit import RestBudget
from ..config import Settings, get_settings
from ..db.migrate import migrate
from ..db.repository import Repository
from ..logging_setup import configure_logging

log = logging.getLogger(__name__)

JST = ZoneInfo("Asia/Tokyo")
INITIAL_START = date(1990, 1, 1)
PRICE_COLUMNS = (
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "adjOpen",
    "adjHigh",
    "adjLow",
    "adjClose",
    "adjVolume",
    "divCash",
    "splitFactor",
)


class CorpusError(RuntimeError):
    """An error safe to surface without exposing an API token."""


class ProviderResponseError(CorpusError):
    def __init__(self, symbol: str, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"Tiingo returned HTTP {status_code} for {symbol}")


@dataclass(frozen=True)
class UniverseEntry:
    symbol: str
    subsector: str


Uploader = Callable[[Path, str], None]


def load_universe(path: Path) -> list[UniverseEntry]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["symbol", "subsector"]:
            raise CorpusError("universe CSV must have exactly: symbol,subsector")
        entries: list[UniverseEntry] = []
        seen: set[str] = set()
        for line_number, row in enumerate(reader, start=2):
            symbol = (row.get("symbol") or "").strip().upper()
            subsector = (row.get("subsector") or "").strip()
            if not symbol or not subsector:
                raise CorpusError(f"universe CSV line {line_number} has an empty field")
            if symbol in seen:
                raise CorpusError(f"duplicate universe symbol: {symbol}")
            seen.add(symbol)
            entries.append(UniverseEntry(symbol, subsector))
    if not entries:
        raise CorpusError("universe CSV is empty")
    return entries


def corpus_s3_root(settings: Settings) -> str:
    explicit = (settings.corpus_s3_uri or "").strip().rstrip("/")
    if explicit:
        return explicit
    backup = (settings.backup_s3_uri or "").strip().rstrip("/")
    if not backup:
        raise CorpusError("set USSTOCKS_CORPUS_S3_URI or USSTOCKS_BACKUP_S3_URI")
    return f"{backup}/corpus"


def in_closed_market_window(moment: datetime) -> bool:
    local = moment.astimezone(JST)
    return 9 <= local.hour < 17


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "symbols": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorpusError(f"cannot read corpus state: {type(exc).__name__}") from exc
    if payload.get("version") != 1 or not isinstance(payload.get("symbols"), dict):
        raise CorpusError("unsupported corpus state format")
    return payload


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _parse_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or len(value) < 10:
        raise CorpusError("daily row has an invalid date")
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise CorpusError("daily row has an invalid date") from exc


def normalize_rows(symbol: str, rows: object) -> list[dict[str, object]]:
    if not isinstance(rows, list) or not rows:
        raise CorpusError(f"Tiingo returned no daily rows for {symbol}")
    normalized: list[dict[str, object]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise CorpusError(f"{symbol} daily row {index} is not an object")
        missing = [column for column in PRICE_COLUMNS if column not in row]
        if missing:
            raise CorpusError(f"{symbol} daily row {index} is missing: {', '.join(missing)}")
        try:
            normalized.append(
                {
                    "symbol": symbol,
                    "date": _parse_date(row["date"]),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": int(row["volume"]),
                    "adjOpen": float(row["adjOpen"]),
                    "adjHigh": float(row["adjHigh"]),
                    "adjLow": float(row["adjLow"]),
                    "adjClose": float(row["adjClose"]),
                    "adjVolume": float(row["adjVolume"]),
                    "divCash": float(row["divCash"]),
                    "splitFactor": float(row["splitFactor"]),
                }
            )
        except (TypeError, ValueError) as exc:
            raise CorpusError(f"{symbol} daily row {index} has invalid numbers") from exc
    return normalized


def fetch_rows(
    client: httpx.Client,
    budget: RestBudget,
    *,
    base_url: str,
    token: str,
    symbol: str,
    start: date,
) -> list[dict[str, object]]:
    if not budget.try_acquire():
        raise CorpusError("REST budget unavailable")
    try:
        response = client.get(
            f"{base_url.rstrip('/')}/tiingo/daily/{symbol.lower()}/prices",
            params={"startDate": start.isoformat(), "token": token},
        )
    except httpx.HTTPError as exc:
        # HTTP exception strings may include the token-bearing request URL.
        raise CorpusError(f"Tiingo request failed for {symbol}: {type(exc).__name__}") from exc
    budget.record_bytes(len(response.content))
    if response.status_code >= 400:
        raise ProviderResponseError(symbol, response.status_code)
    try:
        payload = response.json()
    except ValueError as exc:
        raise CorpusError(f"Tiingo returned invalid JSON for {symbol}") from exc
    return normalize_rows(symbol, payload)


def _parquet_modules() -> tuple[Any, Any]:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise CorpusError("daily corpus requires the 'parquet' package extra") from exc
    return pa, pq


def read_parquet_rows(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    _, pq = _parquet_modules()
    return pq.read_table(path).to_pylist()


def merge_rows(
    existing: Iterable[dict[str, object]],
    incoming: Iterable[dict[str, object]],
) -> list[dict[str, object]]:
    by_date: dict[date, dict[str, object]] = {}
    for row in existing:
        by_date[_parse_date(row["date"])] = row
    for row in incoming:
        by_date[_parse_date(row["date"])] = row
    return [by_date[key] for key in sorted(by_date)]


def needs_full_refresh(
    existing: Iterable[dict[str, object]],
    incoming: Iterable[dict[str, object]],
) -> bool:
    old = {_parse_date(row["date"]): row for row in existing}
    for row in incoming:
        split = float(row["splitFactor"])
        dividend = float(row["divCash"])
        if split == 1.0 and dividend == 0.0:
            continue
        previous = old.get(_parse_date(row["date"]))
        if previous is None:
            return True
        if float(previous["splitFactor"]) != split or float(previous["divCash"]) != dividend:
            return True
    return False


def write_daily_parquet(path: Path, rows: Sequence[dict[str, object]]) -> None:
    pa, pq = _parquet_modules()
    schema = pa.schema(
        [
            ("symbol", pa.string()),
            ("date", pa.date32()),
            ("open", pa.float64()),
            ("high", pa.float64()),
            ("low", pa.float64()),
            ("close", pa.float64()),
            ("volume", pa.int64()),
            ("adjOpen", pa.float64()),
            ("adjHigh", pa.float64()),
            ("adjLow", pa.float64()),
            ("adjClose", pa.float64()),
            ("adjVolume", pa.float64()),
            ("divCash", pa.float64()),
            ("splitFactor", pa.float64()),
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    pq.write_table(
        pa.Table.from_pylist(list(rows), schema=schema),
        temporary,
        compression="zstd",
    )
    os.replace(temporary, path)


def write_universe_parquet(path: Path, entries: Sequence[UniverseEntry]) -> None:
    pa, pq = _parquet_modules()
    schema = pa.schema([("symbol", pa.string()), ("subsector", pa.string())])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    pq.write_table(
        pa.Table.from_pylist(
            [{"symbol": entry.symbol, "subsector": entry.subsector} for entry in entries],
            schema=schema,
        ),
        temporary,
        compression="zstd",
    )
    os.replace(temporary, path)


def _parse_s3_destination(destination: str) -> tuple[str, str]:
    parsed = urlparse(destination)
    key = parsed.path.lstrip("/")
    if parsed.scheme != "s3" or not parsed.netloc or not key:
        raise CorpusError("corpus upload destination must be s3://bucket/key")
    return parsed.netloc, key


def aws_upload(local_path: Path, destination: str) -> None:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise CorpusError("daily corpus requires the 'parquet' package extra") from exc
    bucket, key = _parse_s3_destination(destination)
    try:
        # The host's bundled AWS CLI/botocore transport cannot resolve AWS
        # endpoints even though the OS resolver and httpx can. Let botocore do
        # only SigV4 signing, then send the short-lived URL over the same httpx
        # stack already used successfully for provider traffic.
        client = boto3.client("s3", config=Config(signature_version="s3v4"))
        signed_url = client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": bucket,
                "Key": key,
                "ServerSideEncryption": "AES256",
            },
            ExpiresIn=900,
            HttpMethod="PUT",
        )
        response = httpx.put(
            signed_url,
            content=local_path.read_bytes(),
            headers={"x-amz-server-side-encryption": "AES256"},
            timeout=120.0,
        )
        if response.status_code >= 400:
            raise CorpusError(f"S3 upload returned HTTP {response.status_code}")
    except CorpusError:
        raise
    except Exception as exc:
        # Signed URLs and SDK exception messages can contain credentials or
        # request metadata. Keep logs stable and secret-free.
        raise CorpusError(f"S3 upload failed: {type(exc).__name__}") from exc


def _last_success(entry_state: dict[str, Any]) -> datetime:
    value = entry_state.get("last_success_utc")
    if not isinstance(value, str):
        return datetime.min.replace(tzinfo=UTC)
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)


def select_entries(
    entries: Sequence[UniverseEntry],
    state: dict[str, Any],
    local_root: Path,
    *,
    now: datetime,
    max_symbols: int,
    max_new_symbols: int,
    refresh_hours: float,
) -> list[UniverseEntry]:
    symbol_state = state["symbols"]
    new: list[UniverseEntry] = []
    due: list[UniverseEntry] = []
    cutoff = now - timedelta(hours=refresh_hours)
    for entry in entries:
        path = local_root / "daily" / f"symbol={entry.symbol}" / "part.parquet"
        current = symbol_state.get(entry.symbol, {})
        if not path.exists():
            new.append(entry)
        elif _last_success(current) <= cutoff:
            due.append(entry)
    new_selected = new[: min(max_new_symbols, max_symbols)]
    due.sort(key=lambda item: _last_success(symbol_state.get(item.symbol, {})))
    return new_selected + due[: max(0, max_symbols - len(new_selected))]


def _upload_pending(
    entries: Sequence[UniverseEntry],
    state: dict[str, Any],
    local_root: Path,
    s3_root: str,
    uploader: Uploader,
    now: datetime,
) -> int:
    uploaded = 0
    for entry in entries:
        current = state["symbols"].get(entry.symbol, {})
        if not current.get("pending_upload"):
            continue
        local_path = local_root / "daily" / f"symbol={entry.symbol}" / "part.parquet"
        if not local_path.exists():
            current["last_error"] = "pending Parquet file is missing"
            state["symbols"][entry.symbol] = current
            continue
        uploader(
            local_path,
            f"{s3_root}/daily/symbol={entry.symbol}/part.parquet",
        )
        current["pending_upload"] = False
        current["last_success_utc"] = now.isoformat()
        current["last_error"] = None
        state["symbols"][entry.symbol] = current
        uploaded += 1
    return uploaded


def run(
    settings: Settings,
    *,
    now: datetime | None = None,
    force: bool = False,
    max_symbols: int | None = None,
    max_new_symbols: int | None = None,
    uploader: Uploader = aws_upload,
    client: httpx.Client | None = None,
) -> int:
    moment = (now or datetime.now(tz=UTC)).astimezone(UTC)
    if not force and not in_closed_market_window(moment):
        log.info("outside 09:00-17:00 JST; daily corpus run skipped")
        return 0
    token = (settings.tiingo_api_key or "").strip()
    if not token:
        raise CorpusError("USSTOCKS_TIINGO_API_KEY is not set")

    symbol_limit = settings.corpus_max_symbols_per_run if max_symbols is None else max_symbols
    new_symbol_limit = (
        settings.corpus_max_new_symbols_per_run if max_new_symbols is None else max_new_symbols
    )
    if symbol_limit < 1:
        raise CorpusError("max symbols per run must be at least 1")
    if new_symbol_limit < 0:
        raise CorpusError("max new symbols per run cannot be negative")
    if new_symbol_limit > symbol_limit:
        raise CorpusError("max new symbols per run cannot exceed max symbols per run")

    entries = load_universe(settings.corpus_universe_path)
    local_root = settings.corpus_local_dir
    local_root.mkdir(parents=True, exist_ok=True)
    state_path = local_root / "state.json"
    state = load_state(state_path)
    s3_root = corpus_s3_root(settings)

    universe_digest = hashlib.sha256(settings.corpus_universe_path.read_bytes()).hexdigest()
    if state.get("universe_digest") != universe_digest:
        universe_path = local_root / "universe" / "sectors.parquet"
        write_universe_parquet(universe_path, entries)
        uploader(universe_path, f"{s3_root}/universe/sectors.parquet")
        state["universe_digest"] = universe_digest
        save_state(state_path, state)

    try:
        pending_count = _upload_pending(entries, state, local_root, s3_root, uploader, moment)
    finally:
        save_state(state_path, state)
    if pending_count:
        log.info("uploaded %d previously staged partition(s)", pending_count)

    selected = select_entries(
        entries,
        state,
        local_root,
        now=moment,
        max_symbols=symbol_limit,
        max_new_symbols=new_symbol_limit,
        refresh_hours=settings.corpus_refresh_hours,
    )
    if not selected:
        log.info("all daily corpus partitions are fresh")
        return 0

    migrate(settings.db_path, busy_timeout_ms=settings.sqlite_busy_timeout_ms)
    repository = Repository(
        settings.db_path,
        busy_timeout_ms=settings.sqlite_busy_timeout_ms,
        source_priority=settings.source_priority,
    )
    budget = RestBudget(
        repository,
        "tiingo",
        per_hour=settings.rest_calls_per_hour,
        per_day=settings.rest_calls_per_day,
        monthly_bandwidth_bytes=settings.monthly_bandwidth_bytes,
    )
    own_client = client is None
    http_client = client or httpx.Client(timeout=120.0)
    completed = 0
    try:
        for entry in selected:
            current = state["symbols"].get(entry.symbol, {})
            current["last_attempt_utc"] = moment.isoformat()
            path = local_root / "daily" / f"symbol={entry.symbol}" / "part.parquet"
            existing = read_parquet_rows(path)
            start = (
                max(
                    INITIAL_START,
                    _parse_date(existing[-1]["date"])
                    - timedelta(days=settings.corpus_overlap_days),
                )
                if existing
                else INITIAL_START
            )
            try:
                incoming = fetch_rows(
                    http_client,
                    budget,
                    base_url=settings.tiingo_rest_base,
                    token=token,
                    symbol=entry.symbol,
                    start=start,
                )
                if existing and needs_full_refresh(existing, incoming):
                    log.info(
                        "%s has a new split/dividend; refreshing adjusted history",
                        entry.symbol,
                    )
                    incoming = fetch_rows(
                        http_client,
                        budget,
                        base_url=settings.tiingo_rest_base,
                        token=token,
                        symbol=entry.symbol,
                        start=INITIAL_START,
                    )
                    merged = incoming
                else:
                    merged = merge_rows(existing, incoming)
                write_daily_parquet(path, merged)
                current.update(
                    {
                        "last_fetch_utc": moment.isoformat(),
                        "last_bar_date": _parse_date(merged[-1]["date"]).isoformat(),
                        "pending_upload": True,
                        "last_error": None,
                    }
                )
                state["symbols"][entry.symbol] = current
                save_state(state_path, state)
                uploader(
                    path,
                    f"{s3_root}/daily/symbol={entry.symbol}/part.parquet",
                )
                current["pending_upload"] = False
                current["last_success_utc"] = moment.isoformat()
                state["symbols"][entry.symbol] = current
                save_state(state_path, state)
                completed += 1
                log.info(
                    "%s: %d rows through %s",
                    entry.symbol,
                    len(merged),
                    current["last_bar_date"],
                )
            except CorpusError as exc:
                current["last_error"] = str(exc)
                state["symbols"][entry.symbol] = current
                save_state(state_path, state)
                if str(exc) == "REST budget unavailable":
                    log.info("REST budget unavailable; remaining symbols deferred")
                    break
                log.error("%s", exc)
                if current.get("pending_upload"):
                    log.error("stopping after S3 upload failure; staged partition will retry")
                    break
                if isinstance(exc, ProviderResponseError):
                    if 400 <= exc.status_code < 500:
                        log.error("stopping after provider 4xx; inspect the unique-symbol limit")
                    else:
                        log.error("stopping after provider server error")
                    break
    finally:
        if own_client:
            http_client.close()
        repository.close()
    log.info("daily corpus run complete: %d/%d partition(s)", completed, len(selected))
    return 0 if completed == len(selected) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="ignore the JST safety window")
    parser.add_argument("--max-symbols", type=int, default=None)
    parser.add_argument("--max-new-symbols", type=int, default=None)
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        return run(
            settings,
            force=args.force,
            max_symbols=args.max_symbols,
            max_new_symbols=args.max_new_symbols,
        )
    except CorpusError as exc:
        log.error("%s", exc)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
