#!/usr/bin/env bash
# One-time creation of the AWS Terraform state bucket for this repository.
#
# The bucket must exist before ../aws-bootstrap can use it as a backend, so it
# is created with the AWS CLI once and then imported into that root (see its
# import blocks), where every setting below is reviewed and enforced.
#
# Prints the exact commands by default. Pass --apply to run them, as an
# account administrator (an SSO AdministratorAccess session, not the root
# user). Idempotent: an existing bucket owned by this account is re-hardened.
set -euo pipefail

ACCOUNT_ID=339713011188
REGION=us-east-1
BUCKET="ditto-subnet-tfstate-${ACCOUNT_ID}"

apply=false
if [ "${1:-}" = "--apply" ]; then
  apply=true
fi

run() {
  if $apply; then
    "$@"
  else
    printf '%q ' "$@"
    printf '\n'
  fi
}

if $apply; then
  caller="$(aws sts get-caller-identity --query Account --output text)"
  if [ "$caller" != "$ACCOUNT_ID" ]; then
    echo "refusing: credentials are for account ${caller}, expected ${ACCOUNT_ID}" >&2
    exit 1
  fi
  if aws sts get-caller-identity --query Arn --output text | grep -q ':root$'; then
    echo "refusing: use an SSO administrator session, not the root user" >&2
    exit 1
  fi
fi

if $apply && aws s3api head-bucket --bucket "$BUCKET" --expected-bucket-owner "$ACCOUNT_ID" 2>/dev/null; then
  echo "bucket ${BUCKET} already exists in ${ACCOUNT_ID}; re-applying its hardening"
else
  # us-east-1 rejects an explicit LocationConstraint.
  run aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"
fi
run aws s3api put-bucket-ownership-controls --bucket "$BUCKET" \
  --ownership-controls 'Rules=[{ObjectOwnership=BucketOwnerEnforced}]'
run aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
run aws s3api put-bucket-versioning --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled
run aws s3api put-bucket-encryption --bucket "$BUCKET" \
  --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'
