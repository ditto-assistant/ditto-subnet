#!/usr/bin/env bash
# Run the production validator stack with a locally materialized, verified
# dittobench-api build context. Compose 2.40 and 5.0 can corrupt remote Git
# context URLs while converting builds to Bake; a local context avoids that
# compatibility bug without weakening the repository's ref+checksum pin.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/docker-compose.yml"

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

command -v docker >/dev/null 2>&1 || die "Docker is not installed"
command -v git >/dev/null 2>&1 || die "git is not installed"

if [ "$#" -eq 0 ]; then
  die "usage: $0 <docker compose arguments>"
fi

compose_version="$(docker compose version --short 2>/dev/null)" || \
  die "Docker Compose plugin v2 or newer is required"
compose_version="${compose_version#v}"
compose_major="${compose_version%%.*}"
case "$compose_major" in
  '' | *[!0-9]*) die "could not parse Docker Compose version: $compose_version" ;;
esac
if [ "$compose_major" -lt 2 ]; then
  die "Docker Compose plugin v2 or newer is required (found $compose_version)"
fi

docker info >/dev/null 2>&1 || die "Docker Engine is not reachable"
docker buildx version >/dev/null 2>&1 || die "Docker Buildx is not installed"

pinned_contexts="$(
  grep -Eo \
    'https://github\.com/ditto-assistant/dittobench-api\.git\?ref=[^&[:space:]]+&checksum=[0-9a-f]{40}' \
    "$COMPOSE_FILE" || true
)"
if [ -z "$pinned_contexts" ] || [ "$(printf '%s\n' "$pinned_contexts" | sort -u | wc -l | tr -d ' ')" -ne 1 ]; then
  die "docker-compose.yml must contain exactly one structured Git context pin"
fi
pinned_contexts="$(printf '%s\n' "$pinned_contexts" | sort -u)"

repository="${pinned_contexts%%\?*}"
ref_and_checksum="${pinned_contexts#*\?ref=}"
ref="${ref_and_checksum%%&checksum=*}"
checksum="${pinned_contexts##*checksum=}"
case "$checksum" in
  *[!0-9a-f]* | '') die "invalid dittobench-api checksum in docker-compose.yml" ;;
esac
if [ "${#checksum}" -ne 40 ]; then
  die "dittobench-api checksum must be a full 40-character commit"
fi

cache_home="${DITTO_SUBNET_BUILD_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/ditto-subnet}"
checkout="$cache_home/dittobench-api/$checksum"

if [ -e "$checkout" ] && [ ! -d "$checkout/.git" ]; then
  die "cache path exists but is not a Git checkout: $checkout"
fi

if [ ! -d "$checkout/.git" ]; then
  mkdir -p "$(dirname "$checkout")"
  git init --quiet "$checkout"
  git -C "$checkout" remote add origin "$repository"
fi

origin="$(git -C "$checkout" remote get-url origin)"
if [ "$origin" != "$repository" ]; then
  die "cached dittobench-api origin is $origin, expected $repository"
fi

if ! git -C "$checkout" cat-file -e "$checksum^{commit}" 2>/dev/null; then
  printf 'fetching pinned dittobench-api context %s\n' "$checksum" >&2
  git -C "$checkout" fetch --quiet --depth 1 origin "$ref"
  resolved="$(git -C "$checkout" rev-parse FETCH_HEAD)"
  if [ "$resolved" != "$checksum" ]; then
    die "dittobench-api $ref resolved to $resolved, expected $checksum"
  fi
fi

if [ -n "$(
  git -C "$checkout" status \
    --porcelain=v1 --untracked-files=all --ignored=matching
)" ]; then
  die "cached dittobench-api checkout has local changes: $checkout"
fi

git -C "$checkout" checkout --quiet --detach "$checksum"
head_checksum="$(git -C "$checkout" rev-parse HEAD)"
if [ "$head_checksum" != "$checksum" ]; then
  die "cached dittobench-api checkout is $head_checksum, expected $checksum"
fi
if [ ! -f "$checkout/Dockerfile" ]; then
  die "pinned dittobench-api checkout has no Dockerfile: $checkout"
fi

export DITTOBENCH_BUILD_CONTEXT="$checkout"
printf 'using dittobench-api %s with Docker Compose %s\n' \
  "$checksum" "$compose_version" >&2
exec docker compose --project-directory "$ROOT_DIR" -f "$COMPOSE_FILE" "$@"
