#!/usr/bin/env bash
# Mock Terraform plans only: no backend, credentials, real resources or keys.
# Plans the real gcp-platform/cpm-scorer.tf with the root's provider pins and
# lock beside harness.tf, mirroring scripts/test-collector-custody.sh.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$HERE/../../stacks/gcp-platform" && pwd)"
QA="$(mktemp -d)"
trap 'rm -rf -- "$QA"' EXIT
cp "$ROOT_DIR/cpm-scorer.tf" "$ROOT_DIR/versions.tf" "$ROOT_DIR/.terraform.lock.hcl" "$QA/"
cp "$HERE/harness.tf" "$QA/"
cp -R "$HERE/tests" "$QA/"
terraform -chdir="$QA" init -backend=false -input=false -lockfile=readonly
terraform -chdir="$QA" validate
terraform -chdir="$QA" test -no-color
