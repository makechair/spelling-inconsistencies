"""Server-sent events for live prices (spec 3.3).

Design points that the spec's "SSE, at most 1 Hz" leaves open
(docs/spec-review.md B-6):

* Only *changed* symbols are sent. If nothing traded, nothing is emitted, so
  the browser has no way to draw movement that did not happen (spec 3.3:
  never fabricate a change).
* A heartbeat event every ``sse_heartbeat_seconds`` keeps the connection alive
  through Cloudflare's idle timeout without touching prices.
* ``X-Accel-Buffering: no`` and ``Cache-Control: no-cache`` stop intermediaries
  from buffering the stream into multi-second batches, which would silently
  break the 1-second requirement.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from ...config import Settings
from ...db.live_store import LiveStore
from ..deps import get_live_store, get_settings_dep
from ..schemas import LiveOut

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/live", tags=["live"])


def _fingerprint(payload: LiveOut) -> tuple:
    """What counts as a change worth sending."""
    bar = payload.bar
    return (
        payload.last_price,
        payload.last_trade_at,
        payload.session,
        bar.time if bar else None,
        bar.close if bar else None,
        bar.high if bar else None,
        bar.low if bar else None,
        bar.volume if bar else None,
    )


def _sse(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@router.get("")
async def live_stream(
    request: Request,
    symbols: str = Query("", description="Comma-separated tickers; empty = all watched"),
    live_store: LiveStore = Depends(get_live_store),
    settings: Settings = Depends(get_settings_dep),
) -> StreamingResponse:
    wanted = [item.strip().upper() for item in symbols.split(",") if item.strip()]

    async def generate():
        seen: dict[str, tuple] = {}
        last_heartbeat = asyncio.get_running_loop().time()
        last_status: str | None = None

        # Open with a full snapshot so a fresh tab is not blank until the next
        # trade prints.
        try:
            now = datetime.now(tz=UTC)
            initial = {
                symbol: LiveOut.from_snapshot(snapshot, now)
                for symbol, snapshot in live_store.read(wanted or None).items()
            }
            for symbol, payload in initial.items():
                seen[symbol] = _fingerprint(payload)
            yield _sse(
                "snapshot",
                {
                    "server_time": now.isoformat(),
                    "symbols": [payload.model_dump() for payload in initial.values()],
                },
            )

            deadline = (
                asyncio.get_running_loop().time() + settings.sse_max_stream_seconds
            )
            while True:
                if await request.is_disconnected():
                    return
                if asyncio.get_running_loop().time() >= deadline:
                    # Tell the client this was routine, then let EventSource
                    # reconnect. Prevents an undetected dead client from
                    # holding resources forever.
                    yield _sse("cycle", {"reason": "max_stream_lifetime"})
                    return

                await asyncio.sleep(settings.sse_interval_seconds)
                now = datetime.now(tz=UTC)

                changed = []
                for symbol, snapshot in live_store.read(wanted or None).items():
                    payload = LiveOut.from_snapshot(snapshot, now)
                    fingerprint = _fingerprint(payload)
                    if seen.get(symbol) != fingerprint:
                        seen[symbol] = fingerprint
                        changed.append(payload)

                if changed:
                    yield _sse(
                        "update",
                        {
                            "server_time": now.isoformat(),
                            "symbols": [payload.model_dump() for payload in changed],
                        },
                    )

                status, status_at = live_store.read_status()
                if status is not None:
                    digest = json.dumps(
                        {
                            "connected": status.get("connected"),
                            "last_error": status.get("last_error"),
                            "reconnects": status.get("reconnect_count"),
                        },
                        sort_keys=True,
                    )
                    if digest != last_status:
                        last_status = digest
                        yield _sse(
                            "status",
                            {
                                **status,
                                "status_updated_at": (
                                    status_at.isoformat() if status_at else None
                                ),
                            },
                        )

                clock = asyncio.get_running_loop().time()
                if clock - last_heartbeat >= settings.sse_heartbeat_seconds:
                    last_heartbeat = clock
                    # Carries no prices on purpose: it proves the pipe is open
                    # without implying the market moved.
                    yield _sse("heartbeat", {"server_time": now.isoformat()})
        except asyncio.CancelledError:  # pragma: no cover - client hung up
            raise
        except Exception:  # noqa: BLE001
            log.exception("SSE stream failed")
            yield _sse("error", {"message": "stream terminated"})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/snapshot")
def live_snapshot(
    symbols: str = Query(""),
    live_store: LiveStore = Depends(get_live_store),
) -> dict:
    """Non-streaming equivalent, for polling clients and diagnostics."""
    wanted = [item.strip().upper() for item in symbols.split(",") if item.strip()]
    now = datetime.now(tz=UTC)
    payloads = [
        LiveOut.from_snapshot(snapshot, now)
        for snapshot in live_store.read(wanted or None).values()
    ]
    return {"server_time": now.isoformat(), "symbols": [p.model_dump() for p in payloads]}
