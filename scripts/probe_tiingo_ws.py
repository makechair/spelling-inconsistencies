"""Print what the Tiingo IEX websocket actually sends.

Written because the collector reached a state that produces no diagnosis on its
own: the subscription is accepted -- no error frame, no disconnect -- and no
trade ever arrives. Silence is the one failure the logs cannot describe, since
the collector only records what it decides to act on: message type "A" (data)
and "E" (error). A subscription acknowledgement, a heartbeat, or a payload in
some other shape all pass through unlogged.

This connects, subscribes, and prints every frame verbatim, so the answer comes
from the wire rather than from a guess about which thresholdLevel a plan
permits.

    sudo -u usstocks env \\
      USSTOCKS_TIINGO_API_KEY="$(sudo grep '^USSTOCKS_TIINGO_API_KEY=' \\
        /etc/usstocks/usstocks.env | cut -d= -f2-)" \\
      /opt/usstocks/current/venv/bin/python \\
      /opt/usstocks/app/scripts/probe_tiingo_ws.py --seconds 30

Costs no REST budget: a websocket connection is not a counted request.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime

import websockets

DEFAULT_URL = "wss://api.tiingo.com/iex"


def stamp() -> str:
    return datetime.now(tz=UTC).strftime("%H:%M:%S")


async def probe(
    url: str, key: str, symbols: list[str], threshold: int | None, seconds: float
) -> int:
    event_data: dict[str, object] = {"tickers": symbols}
    if threshold is not None:
        event_data["thresholdLevel"] = threshold
    subscribe = {"eventName": "subscribe", "authorization": key, "eventData": event_data}

    label = "omitted" if threshold is None else str(threshold)
    print(f"{stamp()} connecting to {url} (thresholdLevel: {label}, tickers: {symbols})")

    frames = 0
    trades = 0
    try:
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as socket:
            await socket.send(json.dumps(subscribe))
            print(f"{stamp()} subscribe sent; listening for {seconds:.0f}s")
            deadline = asyncio.get_running_loop().time() + seconds
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=remaining)
                except TimeoutError:
                    break
                frames += 1
                text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
                # Whole frame, not a summary: the point is to see fields this
                # code does not already know to look for.
                print(f"{stamp()} [{frames:3d}] {text[:400]}")
                try:
                    message = json.loads(text)
                except json.JSONDecodeError:
                    continue
                data = message.get("data")
                is_trade = (
                    message.get("messageType") == "A"
                    and isinstance(data, list)
                    and data[:1] == ["T"]
                )
                if is_trade:
                    trades += 1
    except Exception as exc:  # noqa: BLE001 - a probe reports rather than raises
        print(f"{stamp()} connection ended: {type(exc).__name__}: {exc}")

    print(f"\n{stamp()} {frames} frame(s), {trades} trade message(s)")
    if frames == 0:
        print("  Nothing at all: the socket accepted the subscription and stayed silent.")
    elif trades == 0:
        print("  Frames arrived but none were trades. Check messageType and data[0]")
        print("  above -- the collector builds bars only from \"T\" and discards the rest.")
    return 0 if trades else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="aapl", help="comma separated, lowercase")
    parser.add_argument(
        "--threshold",
        default=None,
        help="thresholdLevel to send; omit the flag to leave it out entirely",
    )
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--url", default=os.environ.get("USSTOCKS_TIINGO_WS_URL", DEFAULT_URL))
    args = parser.parse_args()

    key = os.environ.get("USSTOCKS_TIINGO_API_KEY", "").strip()
    if not key:
        print("USSTOCKS_TIINGO_API_KEY is not set", file=sys.stderr)
        return 2

    threshold = None if args.threshold is None else int(args.threshold)
    symbols = [s.strip().lower() for s in args.symbols.split(",") if s.strip()]
    return asyncio.run(probe(args.url, key, symbols, threshold, args.seconds))


if __name__ == "__main__":
    raise SystemExit(main())
