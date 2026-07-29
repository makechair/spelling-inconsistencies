#!/usr/bin/env bash
#
# Pull-based application deployment.
#
# Why pull and not push: a GitHub Actions runner has no fixed egress address,
# so an SSH-based deploy would mean opening port 22 to 0.0.0.0/0. That
# contradicts the closed-ingress design the specification asks for (3.5, 4.5)
# and that infra/terraform/lightsail.tf enforces. Polling from the instance
# keeps every connection outbound, exactly like the Cloudflare tunnel.
#
# Runs on a timer. Checks whether the tracked branch moved; if it did, fetches,
# rebuilds and restarts. If it did not, exits without touching the services --
# a restart drops the WebSocket and costs REST budget to backfill.
set -Eeuo pipefail

PROJECT="${PROJECT:-usstocks}"
REPO_DIR="${REPO_DIR:-/opt/${PROJECT}/app}"
BRANCH="${DEPLOY_BRANCH:-main}"
COMPOSE_FILE="${COMPOSE_FILE:-deploy/docker-compose.yml}"
ENV_FILE="${ENV_FILE:-/etc/${PROJECT}/${PROJECT}.env}"
LOCK_FILE="/var/lock/${PROJECT}-deploy.lock"

log() { printf '%s deploy-agent: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# One deploy at a time. Overlapping timer firings during a slow build would
# otherwise run docker compose against a half-updated tree.
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  log "another deploy is in progress; exiting"
  exit 0
fi

[[ -f "${ENV_FILE}" ]] || { log "missing env file ${ENV_FILE}"; exit 1; }

# Recovery path only. This cannot bootstrap a bare instance: the systemd unit
# runs this script from inside ${REPO_DIR}, so if the checkout is missing then
# so is this file. The first clone is a documented manual step (see
# docs/aws-deployment.md). What this does cover is a checkout deleted while the
# units stay installed.
if [[ ! -d "${REPO_DIR}/.git" ]]; then
  REPO_URL="$(cat "/etc/${PROJECT}/repo-url")"
  log "no checkout at ${REPO_DIR}; cloning ${REPO_URL}"
  git clone --branch "${BRANCH}" "${REPO_URL}" "${REPO_DIR}"
fi

cd "${REPO_DIR}"

# Before any compose command: compose interpolates ${CLOUDFLARE_TUNNEL_TOKEN}
# from a .env in the project directory, so even a read-only `ps` needs it.
# Secrets live in the env file placed out of band, never in the repository
# (spec 4.5).
ln -sfn "${ENV_FILE}" "${REPO_DIR}/.env"

git remote set-branches origin "${BRANCH}" >/dev/null 2>&1 || true
git fetch --quiet origin "${BRANCH}"

local_sha="$(git rev-parse HEAD)"
remote_sha="$(git rev-parse "origin/${BRANCH}")"

# How many of the three services are up. Used to tell "nothing to do" apart
# from "nothing to do, and also nothing is running".
running_services() {
  docker compose -f "${COMPOSE_FILE}" ps --services --status running 2>/dev/null | grep -c . || true
}

if [[ "${local_sha}" == "${remote_sha}" ]]; then
  # Being at the right revision is not the same as running it. After the first
  # manual clone the branch is already current, so a pure revision check would
  # report success on every timer firing while the services had never started
  # once.
  if [[ "$(running_services)" -ge 3 ]]; then
    log "already at ${local_sha:0:8} and services are up; nothing to do"
    exit 0
  fi
  log "already at ${local_sha:0:8} but services are not up; starting them"
else
  log "updating ${local_sha:0:8} -> ${remote_sha:0:8}"

  # Refuse to deploy a tree that is not exactly what the branch says. A local
  # edit made while debugging would otherwise be silently discarded, or worse,
  # silently kept.
  if ! git diff --quiet || ! git diff --cached --quiet; then
    log "working tree has local modifications; refusing to deploy"
    exit 1
  fi

  git checkout --quiet "${BRANCH}"
  git reset --hard --quiet "origin/${BRANCH}"
fi

log "building and restarting services"
docker compose -f "${COMPOSE_FILE}" up -d --build --remove-orphans

# Prune only dangling images. A blanket prune would delete the previous image
# and remove the fastest rollback path.
docker image prune -f --filter "dangling=true" >/dev/null 2>&1 || true

# Confirm the API answers before calling the deploy good. The collector is
# checked separately by its own healthcheck; this catches the common failure
# where a bad build leaves the API container restarting.
for attempt in $(seq 1 30); do
  if docker compose -f "${COMPOSE_FILE}" exec -T api \
      python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/livez')" \
      >/dev/null 2>&1; then
    log "deploy complete at ${remote_sha:0:8}"
    exit 0
  fi
  sleep 2
done

log "ERROR: api did not become healthy after deploy of ${remote_sha:0:8}"
docker compose -f "${COMPOSE_FILE}" ps
exit 1
