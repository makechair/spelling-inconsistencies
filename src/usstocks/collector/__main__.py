"""Collector entry point."""

from __future__ import annotations

import asyncio
import logging
import signal

from ..adapters.registry import build_adapter
from ..config import get_settings
from ..db.live_store import LiveStore
from ..db.migrate import migrate, migrate_live
from ..db.repository import Repository
from ..logging_setup import configure_logging
from .service import CollectorService

log = logging.getLogger(__name__)


async def _run() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)

    migrate(settings.db_path, busy_timeout_ms=settings.sqlite_busy_timeout_ms)
    migrate_live(settings.live_db_path, busy_timeout_ms=settings.sqlite_busy_timeout_ms)

    adapter = build_adapter(settings)
    repository = Repository(
        settings.db_path,
        busy_timeout_ms=settings.sqlite_busy_timeout_ms,
        source_priority=settings.source_priority,
    )
    live_store = LiveStore(
        settings.live_db_path, busy_timeout_ms=settings.sqlite_busy_timeout_ms
    )
    service = CollectorService(settings, adapter, repository, live_store)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with_handler = getattr(loop, "add_signal_handler", None)
        if with_handler is not None:
            try:
                loop.add_signal_handler(sig, service.stop)
            except NotImplementedError:  # pragma: no cover - non-POSIX
                pass

    try:
        await service.run()
    finally:
        repository.close()
        live_store.close()
    log.info("collector stopped")
    return 0


def main() -> int:
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
