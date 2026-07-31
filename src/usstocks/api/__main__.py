"""API entry point."""

from __future__ import annotations

import uvicorn

from ..config import get_settings


def main() -> int:
    settings = get_settings()
    settings.validate_for_serving()
    uvicorn.run(
        "usstocks.api.app:get_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
        # One worker: SSE clients and SQLite readers gain nothing from more,
        # and the instance has 2 GB of RAM.
        workers=1,
        timeout_keep_alive=75,
        timeout_graceful_shutdown=settings.api_graceful_shutdown_seconds,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
