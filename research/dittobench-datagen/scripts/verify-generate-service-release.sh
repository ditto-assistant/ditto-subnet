#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"
module_dir="research/dittobench-datagen"

[[ -z "$(git status --porcelain)" ]] || {
  echo "release verification refused: checkout is dirty" >&2
  exit 1
}

source_commit="$(git rev-parse HEAD)"
monorepo_tag="$(git tag --points-at HEAD | awk '/^v[0-9]+\.[0-9]+\.[0-9]+$/ { print }')"
[[ "$(printf '%s\n' "$monorepo_tag" | sed '/^$/d' | wc -l | tr -d ' ')" == "1" ]] || {
  echo "release verification refused: HEAD must have exactly one semver release tag" >&2
  exit 1
}
git merge-base --is-ancestor HEAD origin/main || {
  echo "release verification refused: tagged commit is not reachable from fetched origin/main" >&2
  exit 1
}

declared_version="$(sed -n 's/^const Version = "\([0-9][0-9.]*\)"$/\1/p' "$module_dir/internal/version/version.go")"
[[ "$declared_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "release verification refused: invalid component version $declared_version" >&2
  exit 1
}
component_tag="v$declared_version"
[[ "$component_tag" = "$monorepo_tag" ]] || {
  echo "release verification refused: datagen version $component_tag does not match monorepo release $monorepo_tag" >&2
  exit 1
}

(
  cd "$module_dir"
  go test ./...
  go test ./gen -run '^TestV7KnownVector$' -count=1
)
docker build \
  --build-arg "SOURCE_COMMIT=$source_commit" \
  --build-arg "SOURCE_TAG=$component_tag" \
  --file "$module_dir/cmd/generate-service/Dockerfile" \
  --tag "generate-service:$component_tag" \
  "$module_dir"

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  printf 'component_tag=%s\n' "$component_tag" >>"$GITHUB_OUTPUT"
fi
