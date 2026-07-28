#!/usr/bin/env bash
#
# Local development stack: collector + API against the mock feed, with auth
# off. Everything binds to loopback; config.py refuses "auth disabled" on any
# other address, so this cannot accidentally become a public deployment.
set -Eeuo pipefail
cd "$(dirname "$0")/.."

export USSTOCKS_PRIMARY_SOURCE="${USSTOCKS_PRIMARY_SOURCE:-mock}"
export USSTOCKS_AUTH_MODE="${USSTOCKS_AUTH_MODE:-disabled}"
export USSTOCKS_API_HOST=127.0.0.1
export USSTOCKS_DB_PATH="${USSTOCKS_DB_PATH:-./data/market.db}"
export USSTOCKS_LIVE_DB_PATH="${USSTOCKS_LIVE_DB_PATH:-./data/live.db}"
export USSTOCKS_SYMBOL_REFRESH_SECONDS=2

PY="${PY:-.venv/bin/python}"
[[ -x "$PY" ]] || PY=python3

"$PY" -m usstocks.db.migrate
"$PY" scripts/seed.py AAPL MSFT NVDA

pids=()
cleanup() {
  trap - EXIT INT TERM
  for pid in "${pids[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

"$PY" -m usstocks.collector & pids+=($!)
"$PY" -m usstocks.api & pids+=($!)

echo
echo "  http://127.0.0.1:8000  (mock data -- every price here is fabricated)"
echo
wait -n
