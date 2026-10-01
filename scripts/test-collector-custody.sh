#!/usr/bin/env bash
# Mock Terraform plans only: no backend, credentials, real resources or keys.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
QA="$(mktemp -d)"
trap 'rm -rf -- "$QA"' EXIT
cp -R "$ROOT/infra/terraform/tests/collector-custody/." "$QA/"
cp "$ROOT/infra/terraform/stacks/gcp-platform/collector-custody.tf" "$QA/"
mkdir "$QA/files"
cp "$ROOT/infra/terraform/stacks/gcp-platform/files/collector-custody-startup.sh.tpl" "$QA/files/"
terraform -chdir="$QA" init -backend=false -input=false
terraform -chdir="$QA" validate
terraform -chdir="$QA" test -no-color
