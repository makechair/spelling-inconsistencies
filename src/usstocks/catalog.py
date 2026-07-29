"""Import the provider's ticker universe for local symbol search.

Search used to call the provider for every distinct query, spending the same
50 calls/hour the collector needs for backfill (spec-review A-2). Autocomplete
could take the whole allowance on its own.

Tiingo publishes its supported tickers as a static zip rather than an API
endpoint, so downloading it costs no requests. Using *that* file, rather than a
different provider's ticker list, is the point: a symbol found in search is one
the price endpoints actually serve. Mixing sources produces the failure this
project already met by hand -- a ticker that looks valid, is accepted into the
watchlist, and then returns no bars.

Run by ``usstocks-catalog.timer``; see docs/operations.md.
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import tempfile
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx

from .config import Settings, get_settings
from .db.repository import Repository
from .logging_setup import configure_logging

log = logging.getLogger(__name__)

# Written in chunks rather than one transaction. The file holds ~100k rows, and
# a single transaction that size holds the write lock long enough for the
# collector's next bar write to exhaust its busy timeout. Chunks interleave;
# the refreshed_at stamp is what keeps the result consistent.
CHUNK_ROWS = 2_000

WANTED_ASSET_TYPES = frozenset({"Stock", "ETF"})


def _download(url: str, destination: Path, timeout: float) -> int:
    """Stream the archive to disk. Never read it whole: this runs on 1 GB."""
    total = 0
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as response:
        response.raise_for_status()
        with destination.open("wb") as handle:
            for chunk in response.iter_bytes(64 * 1024):
                handle.write(chunk)
                total += len(chunk)
    return total


def _rows(archive: Path, *, keep_after: datetime) -> Iterator[tuple]:
    """Yield catalog rows from the zip, streaming and filtered.

    The file lists every ticker the provider has ever carried, including long
    delisted ones. Keeping them would bury a search for a live symbol under
    decades of dead ones, so entries whose coverage ended before ``keep_after``
    are dropped.
    """
    with zipfile.ZipFile(archive) as bundle:
        members = [name for name in bundle.namelist() if name.lower().endswith(".csv")]
        if not members:
            raise RuntimeError(f"no csv member in {archive.name}")
        with bundle.open(members[0]) as raw:
            reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", errors="replace"))
            for record in reader:
                symbol = (record.get("ticker") or "").strip().upper()
                if not symbol:
                    continue
                asset_type = (record.get("assetType") or "").strip()
                if asset_type not in WANTED_ASSET_TYPES:
                    continue
                if (record.get("priceCurrency") or "").strip().upper() != "USD":
                    continue
                end_date = (record.get("endDate") or "").strip()
                if end_date and end_date < keep_after.strftime("%Y-%m-%d"):
                    continue
                yield (
                    symbol,
                    (record.get("name") or "").strip() or None,
                    (record.get("exchange") or "").strip() or None,
                    asset_type,
                    "USD",
                    (record.get("startDate") or "").strip() or None,
                    end_date or None,
                )


def import_catalog(settings: Settings | None = None) -> int:
    """Refresh ``symbol_catalog``. Returns the number of rows kept."""
    settings = settings or get_settings()
    # Microseconds, and a token, because this value identifies a run rather
    # than recording a time. Second resolution makes two imports in the same
    # second indistinguishable, and then the prune keeps everything -- the
    # rows the new import did not carry look like rows it did.
    stamp = f"{datetime.now(tz=UTC):%Y-%m-%dT%H:%M:%S.%f}Z-{uuid4().hex[:8]}"
    keep_after = datetime.now(tz=UTC) - timedelta(days=settings.catalog_keep_days)

    # NamedTemporaryFile(delete=False) plus an explicit unlink: the archive has
    # to be seekable for zipfile, and it must not survive a crash on a host
    # where disk is the scarce resource.
    handle = tempfile.NamedTemporaryFile(
        prefix="usstocks-catalog-", suffix=".zip", delete=False
    )
    archive = Path(handle.name)
    handle.close()

    written = 0
    try:
        size = _download(settings.catalog_url, archive, settings.catalog_timeout_seconds)
        log.info("downloaded %s (%.1f MB)", settings.catalog_url, size / 1_048_576)

        with Repository(
            settings.db_path,
            busy_timeout_ms=settings.sqlite_busy_timeout_ms,
            source_priority=settings.source_priority,
        ) as repo:
            batch: list[tuple] = []
            for row in _rows(archive, keep_after=keep_after):
                batch.append((*row, stamp))
                if len(batch) >= CHUNK_ROWS:
                    written += repo.upsert_catalog(batch)
                    batch.clear()
            if batch:
                written += repo.upsert_catalog(batch)

            if written == 0:
                # A truncated or reshaped file must not wipe a working catalog.
                raise RuntimeError("catalog import produced no rows; keeping the previous one")
            removed = repo.prune_catalog(stamp)
            log.info("catalog refreshed: %d symbols kept, %d dropped", written, removed)
    finally:
        # Runs on the success path too: the archive is the largest thing this
        # process touches and there is no reason to keep it once imported.
        try:
            os.unlink(archive)
        except FileNotFoundError:  # pragma: no cover - already gone
            pass

    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        import_catalog(settings)
    except Exception as exc:  # noqa: BLE001 - the timer wants a non-zero exit
        log.error("catalog import failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
