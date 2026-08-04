#!/usr/bin/env bash
set -euo pipefail

base_revision="${1:?usage: verify-version-bump.sh BASE_REVISION}"
repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

version_file="research/dittobench-datagen/internal/version/version.go"
zero_revision=0000000000000000000000000000000000000000

read_version() {
  sed -n 's/^const Version = "\([0-9][0-9.]*\)"$/\1/p'
}

current_version="$(read_version <"$version_file")"
[[ "$current_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "datagen release refused: invalid current component version" >&2
  exit 1
}

if [ "$base_revision" = "$zero_revision" ]; then
  exit 0
fi
[[ "$base_revision" =~ ^[0-9a-f]{40}$ ]]
git cat-file -e "$base_revision^{commit}"
if ! git cat-file -e "$base_revision:$version_file" 2>/dev/null; then
  exit 0
fi

previous_version="$(git show "$base_revision:$version_file" | read_version)"
[[ "$previous_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "datagen release refused: invalid component version at release base" >&2
  exit 1
}
if [ "$current_version" = "$previous_version" ]; then
  echo "datagen release refused: bump $version_file when datagen changes" >&2
  exit 1
fi
