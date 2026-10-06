#!/usr/bin/env bash
# Mock Terraform plans only: no backend, credentials, real resources or keys.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
QA="$(mktemp -d)"
trap 'rm -rf -- "$QA"' EXIT
# Copy the actual dedicated production root, including its pinned provider lock.
# Exclude only remote backend and enabled public intent from credential-free QA.
cp -R "$ROOT/infra/terraform/stacks/gcp-collector-custody/." "$QA/"
rm "$QA/backend.tf" "$QA/prod.auto.tfvars"
cp -R "$ROOT/infra/terraform/tests/collector-custody/tests" "$QA/"
rm "$QA/tests/manual-mailbox.tftest.hcl"
terraform -chdir="$QA" init -backend=false -input=false -lockfile=readonly
terraform -chdir="$QA" validate
terraform -chdir="$QA" test -no-color

# The new project uses the identical resource graph and phase tests with a
# different hard project pin; dereference only these repository-owned symlinks.
GAMMA="$QA/gamma-qa"
mkdir "$GAMMA"
cp -RL "$ROOT/infra/terraform/stacks/gcp-gamma-custody/." "$GAMMA/"
rm "$GAMMA/backend.tf" "$GAMMA/prod.auto.tfvars"
cp -R "$ROOT/infra/terraform/tests/collector-custody/tests" "$GAMMA/"
cp "$ROOT/infra/terraform/tests/gamma-custody/tests/"*.tftest.hcl "$GAMMA/tests/"
sed -i 's/ditto-app-dev/sn118-gamma-custody/g' "$GAMMA/tests/custody.tftest.hcl"
terraform -chdir="$GAMMA" init -backend=false -input=false -lockfile=readonly
terraform -chdir="$GAMMA" validate
terraform -chdir="$GAMMA" test -no-color

# The already-authorized preview backend bootstraps the new backend through
# two exact-object grants only. Test its actual definitions without cloud calls.
BACKEND="$QA/backend-qa"
mkdir "$BACKEND"
cp "$ROOT/infra/terraform/stacks/gcp-preview/"*.tf "$BACKEND/"
cp "$ROOT/infra/terraform/stacks/gcp-preview/.terraform.lock.hcl" "$BACKEND/"
rm "$BACKEND/backend.tf"
cp -R "$ROOT/infra/terraform/tests/collector-backend/tests" "$BACKEND/"
terraform -chdir="$BACKEND" init -backend=false -input=false -lockfile=readonly
terraform -chdir="$BACKEND" validate
terraform -chdir="$BACKEND" test -no-color
