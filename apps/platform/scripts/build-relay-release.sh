#!/usr/bin/env bash
# Build and smoke-test a self-contained Platform relay artifact from the
# monorepo. The relay runtime is the Go model-relay service; the artifact ships
# one statically linked linux/amd64 binary, never anything that resolves
# differently (or not at all) on the release host. go.mod must not contain a
# local replace directive for the same reason the wheel builder refused local
# path exports: all inputs are pinned, and the release host never builds from
# source or resolves anything mutable.

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
# DITTO_RELAY_GO_MODULE_ROOT exists for the hermetic script tests; production
# always builds the monorepo module.
module_root="${DITTO_RELAY_GO_MODULE_ROOT:-$repo_root/services/model-relay}"
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

# Module hygiene, before any toolchain command runs. A replace directive points
# the build at a local path that does not exist outside this checkout, and a
# missing go.sum means the dependency set is not pinned at all.
[ -f "$module_root/go.mod" ] || {
  echo "ERROR: $module_root/go.mod is missing" >&2
  exit 1
}
[ -f "$module_root/go.sum" ] || {
  echo "ERROR: $module_root/go.sum is missing; the relay module must pin its dependencies" >&2
  exit 1
}
if grep -q '=>' "$module_root/go.mod"; then
  echo "ERROR: go.mod contains a replace directive; relay releases must not depend on local module paths" >&2
  exit 1
fi

staging="$(mktemp -d "$artifact_parent/.relay-artifact.XXXXXX")"
smoke_root="$(mktemp -d "${TMPDIR:-/tmp}/relay-artifact-smoke.XXXXXX")"
cleanup() {
  rm -rf -- "$staging" "$smoke_root"
}
trap cleanup EXIT

# go.sum must be tidy: a stale lock hides dependencies the build would resolve
# over the network, which is the Go analogue of an incomplete transitive lock.
(cd "$module_root" && go mod tidy -diff) || {
  echo "ERROR: go.mod/go.sum are not tidy (go mod tidy -diff failed)" >&2
  exit 1
}
(cd "$module_root" && go mod verify) || {
  echo "ERROR: go mod verify failed; the module cache does not match go.sum" >&2
  exit 1
}

# Reproducible, statically linked linux/amd64 binary. -trimpath removes the
# builder's paths, -buildvcs=false keeps the stamp independent of git state,
# and the ldflags -X stamp is how a no-git artifact knows its own identity
# (the deploy script additionally exports DITTO_BUILD_COMMIT for /health).
build_flags=(-trimpath -buildvcs=false -ldflags "-X main.buildCommit=$source_commit")
(
  cd "$module_root" &&
    CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
      go build "${build_flags[@]}" -o "$staging/model-relay" ./cmd/model-relay
)
[ -s "$staging/model-relay" ] && [ -x "$staging/model-relay" ] || {
  echo "ERROR: go build did not produce a model-relay binary" >&2
  exit 1
}

cp "$platform_root/scripts/ecosystem.config.js" "$staging/ecosystem.config.js"
cp "$platform_root/scripts/deploy-relay-release.sh" "$staging/deploy-relay-release.sh"
printf '%s\n' "$source_commit" > "$staging/source-commit"

sha256_tool() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$@"
  else
    shasum -a 256 "$@"
  fi
}
(
  cd "$staging" &&
    sha256_tool model-relay ecosystem.config.js deploy-relay-release.sh source-commit \
      > SHA256SUMS
)

# Smoke: the artifact binary must run and report the stamped commit. A linux
# CI runner executes the shipped linux/amd64 binary itself. On a non-linux dev
# machine that cross-compiled binary cannot execute, so a host-native build of
# the same package at the same commit runs the identical self-check instead --
# verification is never silently skipped, and CI always exercises the real
# artifact. This proves the binary starts and knows its identity; the canary
# on the host is what proves it can boot, connect, and answer.
host_os="$(uname -s)"
host_arch="$(uname -m)"
if [ "$host_os" = "Linux" ] && { [ "$host_arch" = "x86_64" ] || [ "$host_arch" = "amd64" ]; }; then
  smoke_bin="$staging/model-relay"
else
  smoke_bin="$smoke_root/model-relay"
  (
    cd "$module_root" &&
      CGO_ENABLED=0 go build "${build_flags[@]}" -o "$smoke_bin" ./cmd/model-relay
  )
fi
smoke_output="$("$smoke_bin" --version)" || {
  echo "ERROR: model-relay --version self-check failed" >&2
  exit 1
}
case "$smoke_output" in
  *"$source_commit"*) ;;
  *)
    echo "ERROR: model-relay --version did not report $source_commit" >&2
    exit 1
    ;;
esac

mv "$staging" "$artifact_dir"
trap - EXIT
rm -rf -- "$smoke_root"
echo "relay-artifact=$artifact_dir"
