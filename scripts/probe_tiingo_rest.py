"""Print what the Tiingo IEX REST endpoint actually returns for a day.

Written because "after-hours data is missing" has three possible causes that
look identical from the chart, and only the raw response tells them apart:

1. the provider never sent those minutes,
2. it sent them as volume-0 filler and the adapter dropped them on purpose
   (the resampler emits a bar for every minute whether or not anything traded;
   `forceFill=false` does not suppress it -- see adapters/tiingo.py), or
3. the collector never asked, because the gap or the calendar check refused.

This asks with exactly the parameters the adapter uses, then reports the rows
by session and by whether they carry volume. Cause 3 is ruled out separately by
the collector's own logs; causes 1 and 2 are visible here.

    sudo -u usstocks env \\
      USSTOCKS_TIINGO_API_KEY="$(sudo grep '^USSTOCKS_TIINGO_API_KEY=' \\
        /etc/usstocks/usstocks.env | cut -d= -f2-)" \\
      /opt/usstocks/current/venv/bin/python \\
      /opt/usstocks/app/scripts/probe_tiingo_rest.py AAPL SKHY

Costs one REST call per symbol from the same allowance the collector uses, so
pass only the symbols in question.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import UTC, date, datetime, timedelta

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from usstocks.calendar_us import EASTERN, classify  # noqa: E402

DEFAULT_BASE = "https://api.tiingo.com"


def parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="+")
    ap.add_argument(
        "--date",
        help="ET trading date, YYYY-MM-DD. Default: the most recent weekday.",
    )
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument(
        "--no-after-hours",
        action="store_true",
        help="Ask without afterHours=true, to show what the flag is worth.",
    )
    args = ap.parse_args()

    token = os.environ.get("USSTOCKS_TIINGO_API_KEY", "").strip()
    if not token:
        print("USSTOCKS_TIINGO_API_KEY is not set", file=sys.stderr)
        return 2

    if args.date:
        day = date.fromisoformat(args.date)
    else:
        day = datetime.now(tz=EASTERN).date()
        while day.weekday() >= 5:
            day -= timedelta(days=1)

    print(f"ET trading date : {day}")
    print(f"afterHours      : {'false' if args.no_after_hours else 'true'}")
    print()

    for symbol in args.symbols:
        params = {
            "startDate": day.isoformat(),
            "endDate": (day + timedelta(days=1)).isoformat(),
            "resampleFreq": "1min",
            "columns": "open,high,low,close,volume",
            "forceFill": "false",
            "token": token,
        }
        if not args.no_after_hours:
            params["afterHours"] = "true"

        url = f"{args.base}/iex/{symbol.lower()}/prices"
        response = httpx.get(url, params=params, timeout=60.0)
        print(f"=== {symbol.upper()}  HTTP {response.status_code} "
              f"({len(response.content):,} bytes)")
        if response.status_code >= 400:
            print(f"    {response.text[:300]}")
            print()
            continue

        payload = response.json()
        if not isinstance(payload, list):
            print(f"    unexpected payload: {type(payload).__name__}")
            print()
            continue

        rows = []
        for item in payload:
            timestamp = parse_ts(item.get("date"))
            if timestamp is None:
                continue
            # Keep only the requested ET day; the day-granular query overhangs.
            if timestamp.astimezone(EASTERN).date() != day:
                continue
            volume = item.get("volume") or 0
            rows.append((timestamp, int(volume), classify(timestamp)))
        rows.sort()

        if not rows:
            print("    no rows for this date")
            print()
            continue

        # The distinction that matters: a minute the provider reported with a
        # trade, versus a minute it filled in from the previous close.
        traded = Counter()
        filler = Counter()
        for _, volume, session in rows:
            (traded if volume else filler)[session.value] += 1

        print(f"    rows in range : {len(rows):,}")
        print(f"    {'session':<9} {'volume>0':>9} {'volume=0':>9}")
        for session in ("pre", "regular", "post", "closed"):
            if traded[session] or filler[session]:
                print(f"    {session:<9} {traded[session]:>9,} {filler[session]:>9,}")

        with_volume = [row for row in rows if row[1]]
        print()
        print(f"    最終行（全体）      : {fmt(rows[-1])}")
        if with_volume:
            print(f"    最終行（出来高あり）: {fmt(with_volume[-1])}")
            print("      ^ 保存されるのはこちらまで。以降は出来高0なので破棄される。")
        else:
            print("    出来高のある行が1本もない")
        print()

    return 0


def fmt(row: tuple[datetime, int, object]) -> str:
    timestamp, volume, session = row
    et = timestamp.astimezone(EASTERN)
    jst = timestamp.astimezone(UTC) + timedelta(hours=9)
    return (
        f"{et:%Y-%m-%d %H:%M} ET / {jst:%H:%M} JST  "
        f"volume={volume:<10,} session={session.value}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
