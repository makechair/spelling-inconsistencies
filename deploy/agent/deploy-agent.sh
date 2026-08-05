#!/usr/bin/env bash
#
# Pull-based application deployment.
#
# DEPLOY_RUNTIME=systemd (recommended on the 2 GB Lightsail plan):
#   build an immutable venv release, atomically switch /opt/usstocks/current,
#   restart only collector/API, health-check, and roll back on failure.
#
# DEPLOY_RUNTIME=compose (compatibility path):
#   retain the original local Docker image build workflow.
#
# The instance initiates every connection. This keeps SSH closed to arbitrary
# GitHub-hosted runner addresses and consumes no GitHub Actions build minutes.
set -Eeuo pipefail

PROJECT="${PROJECT:-usstocks}"
REPO_DIR="${REPO_DIR:-/opt/${PROJECT}/app}"
BRANCH="${DEPLOY_BRANCH:-main}"
RUNTIME="${DEPLOY_RUNTIME:-compose}"
COMPOSE_FILE="${COMPOSE_FILE:-deploy/docker-compose.yml}"
ENV_FILE="${ENV_FILE:-/etc/${PROJECT}/${PROJECT}.env}"
RELEASES_DIR="${RELEASES_DIR:-/opt/${PROJECT}/releases}"
CURRENT_LINK="${CURRENT_LINK:-/opt/${PROJECT}/current}"
PIP_CACHE_DIR="${PIP_CACHE_DIR:-/var/cache/${PROJECT}/pip}"
KEEP_RELEASES="${DEPLOY_KEEP_RELEASES:-3}"
if [[ -d "/run/${PROJECT}-deploy" ]]; then
  DEFAULT_LOCK_FILE="/run/${PROJECT}-deploy/deploy.lock"
else
  # Compatibility with the previous non-root Compose unit.
  DEFAULT_LOCK_FILE="/var/lock/${PROJECT}-deploy.lock"
fi
LOCK_FILE="${LOCK_FILE:-${DEFAULT_LOCK_FILE}}"
COMPOSE_MARKER="${REPO_DIR}/.deployed-compose-sha"
FAILED_SYSTEMD_MARKER="/opt/${PROJECT}/failed-systemd-sha"
BUILD_DIR=""
TEMP_LINK=""

log() { printf '%s deploy-agent: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

cleanup_temp() {
  if [[ -n "${TEMP_LINK}" && -L "${TEMP_LINK}" ]]; then
    rm -f -- "${TEMP_LINK}"
  fi
  if [[ -n "${BUILD_DIR}" && -d "${BUILD_DIR}" ]]; then
    case "${BUILD_DIR}" in
      "${RELEASES_DIR}"/.build-*) rm -rf -- "${BUILD_DIR}" ;;
      *) log "refusing to clean unexpected build path ${BUILD_DIR}" ;;
    esac
  fi
}
trap cleanup_temp EXIT

# Every git command runs as the service account, never as root.
#
# The unit is root so it can swap the /opt symlink and restart units, but the
# checkout belongs to usstocks. Fetching as root leaves root-owned objects and
# a root-owned .git/FETCH_HEAD inside a tree the service account owns, and the
# next manual command there dies with "cannot open '.git/FETCH_HEAD':
# Permission denied" -- after the deploy that caused it has already finished.
# Keeping one writer keeps the ownership uniform.
#
# GIT_SSH_COMMAND has to be passed explicitly: runuser builds a fresh
# environment, and losing it would drop the deploy key and the known_hosts
# path together.
git_as_service_account() {
  runuser -u "${PROJECT}" -- env \
    HOME="/var/lib/${PROJECT}" \
    GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-}" \
    git "$@"
}

git_repo() {
  git_as_service_account -c "safe.directory=${REPO_DIR}" -C "${REPO_DIR}" "$@"
}

