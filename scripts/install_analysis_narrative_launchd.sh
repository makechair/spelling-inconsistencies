#!/usr/bin/env bash
set -Eeuo pipefail

EXCHANGE_URI="${1:?usage: $0 s3://bucket/analysis-exchange}"
[[ "${EXCHANGE_URI}" == s3://*/analysis-exchange ]] || {
  echo "expected s3://bucket/analysis-exchange" >&2
  exit 1
}

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="${REPO_DIR}/deploy/launchd/com.makechair.usstocks-analysis-narrative.plist.template"
TARGET="${HOME}/Library/LaunchAgents/com.makechair.usstocks-analysis-narrative.plist"
LOG_DIR="${HOME}/Library/Logs/usstocks"
PYTHON="${REPO_DIR}/.venv/bin/python"
SCRIPT="${REPO_DIR}/scripts/run_analysis_narrative_bridge.py"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

[[ -x "${PYTHON}" ]] || { echo "missing ${PYTHON}" >&2; exit 1; }
command -v aws >/dev/null || { echo "aws CLI is required" >&2; exit 1; }
command -v ollama >/dev/null || { echo "Ollama is required" >&2; exit 1; }
mkdir -p "$(dirname "${TARGET}")" "${LOG_DIR}"

escape() { printf '%s' "$1" | sed 's/[&|]/\\&/g'; }
sed \
  -e "s|__PYTHON__|$(escape "${PYTHON}")|g" \
  -e "s|__SCRIPT__|$(escape "${SCRIPT}")|g" \
  -e "s|__EXCHANGE_URI__|$(escape "${EXCHANGE_URI}")|g" \
  -e "s|__REPO__|$(escape "${REPO_DIR}")|g" \
  -e "s|__LOG_DIR__|$(escape "${LOG_DIR}")|g" \
  "${TEMPLATE}" > "${TARGET}"
plutil -lint "${TARGET}"
launchctl bootout "gui/${UID}" "${TARGET}" 2>/dev/null || true
launchctl bootstrap "gui/${UID}" "${TARGET}"
echo "installed ${TARGET} (daily 14:45 JST)"
