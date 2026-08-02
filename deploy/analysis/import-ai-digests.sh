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

install -d -m 0750 /var/lib/usstocks/corpus/analysis/daily
aws s3 sync \
  "${EXCHANGE_URI}/output/daily/" \
  /var/lib/usstocks/corpus/analysis/daily/ \
  --exclude '*' \
  --include 'date=*/ai_digest.json' \
  --only-show-errors
