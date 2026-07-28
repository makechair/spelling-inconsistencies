#!/usr/bin/env python3
"""Add symbols to the watchlist without going through the API.

Useful for first-run setup and for development; the collector picks them up on
its next symbol-refresh poll.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from usstocks.config import get_settings  # noqa: E402
from usstocks.db.migrate import migrate  # noqa: E402
from usstocks.db.repository import Repository  # noqa: E402
from usstocks.models import SymbolInfo  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: seed.py TICKER [TICKER ...]", file=sys.stderr)
        return 2

    settings = get_settings()
    migrate(settings.db_path, busy_timeout_ms=settings.sqlite_busy_timeout_ms)
    with Repository(
        settings.db_path,
        busy_timeout_ms=settings.sqlite_busy_timeout_ms,
        source_priority=settings.source_priority,
    ) as repo:
        for raw in argv:
            symbol = raw.strip().upper()
            repo.upsert_symbol(SymbolInfo(symbol=symbol, is_watched=True))
            print(f"watching {symbol}")
        print(f"watchlist: {', '.join(repo.watched_symbols())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
