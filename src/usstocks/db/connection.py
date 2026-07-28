"""SQLite connection helpers.

WAL is required by the spec (8, 11.1). Two nuances the spec misses:

* Its "single writer" rule cannot hold literally, because the API must write
  the watchlist (spec 3.1). We keep the *time-series* single-writer invariant
  and let small config tables take a short write lock, guarded by
  busy_timeout. See docs/spec-review.md A-3.
* FastAPI runs synchronous endpoints in a worker threadpool, and a sqlite3
  connection may not be shared across threads. Handing each thread its own
  connection is both the correct fix and free under WAL, which supports many
  concurrent readers.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def connect(
    path: Path | str,
    *,
    busy_timeout_ms: int = 5_000,
    read_only: bool = False,
) -> sqlite3.Connection:
    path = Path(path)
    if read_only:
        conn = sqlite3.connect(
            f"file:{path}?mode=ro",
            uri=True,
            timeout=busy_timeout_ms / 1000,
            check_same_thread=False,
        )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            path, timeout=busy_timeout_ms / 1000, check_same_thread=False
        )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute("PRAGMA foreign_keys = ON")
    if not read_only:
        conn.execute("PRAGMA journal_mode = WAL")
        # NORMAL is the right trade-off under WAL: durable across process
        # crashes, only at risk on OS/power loss, which the daily S3 backup
        # and REST re-fetch already cover (spec 4.4, docs/spec-review.md B-9).
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA wal_autocheckpoint = 1000")
    return conn


class ConnectionPool:
    """One connection per thread, closable from any thread.

    ``check_same_thread=False`` alone would be unsafe: it permits genuine
    concurrent use of one connection. Pairing it with thread-local instances
    means no connection is ever touched by two threads, while still allowing
    shutdown to close them all.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        busy_timeout_ms: int = 5_000,
        read_only: bool = False,
    ) -> None:
        self.path = Path(path)
        self._busy_timeout_ms = busy_timeout_ms
        self._read_only = read_only
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    def get(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(
                self.path,
                busy_timeout_ms=self._busy_timeout_ms,
                read_only=self._read_only,
            )
            self._local.conn = conn
            with self._lock:
                self._all.append(conn)
        return conn

    def close(self) -> None:
        with self._lock:
            connections, self._all = self._all, []
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover - best effort on shutdown
                pass
        self._local = threading.local()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """IMMEDIATE so writer contention fails fast into busy_timeout instead of
    deadlocking on upgrade from a deferred read transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