atomic_current_link() {
  local target="$1"
  # A link to itself is unrecoverable without manual repair: every path through
  # it, including the one the units use to find the interpreter, fails with
  # ELOOP rather than ENOENT, and the message names symbolic links instead of
  # the release that is missing.
  if [[ -z "${target}" || "${target}" == "${CURRENT_LINK}" ]]; then
    log "refusing to point ${CURRENT_LINK} at ${target:-<empty>}"
    return 1
  fi
  TEMP_LINK="${CURRENT_LINK}.tmp.$$"
  rm -f -- "${TEMP_LINK}"
  ln -s "${target}" "${TEMP_LINK}"
  mv -Tf -- "${TEMP_LINK}" "${CURRENT_LINK}"
  TEMP_LINK=""
}

# The release `current` resolves to, or empty when there is none.
#
# readlink -e, not -f: -f prints the path even when the final component does
# not exist, so on a first deployment it reports the link itself as though it
# were a release directory.
resolved_current() {
  readlink -e "${CURRENT_LINK}" 2>/dev/null || true
}

systemd_release_is_current() {
  local sha="$1"
  local target=""
  target="$(resolved_current)"
  [[ "${target}" == "${RELEASES_DIR}/${sha}" ]] &&
    [[ -x "${target}/venv/bin/python" ]] &&
    [[ -d "${target}/web" ]] &&
    [[ -f "${target}/data/universe.csv" ]] &&
    [[ -f "${target}/data/universe_jp.csv" ]] &&
    [[ -x "${target}/backup.sh" ]] &&
    [[ -f "${target}/REVISION" ]] &&
    [[ "$(<"${target}/REVISION")" == "${sha}" ]]
}

compose_release_is_current() {
  local sha="$1"
  [[ -f "${COMPOSE_MARKER}" ]] && [[ "$(<"${COMPOSE_MARKER}")" == "${sha}" ]]
}

remove_release_dir() {
  local path="$1"
  case "${path}" in
    "${RELEASES_DIR}"/[0-9a-f]*)
      [[ "${path}" != "${RELEASES_DIR}" ]] && rm -rf -- "${path}"
      ;;
    *) log "refusing to remove unexpected release path ${path}"; return 1 ;;
  esac
}

