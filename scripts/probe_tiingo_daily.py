"""Check what the Tiingo daily endpoint gives us before designing around it.

Phase 0 of the historical corpus. The intraday endpoint turned out to be
unreliable for history -- data we captured live is no longer served on
re-query (docs/spec-review.md A-6, and the SKHY case on 2026-07-30) -- so the
corpus is built on adjusted daily bars instead. Before committing to that, two
things have to come from the wire rather than from documentation:

1. How much history one call actually returns, and whether the adjusted fields
   (adjClose, splitFactor, divCash) are present. A corpus for event studies is
   worthless without split/dividend adjustment.
2. What one symbol costs in bytes, so the 50-symbol universe can be sized
   against the 1 GB/month ingress budget before the first bulk run.

*** This deliberately does not enumerate symbols. ***

Tiingo's free tier is documented as limiting unique symbols per month, and the
number is not retrievable from the API. A probe that swept 50 tickers to
"discover" the cap would spend the very allowance the corpus needs. So the
default is a symbol already in the watchlist -- already counted this month --
and the 50-symbol figures below are extrapolated from it. Finding the cap is a
separate decision, made deliberately, not a side effect of measuring.

    sudo -u usstocks env \\
      USSTOCKS_TIINGO_API_KEY="$(sudo grep '^USSTOCKS_TIINGO_API_KEY=' \\
        /etc/usstocks/usstocks.env | cut -d= -f2-)" \\
      /opt/usstocks/current/venv/bin/python \\
      /opt/usstocks/app/scripts/probe_tiingo_daily.py

Costs two REST calls per symbol (metadata + prices) from the same allowance the
collector uses. Run it outside market hours.
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx

DEFAULT_BASE = "https://api.tiingo.com"

# Adjusted fields are the reason for using this endpoint at all: an event study
# across a split reads as a 50% crash without them.
REQUIRED_FIELDS = ("date", "close", "adjClose", "adjVolume", "splitFactor", "divCash")

UNIVERSE_SIZE = 50          # AI / semiconductor names, per the Phase 0 decision
MONTHLY_BUDGET_BYTES = 1_000_000_000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "symbols",
        nargs="*",
        default=["AAPL"],
        help="Default AAPL: already in the watchlist, so it cannot add to a "
             "monthly unique-symbol count. Pass others only deliberately.",
    )
    ap.add_argument("--start", default="1990-01-01")
    ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args()

    token = os.environ.get("USSTOCKS_TIINGO_API_KEY", "").strip()
    if not token:
        print("USSTOCKS_TIINGO_API_KEY is not set", file=sys.stderr)
        return 2

    print(f"startDate       : {args.start}")
    print(f"universe target : {UNIVERSE_SIZE} symbols (AI / semiconductor)")
    print()

    for symbol in args.symbols:
        print(f"=== {symbol.upper()}")

        # Metadata first: it states the provider's own coverage bounds, which
        # is the cheapest way to learn whether a symbol is served at all.
        meta = httpx.get(
            f"{args.base}/tiingo/daily/{symbol.lower()}",
            params={"token": token},
            timeout=60.0,
        )
        if meta.status_code >= 400:
            print(f"    metadata HTTP {meta.status_code}: {meta.text[:200]}")
            print()
            continue
        info = meta.json()
        print(f"    name          : {info.get('name')}")
        print(f"    exchange      : {info.get('exchangeCode')}")
        print(f"    coverage      : {info.get('startDate')} .. {info.get('endDate')}")

        prices = httpx.get(
            f"{args.base}/tiingo/daily/{symbol.lower()}/prices",
            params={"startDate": args.start, "token": token},
            timeout=120.0,
        )
        print(f"    prices        : HTTP {prices.status_code} "
              f"({len(prices.content):,} bytes)")
        if prices.status_code >= 400:
            print(f"    {prices.text[:300]}")
            print()
            continue

        rows = prices.json()
        if not isinstance(rows, list) or not rows:
            print("    no rows returned")
            print()
            continue

        first, last = rows[0], rows[-1]
        print(f"    rows          : {len(rows):,}")
        print(f"    range         : {first.get('date', '?')[:10]} .. "
              f"{last.get('date', '?')[:10]}")

        missing = [field for field in REQUIRED_FIELDS if field not in last]
        if missing:
            print(f"    MISSING       : {', '.join(missing)}")
            print("      ^ 分割・配当調整ができないため、イベントスタディには使えない")
        else:
            print(f"    adjusted      : ok ({last['adjClose']=:.4f}, "
                  f"split={last['splitFactor']}, div={last['divCash']})")

        # Everything below is extrapolation from this one symbol. Stated as
        # such: the point of the probe is to make the bulk run predictable, not
        # to pretend one sample measured the universe.
        per_symbol = len(prices.content)
        total = per_symbol * UNIVERSE_SIZE
        print()
        print(f"    → {UNIVERSE_SIZE}銘柄の初回取得（この銘柄から外挿）")
        print(f"        calls     : {UNIVERSE_SIZE} （1銘柄1コールで全期間）")
        print(f"        ingress   : 約 {total / 1_000_000:.1f} MB "
              f"= 月間予算の {total / MONTHLY_BUDGET_BYTES:.1%}")
        print(f"        rows      : 約 {len(rows) * UNIVERSE_SIZE:,}")
        # 370 calls/day is what the poll-loop rebalancing leaves for history.
        print(f"        所要日数  : {UNIVERSE_SIZE / 370:.1f} 日 "
              f"（履歴用 370 calls/day のうち）")
        print()

    print("--- 未確認のまま残る点 ---")
    print("月間ユニークシンボル上限は API から読めず、ドキュメントも取得できなかった。")
    print("この探査は既存銘柄しか触っていないので、上限は消費していない。")
    print(f"{UNIVERSE_SIZE}銘柄を一度に投入する前に、少数ずつ増やして 4xx を観測するのが安全。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
