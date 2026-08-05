"""Compute comparable metrics from the archived XBRL facts.

Phase B of docs/earnings-spec.md. Reads the Parquet the EDGAR loader writes,
applies the three rules production data forced (periodic filings only, newest
filing per period, ratios only between matching units), and writes one row per
symbol and reporting period.

Nothing here is an estimate. A concept the filer never tagged stays null all
the way to the report, because a zero would read as "spent nothing" rather
than "did not say".
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from ..config import Settings, get_settings
from ..logging_setup import configure_logging
from .daily import CorpusError, Uploader, aws_upload, corpus_s3_root, load_state, save_state

log = logging.getLogger(__name__)


def _modules() -> tuple[Any, Any]:
    try:
        import duckdb
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise CorpusError(
            "fundamentals metrics require the 'parquet' package extra"
        ) from exc
    return duckdb, pq


def discover_inputs(local_root: Path) -> tuple[list[Path], Path]:
    facts = sorted((local_root / "fundamentals").glob("symbol=*/part.parquet"))
    if not facts:
        raise CorpusError("no fundamentals Parquet found; run the EDGAR loader first")
    sectors = local_root / "universe" / "sectors.parquet"
    if not sectors.exists():
        raise CorpusError(f"universe sectors Parquet is missing: {sectors}")
    return facts, sectors


def metrics_s3_root(settings: Settings) -> str:
    return f"{corpus_s3_root(settings)}/fundamentals_metrics"


def compute(facts: list[Path], sectors: Path) -> Any:
    duckdb, _ = _modules()
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET threads = 1")
        connection.execute("SET memory_limit = '256MB'")
        connection.from_parquet(
            [str(path) for path in facts], hive_partitioning=False
        ).create_view("fundamentals_input")
        connection.from_parquet(str(sectors), hive_partitioning=False).create_view(
            "sectors_input"
        )
        sql = (
            resources.files("usstocks.corpus")
            .joinpath("sql/fundamentals_metrics.sql")
            .read_text(encoding="utf-8")
        )
        # Split on statement terminators only. A bare split(";") also cuts at
        # semicolons inside comments, which turns prose into a parse error.
        for number, statement in enumerate(re.split(r";\s*\n", sql), start=1):
            if not statement.strip():
                continue
            try:
                connection.execute(statement)
            except duckdb.Error as exc:
                raise CorpusError(f"metrics statement {number} failed: {exc}") from exc
        return connection.execute(
            "SELECT * FROM fundamentals_metrics ORDER BY symbol, period_type, period_end"
        ).to_arrow_table()
    finally:
        connection.close()


def write_metrics(path: Path, table: Any) -> str:
    _, pq = _modules()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    pq.write_table(table, temporary, compression="zstd")
    digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
    temporary.replace(path)
    return digest


def run(settings: Settings, *, uploader: Uploader = aws_upload) -> int:
    local_root = settings.corpus_local_dir
    facts, sectors = discover_inputs(local_root)
    table = compute(facts, sectors)

    path = local_root / "fundamentals_metrics" / "part.parquet"
    digest = write_metrics(path, table)

    state_path = local_root / "fundamentals-metrics-state.json"
    state = load_state(state_path)
    if state.get("digest") != digest:
        uploader(path, f"{metrics_s3_root(settings)}/part.parquet")
        state["digest"] = digest
    state["row_count"] = table.num_rows
    state["last_success_utc"] = datetime.now(tz=UTC).isoformat()
    save_state(state_path, state)

    # Counting what survived each guard is the only way to notice that a
    # provider change quietly emptied a column.
    columns = table.column_names
    covered = {
        name: table.num_rows - table.column(name).null_count
        for name in ("revenue", "gross_margin", "inventory_days", "revenue_yoy")
        if name in columns
    }
    log.info(
        "fundamentals metrics complete: %d row(s) from %d symbol(s); populated %s",
        table.num_rows,
        len(facts),
        ", ".join(f"{name}={count}" for name, count in covered.items()),
    )
    return 0


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
