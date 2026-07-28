"""Forward-only SQL migrations (spec 4.6).

Each ``NNNN_name.sql`` in ``migrations/`` runs once, in filename order, inside
a transaction, and is recorded in ``schema_migrations``.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from ..config import get_settings
from ..logging_setup import configure_logging
from .connection import connect

log = logging.getLogger(__name__)
MIGRATIONS_DIR = Path(__file__).parent / "migrations"

LIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS live_state (
    symbol         TEXT PRIMARY KEY,
    last_price     REAL,
    last_trade_utc TEXT,
    session        TEXT,
    prev_close     REAL,
    bar_json       TEXT,
    source         TEXT,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collector_status (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    status_json TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""


def applied_versions(conn: sqlite3.Connection) -> set[str]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    return {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}


def migrate(db_path: Path, *, busy_timeout_ms: int = 5_000) -> list[str]:
    conn = connect(db_path, busy_timeout_ms=busy_timeout_ms)
    applied: list[str] = []
    try:
        done = applied_versions(conn)
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = path.stem
            if version in done:
                continue
            log.info("applying migration %s", version)
            conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, datetime.now(tz=UTC).isoformat()),
            )
            conn.commit()
            applied.append(version)
    finally:
        conn.close()
    return applied


def migrate_live(db_path: Path, *, busy_timeout_ms: int = 5_000) -> None:
    """The live database is ephemeral state, so it is created, not migrated.

    It can safely live on tmpfs; losing it costs nothing but the current
    in-progress bar, which the next trade rebuilds.
    """
    conn = connect(db_path, busy_timeout_ms=busy_timeout_ms)
    try:
        conn.executescript(LIVE_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply database migrations")
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    db_path = args.db or settings.db_path
    applied = migrate(db_path, busy_timeout_ms=settings.sqlite_busy_timeout_ms)
    migrate_live(settings.live_db_path, busy_timeout_ms=settings.sqlite_busy_timeout_ms)
    if applied:
        log.info("applied %d migration(s): %s", len(applied), ", ".join(applied))
    else:
        log.info("database already up to date: %s", db_path)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
