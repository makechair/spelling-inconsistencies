#!/usr/bin/env bash
#
# Creates the S3 bucket that holds the Terraform state.
#
# This is the one thing Terraform cannot do for itself: the backend must exist
# before `terraform init` can read or write state. Run it once per account,
# with the dev01 profile, then never again.
#
# Usage:
#   scripts/bootstrap-tf-state.sh [profile] [region]
set -Eeuo pipefail

PROFILE="${1:-dev01}"
REGION="${2:-ap-northeast-1}"
PROJECT="${PROJECT:-usstocks}"
ENVIRONMENT="${ENVIRONMENT:-dev01}"

command -v aws >/dev/null || { echo "aws CLI not found" >&2; exit 1; }

ACCOUNT_ID="$(aws sts get-caller-identity --profile "${PROFILE}" --query Account --output text)"
BUCKET="${PROJECT}-tfstate-${ENVIRONMENT}-${ACCOUNT_ID}"

echo "account : ${ACCOUNT_ID}"
echo "region  : ${REGION}"
echo "bucket  : ${BUCKET}"

if aws s3api head-bucket --bucket "${BUCKET}" --profile "${PROFILE}" 2>/dev/null; then
  echo "bucket already exists; nothing to do"
else
  # us-east-1 rejects a LocationConstraint; every other region requires one.
  if [[ "${REGION}" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "${BUCKET}" --region "${REGION}" --profile "${PROFILE}"
  else
    aws s3api create-bucket --bucket "${BUCKET}" --region "${REGION}" \
      --create-bucket-configuration "LocationConstraint=${REGION}" --profile "${PROFILE}"
  fi
  echo "created ${BUCKET}"
fi

# Versioning matters here in a way it does not for the backups: state is
# overwritten in place, so a corrupt write is only recoverable from a previous
# version.
aws s3api put-bucket-versioning --bucket "${BUCKET}" \
  --versioning-configuration Status=Enabled --profile "${PROFILE}"

aws s3api put-bucket-encryption --bucket "${BUCKET}" --profile "${PROFILE}" \
  --server-side-encryption-configuration '{
    "Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]
  }'

aws s3api put-public-access-block --bucket "${BUCKET}" --profile "${PROFILE}" \
  --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

# State holds resource metadata and is worth keeping small; old versions past
# a couple of months have no practical recovery value.
aws s3api put-bucket-lifecycle-configuration --bucket "${BUCKET}" --profile "${PROFILE}" \
  --lifecycle-configuration '{
    "Rules":[{
      "ID":"expire-old-state-versions",
      "Status":"Enabled",
      "Filter":{"Prefix":""},
      "NoncurrentVersionExpiration":{"NoncurrentDays":90},
      "AbortIncompleteMultipartUpload":{"DaysAfterInitiation":7}
    }]
  }'

cat <<EOF

Done. Next:

  cd infra/terraform
  cp backend.hcl.example backend.hcl
  cp dev01.tfvars.example dev01.tfvars
  # set bucket=${BUCKET} in backend.hcl, and tf_state_bucket in dev01.tfvars

  terraform init -backend-config=backend.hcl
  terraform plan  -var-file=dev01.tfvars
  terraform apply -var-file=dev01.tfvars
EOF