build_systemd_release() {
  local sha="$1"
  local release_dir="${RELEASES_DIR}/${sha}"

  if [[ -x "${release_dir}/venv/bin/python" &&
        -d "${release_dir}/web" &&
        -f "${release_dir}/data/universe.csv" &&
        -f "${release_dir}/data/universe_jp.csv" &&
        -x "${release_dir}/backup.sh" &&
        -f "${release_dir}/REVISION" &&
        "$(<"${release_dir}/REVISION")" == "${sha}" ]]; then
    log "reusing previously built release ${sha:0:8}"
    return
  fi

  if [[ -e "${release_dir}" ]]; then
    log "discarding incomplete release ${release_dir}"
    remove_release_dir "${release_dir}"
  fi

  BUILD_DIR="${RELEASES_DIR}/.build-${sha}-$$"
  install -d -o usstocks -g usstocks -m 0755 "${BUILD_DIR}"

  log "creating Python release ${sha:0:8} (cached wheels: ${PIP_CACHE_DIR})"
  runuser -u usstocks -- env \
    HOME=/var/lib/usstocks \
    PIP_CACHE_DIR="${PIP_CACHE_DIR}" \
    python3 -m venv "${BUILD_DIR}/venv"
  runuser -u usstocks -- env \
    HOME=/var/lib/usstocks \
    PIP_CACHE_DIR="${PIP_CACHE_DIR}" \
    "${BUILD_DIR}/venv/bin/python" -m pip install \
      --disable-pip-version-check "${REPO_DIR}[parquet]"

  cp -a "${REPO_DIR}/web" "${BUILD_DIR}/web"
  install -d -o usstocks -g usstocks -m 0755 "${BUILD_DIR}/data"
  # Every universe CSV, not one file by name. Naming them individually meant
  # universe_jp.csv was committed, deployed, and still absent from the release.
  for csv in "${REPO_DIR}"/data/*.csv; do
    install -m 0644 "${csv}" "${BUILD_DIR}/data/$(basename "${csv}")"
  done
  install -m 0755 "${REPO_DIR}/deploy/backup/backup.sh" "${BUILD_DIR}/backup.sh"
  install -m 0755 \
    "${REPO_DIR}/deploy/analysis/import-ai-digests.sh" \
    "${BUILD_DIR}/analysis-import.sh"
  printf '%s\n' "${sha}" > "${BUILD_DIR}/REVISION"

  # Runtime processes can read but cannot mutate their own release.
  chown -R root:root "${BUILD_DIR}"
  chmod -R go-w "${BUILD_DIR}"
  mv -- "${BUILD_DIR}" "${release_dir}"
  BUILD_DIR=""
}

systemd_stack_healthy() {
  systemctl is-active --quiet usstocks-collector.service &&
    systemctl is-active --quiet usstocks-api.service &&
    curl -fsS --max-time 2 http://127.0.0.1:8000/api/livez >/dev/null
}

restart_systemd_stack() {
  systemctl restart usstocks-collector.service
  systemctl restart usstocks-api.service
}

rollback_systemd_release() {
  local previous_target="$1"
  if [[ -n "${previous_target}" && -d "${previous_target}" ]]; then
    log "rolling back current -> $(basename "${previous_target}")"
    atomic_current_link "${previous_target}"
    systemctl restart usstocks-collector.service || true
    systemctl restart usstocks-api.service || true
  else
    log "no previous release exists; stopping failed first deployment"
    systemctl stop usstocks-api.service usstocks-collector.service || true
    # An if, not `[[ ... ]] && unlink`: under set -e the && form makes the
    # whole function return non-zero whenever the link is already absent,
    # which is the ordinary case here.
    if [[ -L "${CURRENT_LINK}" ]]; then
      unlink "${CURRENT_LINK}"
    fi
  fi
}

cleanup_old_releases() {
  local current_target=""
  local kept=0
  local entry=""
  local path=""
  local -a entries=()

  [[ "${KEEP_RELEASES}" =~ ^[0-9]+$ ]] || {
    log "DEPLOY_KEEP_RELEASES must be an integer"
    return 1
  }
  (( KEEP_RELEASES >= 2 )) || KEEP_RELEASES=2

  current_target="$(resolved_current)"
  while IFS= read -r entry; do
    entries+=("${entry}")
  done < <(
    find "${RELEASES_DIR}" -mindepth 1 -maxdepth 1 -type d \
      -name '[0-9a-f]*' -printf '%T@ %p\n' | sort -rn
  )

  for entry in "${entries[@]}"; do
    path="${entry#* }"
    if [[ "${path}" == "${current_target}" ]]; then
      ((kept += 1))
      continue
    fi
    if (( kept < KEEP_RELEASES )); then
      ((kept += 1))
      continue
    fi
    log "removing old release $(basename "${path}")"
    remove_release_dir "${path}"
  done
}

deploy_systemd() {
  local sha="$1"
  local release_dir="${RELEASES_DIR}/${sha}"
  local previous_target=""
  local healthy=false

  install -d -o root -g root -m 0755 "${RELEASES_DIR}"
  install -d -o usstocks -g usstocks -m 0750 "${PIP_CACHE_DIR}"
  build_systemd_release "${sha}"

  previous_target="$(resolved_current)"
  log "switching current -> ${sha:0:8}"
  atomic_current_link "${release_dir}"

  if restart_systemd_stack; then
    for _attempt in $(seq 1 30); do
      if systemd_stack_healthy; then
        healthy=true
        break
      fi
      sleep 2
    done
  fi

  if [[ "${healthy}" != true ]]; then
    log "ERROR: release ${sha:0:8} did not become healthy"
    printf '%s\n' "${sha}" > "${FAILED_SYSTEMD_MARKER}"
    rollback_systemd_release "${previous_target}"
    return 1
  fi

  rm -f -- "${FAILED_SYSTEMD_MARKER}"
  cleanup_old_releases
  log "deploy complete at ${sha:0:8} (systemd)"
}

deploy_compose() {
  local sha="$1"

  ln -sfn "${ENV_FILE}" "${REPO_DIR}/.env"
  log "building and restarting Compose services"
  docker compose -f "${COMPOSE_FILE}" up -d --build --remove-orphans

  # Keep the previous tagged image as a quick manual rollback candidate.
  docker image prune -f --filter "dangling=true" >/dev/null 2>&1 || true

  for _attempt in $(seq 1 30); do
    if docker compose -f "${COMPOSE_FILE}" exec -T api \
      python -c \
        "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/livez')" \
      >/dev/null 2>&1; then
      printf '%s\n' "${sha}" > "${COMPOSE_MARKER}"
      log "deploy complete at ${sha:0:8} (compose)"
      return
    fi
    sleep 2
  done

  log "ERROR: api did not become healthy after Compose deploy of ${sha:0:8}"
  docker compose -f "${COMPOSE_FILE}" ps
  return 1
}

case "${RUNTIME}" in
  systemd | compose) ;;
  *) log "DEPLOY_RUNTIME must be 'systemd' or 'compose', got ${RUNTIME}"; exit 2 ;;
esac

mkdir -p "$(dirname "${LOCK_FILE}")"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  log "another deploy is in progress; exiting"
  exit 0
fi

[[ -f "${ENV_FILE}" ]] || { log "missing env file ${ENV_FILE}"; exit 1; }

if [[ ! -d "${REPO_DIR}/.git" ]]; then
  REPO_URL_FILE="/etc/${PROJECT}/repo-url"
  [[ -s "${REPO_URL_FILE}" ]] || {
    log "repository is absent and ${REPO_URL_FILE} is missing"
    exit 1
  }
  REPO_URL="$(<"${REPO_URL_FILE}")"
  log "first run: cloning ${REPO_URL}"
  install -d -o "${PROJECT}" -g "${PROJECT}" -m 0750 "$(dirname "${REPO_DIR}")"
  git_as_service_account clone --branch "${BRANCH}" "${REPO_URL}" "${REPO_DIR}"
fi

git_repo remote set-branches origin "${BRANCH}" >/dev/null 2>&1 || true
git_repo fetch --quiet origin "${BRANCH}"

local_sha="$(git_repo rev-parse HEAD)"
remote_sha="$(git_repo rev-parse "origin/${BRANCH}")"

if [[ "${RUNTIME}" == systemd &&
      -f "${FAILED_SYSTEMD_MARKER}" &&
      "$(<"${FAILED_SYSTEMD_MARKER}")" == "${remote_sha}" ]]; then
  log "revision ${remote_sha:0:8} previously failed; waiting for a new revision"
  exit 0
fi

if [[ "${local_sha}" == "${remote_sha}" ]]; then
  if [[ "${RUNTIME}" == systemd ]] && systemd_release_is_current "${remote_sha}"; then
    log "already at ${remote_sha:0:8}; nothing to do"
    exit 0
  fi
  if [[ "${RUNTIME}" == compose ]] && compose_release_is_current "${remote_sha}"; then
    log "already at ${remote_sha:0:8}; nothing to do"
    exit 0
  fi
else
  log "updating ${local_sha:0:8} -> ${remote_sha:0:8}"
fi

# Tracked local debugging edits are never silently discarded or deployed.
if ! git_repo diff --quiet || ! git_repo diff --cached --quiet; then
  log "working tree has local modifications; refusing to deploy"
  exit 1
fi

if [[ "${local_sha}" != "${remote_sha}" ]]; then
  git_repo checkout --quiet "${BRANCH}"
  git_repo reset --hard --quiet "origin/${BRANCH}"
fi

if [[ "${RUNTIME}" == systemd ]]; then
  deploy_systemd "${remote_sha}"
else
  deploy_compose "${remote_sha}"
fi
