#!/usr/bin/env bash
# Build and smoke-test a self-contained Platform relay artifact from the
# monorepo. Local workspace dependencies are shipped as wheels, never as paths
# that can resolve differently (or not at all) on the release host.

set -euo pipefail

artifact_dir="${1:?usage: build-relay-release.sh <artifact-dir> <source-commit>}"
source_commit="${2:?usage: build-relay-release.sh <artifact-dir> <source-commit>}"
[[ "$source_commit" =~ ^[0-9a-f]{40}$ ]] || {
  echo "ERROR: source commit must be a full lowercase SHA" >&2
  exit 1
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
platform_root="$(cd "$script_dir/.." && pwd)"
repo_root="$(git -C "$platform_root" rev-parse --show-toplevel)"
shared_root="$repo_root/packages/ditto-screening-protocol"
head_commit="$(git -C "$repo_root" rev-parse HEAD)"
[ "$head_commit" = "$source_commit" ] || {
  echo "ERROR: source commit $source_commit does not match checkout $head_commit" >&2
  exit 1
}

case "$artifact_dir" in
  /*) ;;
  *) artifact_dir="$PWD/$artifact_dir" ;;
esac
[ ! -e "$artifact_dir" ] || {
  echo "ERROR: artifact path already exists: $artifact_dir" >&2
  exit 1
}
artifact_parent="$(dirname "$artifact_dir")"
mkdir -p "$artifact_parent"

staging="$(mktemp -d "$artifact_parent/.relay-artifact.XXXXXX")"
smoke_root="$(mktemp -d "${TMPDIR:-/tmp}/relay-artifact-smoke.XXXXXX")"
cleanup() {
  rm -rf -- "$staging" "$smoke_root"
}
trap cleanup EXIT

uv build --wheel --no-create-gitignore --out-dir "$staging" "$shared_root"
uv build --wheel --no-create-gitignore --out-dir "$staging" "$platform_root"
uv export --project "$platform_root" --frozen --no-dev --no-emit-project \
  --no-emit-package ditto-screening-protocol --no-header --no-annotate \
  --format requirements-txt --output-file "$staging/requirements.lock" \
  >/dev/null

if grep -Eq 'file:|packages/ditto-screening-protocol|ditto-screening-protocol' \
  "$staging/requirements.lock"; then
  echo "ERROR: exported requirements contain a local shared-package reference" >&2
  exit 1
fi

shopt -s nullglob
platform_wheels=("$staging"/ditto_platform-*.whl)
protocol_wheels=("$staging"/ditto_screening_protocol-*.whl)
shopt -u nullglob
[ "${#platform_wheels[@]}" -eq 1 ] || {
  echo "ERROR: build must produce exactly one platform wheel" >&2
  exit 1
}
[ "${#protocol_wheels[@]}" -eq 1 ] || {
  echo "ERROR: build must produce exactly one screening-protocol wheel" >&2
  exit 1
}

cp "$platform_root/scripts/ecosystem.config.js" "$staging/ecosystem.config.js"
cp "$platform_root/scripts/deploy-relay-release.sh" "$staging/deploy-relay-release.sh"
printf '%s\n' "$source_commit" > "$staging/source-commit"

# This is the same install order used on the VM. It catches missing wheels,
# local-path exports, incomplete transitive locks, and import failures before an
# artifact can be uploaded.
uv venv --python 3.12 "$smoke_root/.venv"
uv pip install --python "$smoke_root/.venv/bin/python" \
  --requirement "$staging/requirements.lock"
uv pip install --python "$smoke_root/.venv/bin/python" --no-deps \
  "${protocol_wheels[0]}"
uv pip install --python "$smoke_root/.venv/bin/python" --no-deps \
  "${platform_wheels[0]}"
"$smoke_root/.venv/bin/python" -c \
  'import ditto.api_server; import ditto_screening_protocol'

mv "$staging" "$artifact_dir"
trap - EXIT
rm -rf -- "$smoke_root"
echo "relay-artifact=$artifact_dir"
