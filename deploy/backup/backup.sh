#!/usr/bin/env bash
#
# Consistent SQLite backup to S3 (spec 4.4, 10.4).
#
# Copying a live WAL database with `cp` yields a file that may not open. This
# uses `VACUUM INTO`, which takes a read lock, writes a defragmented copy, and
# leaves the collector running.
#
# Note the disk requirement the spec omits (docs/spec-review.md C-1): the copy
# is roughly the size of the database itself, so the volume needs at least that
# much free space on top of normal growth. The script refuses to start
# otherwise rather than filling the disk and taking the collector down with it.

set -Eeuo pipefail

DB_PATH="${USSTOCKS_DB_PATH:-/var/lib/usstocks/market.db}"
BACKUP_DIR="${USSTOCKS_BACKUP_DIR:-/var/backups/usstocks}"
S3_URI="${USSTOCKS_BACKUP_S3_URI:-}"            # e.g. s3://my-bucket/usstocks
KEEP_LOCAL="${USSTOCKS_BACKUP_KEEP_LOCAL:-3}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARTIFACT="${BACKUP_DIR}/market-${STAMP}.db"

log() { printf '%s backup: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
cleanup() { rm -f "${ARTIFACT}" "${ARTIFACT}.gz.part" 2>/dev/null || true; }
trap cleanup ERR

[[ -f "${DB_PATH}" ]] || { log "database not found: ${DB_PATH}"; exit 1; }
mkdir -p "${BACKUP_DIR}"

db_bytes=$(stat -c %s "${DB_PATH}")
free_bytes=$(df --output=avail -B1 "${BACKUP_DIR}" | tail -1)
if (( free_bytes < db_bytes * 2 )); then
  log "refusing to run: need $((db_bytes * 2)) bytes free, have ${free_bytes}"
  exit 1
fi

log "creating consistent copy of ${DB_PATH} (${db_bytes} bytes)"
sqlite3 "file:${DB_PATH}?mode=ro" "VACUUM INTO '${ARTIFACT}'"

# Prove the copy opens and passes a structural check before it is trusted.
integrity=$(sqlite3 "${ARTIFACT}" "PRAGMA integrity_check;" || echo "failed")
if [[ "${integrity}" != "ok" ]]; then
  log "integrity check failed: ${integrity}"
  exit 1
fi
bars=$(sqlite3 "${ARTIFACT}" "SELECT COUNT(*) FROM bars_1m;")
log "copy verified: ${bars} bars"

gzip -9 "${ARTIFACT}"
ARCHIVE="${ARTIFACT}.gz"
log "compressed to $(stat -c %s "${ARCHIVE}") bytes"

if [[ -n "${S3_URI}" ]]; then
  # STANDARD_IA suits write-once/read-rarely backups. Lifecycle rules on the
  # bucket handle generation retention (spec 10.4); the script does not try to
  # delete remote objects itself.
  aws s3 cp "${ARCHIVE}" "${S3_URI}/daily/$(basename "${ARCHIVE}")" \
    --storage-class STANDARD_IA --only-show-errors
  log "uploaded to ${S3_URI}/daily/$(basename "${ARCHIVE}")"
else
  log "USSTOCKS_BACKUP_S3_URI unset; keeping local copy only"
fi

# Local copies are a convenience; S3 is the retention system of record.
find "${BACKUP_DIR}" -name 'market-*.db.gz' -type f -printf '%T@ %p\n' \
  | sort -rn | tail -n "+$((KEEP_LOCAL + 1))" | cut -d' ' -f2- \
  | xargs -r rm -f

log "done"
