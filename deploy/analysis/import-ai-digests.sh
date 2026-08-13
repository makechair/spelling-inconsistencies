#!/usr/bin/env bash
set -Eeuo pipefail

ENV_FILE="${USSTOCKS_ENV_FILE:-/etc/usstocks/usstocks.env}"
[[ -r "${ENV_FILE}" ]] || { echo "missing ${ENV_FILE}" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

EXCHANGE_URI="${USSTOCKS_ANALYSIS_EXCHANGE_S3_URI:-${USSTOCKS_BACKUP_S3_URI%/}/analysis-exchange}"
[[ "${EXCHANGE_URI}" == s3://* ]] || { echo "analysis exchange S3 URI is invalid" >&2; exit 1; }

# mkdir, not install -d: the latter chmods a directory that already exists,
# and a chmod is a write even when the mode is already right. Under
# ProtectSystem the failure then reads as "Read-only file system" on a
# directory nobody needed to change.
mkdir -p /var/lib/usstocks/corpus/analysis/daily
aws s3 sync \
  "${EXCHANGE_URI}/output/daily/" \
  /var/lib/usstocks/corpus/analysis/daily/ \
  --exclude '*' \
  --include 'date=*/ai_digest.json' \
  --only-show-errors

# The fundamentals guide, written by the same Mac-side pass. Absent until the
# bridge has run once, which is not an error.
mkdir -p /var/lib/usstocks/corpus/fundamentals
aws s3 cp \
  "${EXCHANGE_URI}/output/fundamentals/digest.json" \
  /var/lib/usstocks/corpus/fundamentals/digest.json \
  --only-show-errors || echo "no fundamentals digest yet"
