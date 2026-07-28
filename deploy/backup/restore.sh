#!/usr/bin/env bash
#
# Restore a backup and verify it (spec 4.4: "periodically restore to another
# environment and confirm the backup is valid").
#
# Usage:
#   restore.sh s3://bucket/usstocks/daily/market-20260728T071000Z.db.gz /tmp/restored.db
#   restore.sh /var/backups/usstocks/market-20260728T071000Z.db.gz /tmp/restored.db
#
# Restoring over a running system is deliberately not automated: it is a
# destructive step that should be a conscious decision. This writes to the
# target path and reports what it contains.

set -Eeuo pipefail

SOURCE="${1:-}"
TARGET="${2:-}"

if [[ -z "${SOURCE}" || -z "${TARGET}" ]]; then
  echo "usage: $0 <s3-uri-or-path> <target.db>" >&2
  exit 2
fi

if [[ -e "${TARGET}" ]]; then
  echo "refusing to overwrite existing file: ${TARGET}" >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

if [[ "${SOURCE}" == s3://* ]]; then
  aws s3 cp "${SOURCE}" "${WORK}/backup.gz" --only-show-errors
else
  cp "${SOURCE}" "${WORK}/backup.gz"
fi

gunzip -c "${WORK}/backup.gz" > "${TARGET}"

integrity=$(sqlite3 "${TARGET}" "PRAGMA integrity_check;")
if [[ "${integrity}" != "ok" ]]; then
  echo "integrity check FAILED: ${integrity}" >&2
  exit 1
fi

echo "restored to ${TARGET}"
sqlite3 "${TARGET}" <<'SQL'
.mode column
.headers on
SELECT COUNT(*) AS bars, COUNT(DISTINCT symbol) AS symbols,
       MIN(timestamp_utc) AS first_bar, MAX(timestamp_utc) AS last_bar
FROM bars_1m;
SELECT source, COUNT(*) AS rows FROM bars_1m GROUP BY source;
SQL

cat <<'NOTE'

Next step after a real recovery: start the collector against this database. It
queues a REST backfill from the last stored bar, so the effective data loss is
much smaller than the 24-hour RPO implies (docs/spec-review.md B-9). Backfill
spends the hourly REST allowance, so a large gap takes several hours to close.
NOTE
