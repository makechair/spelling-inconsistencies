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
        # and the instance has 1 GB of RAM (spec 12, "1GB memory pressure").
        workers=1,
        timeout_keep_alive=75,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
