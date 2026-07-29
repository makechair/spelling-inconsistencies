"""Print what the Alpaca IEX websocket actually sends.

The counterpart to probe_tiingo_ws.py, written for the same reason: to find out
from the wire rather than from documentation whether a plan streams.

That question is now the deciding one. Tiingo's free tier accepts an IEX
subscription -- 200, a subscriptionId, a heartbeat -- and then sends no trade,
while every thresholdLevel it accepts as a parameter is refused as not valid for
the tier. The specification's core premise, a websocket that streams
continuously (spec 5.1), does not hold there any more, which leaves REST
backfill as the only live path and caps it at 50 calls an hour.

Alpaca's free tier is documented as including real-time IEX over websocket, and
this repository already carries the adapter. Given Tiingo changed its terms,
that is worth measuring rather than trusting: run this before switching the
primary source.

    sudo -u usstocks env \\
      USSTOCKS_ALPACA_API_KEY=... USSTOCKS_ALPACA_API_SECRET=... \\
      /opt/usstocks/current/venv/bin/python \\
      /opt/usstocks/app/scripts/probe_alpaca_ws.py --seconds 30

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

DEFAULT_URL = "wss://stream.data.alpaca.markets/v2/iex"


def stamp() -> str:
    return datetime.now(tz=UTC).strftime("%H:%M:%S")


async def probe(url: str, key: str, secret: str, symbols: list[str], seconds: float) -> int:
    print(f"{stamp()} connecting to {url} (tickers: {symbols})")

    frames = 0
    trades = 0
    try:
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as socket:
            await socket.send(json.dumps({"action": "auth", "key": key, "secret": secret}))
            await socket.send(json.dumps({"action": "subscribe", "trades": symbols}))
            print(f"{stamp()} auth and subscribe sent; listening for {seconds:.0f}s")

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
                print(f"{stamp()} [{frames:3d}] {text[:400]}")
                try:
                    messages = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(messages, list):
                    # "t" is a trade. The adapter builds bars from these alone.
                    trades += sum(1 for m in messages if m.get("T") == "t")
    except Exception as exc:  # noqa: BLE001 - a probe reports rather than raises
        print(f"{stamp()} connection ended: {type(exc).__name__}: {exc}")

    print(f"\n{stamp()} {frames} frame(s), {trades} trade message(s)")
    if trades:
        print("  Streams. USSTOCKS_PRIMARY_SOURCE=alpaca is viable on this plan.")
    elif frames:
        print("  Frames arrived but no trades. Check for an error or subscription")
        print("  message above, and whether the market is open.")
    else:
        print("  Nothing at all: check the credentials and the URL.")
    return 0 if trades else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="AAPL", help="comma separated, uppercase")
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--url", default=os.environ.get("USSTOCKS_ALPACA_WS_URL", DEFAULT_URL))
    args = parser.parse_args()

    key = os.environ.get("USSTOCKS_ALPACA_API_KEY", "").strip()
    secret = os.environ.get("USSTOCKS_ALPACA_API_SECRET", "").strip()
    if not key or not secret:
        print(
            "USSTOCKS_ALPACA_API_KEY and USSTOCKS_ALPACA_API_SECRET must be set",
            file=sys.stderr,
        )
        return 2

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    return asyncio.run(probe(args.url, key, secret, symbols, args.seconds))


if __name__ == "__main__":
    raise SystemExit(main())
