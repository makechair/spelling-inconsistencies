#!/usr/bin/env bash
#
# Install the direct-host runtime from an already cloned repository.
#
# This script deliberately refuses to stop Compose or copy its database. The
# one-writer SQLite handoff is an operator-visible cutover documented in
# docs/systemd-deployment.md.
set -Eeuo pipefail

PROJECT="${PROJECT:-usstocks}"
REPO_DIR="${1:-/opt/${PROJECT}/app}"
ENV_FILE="/etc/${PROJECT}/${PROJECT}.env"
UNIT_DIR="/etc/systemd/system"
HAD_CURRENT=false
CURRENT_BEFORE=""

log() { printf 'systemd-install: %s\n' "$*"; }
fail() { log "ERROR: $*" >&2; exit 1; }

[[ "${EUID}" -eq 0 ]] || fail "run as root (sudo $0 ${REPO_DIR})"
[[ -d "${REPO_DIR}/.git" ]] || fail "repository not found at ${REPO_DIR}"
[[ -f "${ENV_FILE}" ]] || fail "create ${ENV_FILE} before installing"
id -u "${PROJECT}" >/dev/null 2>&1 || fail "service account ${PROJECT} does not exist"
if [[ -L "/opt/${PROJECT}/current" ]]; then
  HAD_CURRENT=true
  CURRENT_BEFORE="$(readlink -f "/opt/${PROJECT}/current")"
fi

for command in python3 git curl flock runuser systemctl; do
  command -v "${command}" >/dev/null 2>&1 || fail "required command not found: ${command}"
done
python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' ||
  fail "Python 3.11 or newer is required"
python3 -c 'import ensurepip, venv' >/dev/null 2>&1 ||
  fail "python3-venv is missing (apt-get install python3-venv)"
if [[ -x /usr/bin/cloudflared ]] &&
  ! grep -Eq '^CLOUDFLARE_TUNNEL_TOKEN=[^[:space:]"]+' "${ENV_FILE}"; then
  fail "cloudflared is installed but CLOUDFLARE_TUNNEL_TOKEN is empty in ${ENV_FILE}"
fi

if command -v docker >/dev/null 2>&1; then
  if docker ps --format '{{.Names}}' 2>/dev/null |
    grep -Eq '^usstocks-(collector|api)$'; then
    fail "Compose app containers are running; perform the documented database cutover first"
  fi

  compose_volume="$(
    docker volume inspect usstocks_market-data --format '{{.Mountpoint}}' 2>/dev/null ||
      true
  )"
  if [[ -n "${compose_volume}" &&
        -f "${compose_volume}/market.db" &&
        ! -f "/var/lib/${PROJECT}/market.db" ]]; then
    fail "Compose market.db exists but has not been migrated; see docs/systemd-deployment.md"
  fi
fi

install -d -o root -g root -m 0755 "/opt/${PROJECT}/releases"
install -d -o "${PROJECT}" -g "${PROJECT}" -m 0750 \
  "/var/lib/${PROJECT}" \
  "/var/lib/${PROJECT}/corpus" \
  "/var/backups/${PROJECT}" \
  "/var/cache/${PROJECT}/pip"
chown root:"${PROJECT}" "${ENV_FILE}"
chmod 0640 "${ENV_FILE}"

for unit in \
  usstocks-collector.service \
  usstocks-api.service \
  usstocks-cloudflared.service \
  usstocks-backup.service \
  usstocks-backup.timer \
  usstocks-catalog.service \
  usstocks-catalog.timer \
  usstocks-corpus.service \
  usstocks-corpus.timer \
  usstocks-news-corpus.service \
  usstocks-news-corpus.timer \
  usstocks-event-study.service \
  usstocks-fundamentals.service \
  usstocks-edinet.service \
  usstocks-event-study.timer \
  usstocks-fundamentals.timer \
  usstocks-edinet.timer \
  usstocks-analysis-narrative-import.service \
  usstocks-analysis-narrative-import.timer; do
  install -m 0644 "${REPO_DIR}/deploy/systemd/${unit}" "${UNIT_DIR}/${unit}"
done
install -m 0644 \
  "${REPO_DIR}/deploy/agent/usstocks-deploy.service" \
  "${UNIT_DIR}/usstocks-deploy.service"
install -m 0644 \
  "${REPO_DIR}/deploy/agent/usstocks-deploy.timer" \
  "${UNIT_DIR}/usstocks-deploy.timer"

systemctl daemon-reload

log "building and activating the initial release"
# An explicit installer run is also the recovery path after correcting an env
# or host-package problem on the same revision.
rm -f "/opt/${PROJECT}/failed-systemd-sha"
systemctl start usstocks-deploy.service
CURRENT_AFTER="$(readlink -f "/opt/${PROJECT}/current")"
if [[ "${HAD_CURRENT}" == true && "${CURRENT_BEFORE}" == "${CURRENT_AFTER}" ]]; then
  systemctl restart usstocks-collector.service
  systemctl restart usstocks-api.service
fi
systemctl enable \
  usstocks-collector.service \
  usstocks-api.service \
  usstocks-backup.timer \
  usstocks-catalog.timer \
  usstocks-corpus.timer \
  usstocks-news-corpus.timer \
  usstocks-event-study.timer \
  usstocks-fundamentals.timer \
  usstocks-edinet.timer \
  usstocks-analysis-narrative-import.timer \
  usstocks-deploy.timer
systemctl start \
  usstocks-backup.timer \
  usstocks-catalog.timer \
  usstocks-corpus.timer \
  usstocks-news-corpus.timer \
  usstocks-event-study.timer \
  usstocks-fundamentals.timer \
  usstocks-edinet.timer \
  usstocks-analysis-narrative-import.timer \
  usstocks-deploy.timer

# Populate the catalog now rather than waiting for Sunday. Search falls back to
# the provider until this lands, so a failure here costs REST budget, not
# function.
if ! systemctl start usstocks-catalog.service; then
  log "catalog import failed; symbol search will use the provider until it succeeds"
fi

if [[ -x /usr/bin/cloudflared ]]; then
  systemctl enable usstocks-cloudflared.service
  systemctl restart usstocks-cloudflared.service
  log "cloudflared enabled"
else
  log "cloudflared is not installed; app is healthy on loopback but not externally reachable"
  log "install cloudflared, then run: systemctl enable --now usstocks-cloudflared"
fi

systemctl --no-pager --full status usstocks-collector.service usstocks-api.service
log "installation complete"
