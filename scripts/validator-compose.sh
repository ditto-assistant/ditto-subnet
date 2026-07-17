#!/usr/bin/env bash
# Run the production validator stack with a locally materialized, verified
# dittobench-api build context. Compose 2.40 and 5.0 can corrupt remote Git
# context URLs while converting builds to Bake; a local context avoids that
# compatibility bug without weakening the repository's ref+checksum pin.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/docker-compose.yml"
STATE_DIR="${DITTO_VALIDATOR_UPDATE_STATE_DIR:-$ROOT_DIR/.validator-update}"
MANAGED_IMAGE_FILE="$STATE_DIR/managed-image.env"

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

require_absolute_path() {
  local name="$1"
  local value="$2"
  case "$value" in
    /*) ;;
    *) die "$name must be an absolute path" ;;
  esac
  case "$value" in
    *$'\n'* | *$'\r'*) die "$name must not contain a newline" ;;
  esac
}

command -v docker >/dev/null 2>&1 || die "Docker is not installed"
command -v git >/dev/null 2>&1 || die "git is not installed"

wallets_dir="${DITTO_BITTENSOR_WALLETS_DIR:-$HOME/.bittensor/wallets}"
require_absolute_path DITTO_BITTENSOR_WALLETS_DIR "$wallets_dir"
export DITTO_BITTENSOR_WALLETS_DIR="$wallets_dir"

if [ "$#" -eq 0 ]; then
  die "usage: $0 <docker compose arguments>"
fi

managed_image=""
if [ -f "$MANAGED_IMAGE_FILE" ]; then
  if [ "$(awk 'NF { count++ } END { print count + 0 }' "$MANAGED_IMAGE_FILE")" -ne 1 ]; then
    die "managed image state must contain exactly one non-empty line"
  fi
  managed_line="$(awk 'NF { print; exit }' "$MANAGED_IMAGE_FILE")"
  case "$managed_line" in
    DITTO_SUBNET_IMAGE=*) managed_image="${managed_line#DITTO_SUBNET_IMAGE=}" ;;
    *) die "managed image state is malformed: $MANAGED_IMAGE_FILE" ;;
  esac
  if [[ ! "$managed_image" =~ ^ghcr\.io/ditto-assistant/ditto-subnet-validator@sha256:[0-9a-f]{64}$ ]] &&
    [[ ! "$managed_image" =~ ^ditto-subnet-validator-rollback:[0-9a-z-]+$ ]]; then
    die "managed image is not an immutable validator digest or retained rollback tag"
  fi
  if [ "${DITTO_ALLOW_MANAGED_VALIDATOR_MUTATION:-false}" != "true" ] && \
    [ -n "${DITTO_SUBNET_IMAGE:-}" ] && \
    [ "$DITTO_SUBNET_IMAGE" != "$managed_image" ]; then
    die "DITTO_SUBNET_IMAGE cannot override the adopted managed validator image"
  fi
  if [ "${DITTO_ALLOW_MANAGED_VALIDATOR_MUTATION:-false}" != "true" ]; then
    export DITTO_SUBNET_IMAGE="$managed_image"
  fi
fi

if [ "${1:-}" = "managed-reconcile" ]; then
  [ -n "$managed_image" ] || die "managed-reconcile requires an adopted registry image"
  [ "${DITTO_ALLOW_MANAGED_SIDECAR_RECONCILE:-false}" = "true" ] || \
    die "managed-reconcile must run through validator-auto-update.sh reconcile-sidecars"
  sidecar_ready_timeout="${DITTO_SIDECAR_READY_TIMEOUT_SECONDS:-180}"
  [[ "$sidecar_ready_timeout" =~ ^[1-9][0-9]*$ ]] || \
    die "DITTO_SIDECAR_READY_TIMEOUT_SECONDS must be a positive integer"
  set -- up -d --build --no-deps --wait --wait-timeout "$sidecar_ready_timeout" \
    pylon sandbox-docker model-relay ollama dittobench-api
elif [ -n "$managed_image" ] && [ "${DITTO_ALLOW_MANAGED_VALIDATOR_MUTATION:-false}" != "true" ]; then
  for argument in "$@"; do
    case "$argument" in
      up | down | create | restart | start | stop | kill | rm | run | pause | unpause)
        die "managed validator mode blocks broad Compose mutation; use managed-reconcile or the validator updater"
        ;;
    esac
  done
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

# Read-only/lifecycle commands do not consume a build context. Keep them usable
# with already-built images and during a GitHub outage; ancestry is verified
# only before a command that can materialize a scorer image.
materialize_context=false
for argument in "$@"; do
  case "$argument" in
    up | build | create | run | --build) materialize_context=true ;;
  esac
done
if [ "$materialize_context" != "true" ]; then
  exec docker compose --project-directory "$ROOT_DIR" -f "$COMPOSE_FILE" "$@"
fi

source_revision="$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null)" || \
  die "could not resolve ditto-subnet source revision"
if [[ ! "$source_revision" =~ ^[0-9a-f]{40}$ ]]; then
  die "ditto-subnet source revision must be a full Git SHA"
fi
export DITTO_SOURCE_REVISION="$source_revision"
export DITTO_SOURCE_IDENTITY="local-source:$source_revision"

context_override="${DITTOBENCH_BUILD_CONTEXT:-}"
if [ -n "$context_override" ]; then
  pinned_contexts="$context_override"
else
  pinned_contexts="$(
    grep -Eo \
      'https://github\.com/ditto-assistant/dittobench-api\.git\?ref=[^&[:space:]]+&checksum=[0-9a-f]{40}' \
      "$COMPOSE_FILE" || true
  )"
fi
if [ -z "$pinned_contexts" ] || [ "$(printf '%s\n' "$pinned_contexts" | sort -u | wc -l | tr -d ' ')" -ne 1 ]; then
  die "docker-compose.yml must contain exactly one structured Git context pin"
fi
pinned_contexts="$(printf '%s\n' "$pinned_contexts" | sort -u)"

repository="${pinned_contexts%%\?*}"
ref_and_checksum="${pinned_contexts#*\?ref=}"
ref="${ref_and_checksum%%&checksum=*}"
checksum="${pinned_contexts##*checksum=}"
[ "$repository" = "https://github.com/ditto-assistant/dittobench-api.git" ] || \
  die "dittobench-api context must use the official repository"
local_smoke=false
if [ "$ref" != "refs/heads/main" ]; then
  if [ "${DITTOBENCH_ALLOW_UNMERGED_SMOKE:-false}" != "true" ] || \
    [ "${SUBTENSOR_NETWORK:-finney}" != "local" ]; then
    die "unmerged dittobench-api refs require local network smoke opt-in"
  fi
  case "$ref" in
    refs/heads/*) local_smoke=true ;;
    *) die "local dittobench-api smoke ref must be a branch" ;;
  esac
fi
case "$checksum" in
  *[!0-9a-f]* | '') die "invalid dittobench-api checksum in docker-compose.yml" ;;
esac
if [ "${#checksum}" -ne 40 ]; then
  die "dittobench-api checksum must be a full 40-character commit"
fi

cache_home="${DITTO_SUBNET_BUILD_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/ditto-subnet}"
checkout="$cache_home/dittobench-api/$checksum"
verified_marker="$checkout.ref-verified"
verification_record="$ref $checksum"

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
  printf 'fetching pinned dittobench-api context %s (%s)\n' \
    "$checksum" "$ref" >&2
  git -C "$checkout" fetch --quiet --depth 1 origin "$checksum"
  resolved="$(git -C "$checkout" rev-parse FETCH_HEAD)"
  if [ "$resolved" != "$checksum" ]; then
    die "dittobench-api fetch resolved to $resolved, expected $checksum"
  fi
fi

# A checksum may intentionally lag main, but it must have landed there. Verify
# ancestry once, then cache that evidence beside the immutable SHA checkout so
# routine restarts remain independent of GitHub availability.
if [ ! -f "$verified_marker" ] || \
  [ "$(cat "$verified_marker")" != "$verification_record" ]; then
  printf 'verifying pinned dittobench-api commit is on %s\n' "$ref" >&2
  git -C "$checkout" fetch --quiet origin "$ref" || \
    die "could not fetch dittobench-api $ref for ancestry verification"
  resolved_ref="$(git -C "$checkout" rev-parse FETCH_HEAD)"
  if [ "$local_smoke" = "true" ] && [ "$resolved_ref" != "$checksum" ]; then
    die "local smoke checksum $checksum is not the current $ref commit"
  elif [ "$local_smoke" != "true" ] && \
    ! git -C "$checkout" merge-base --is-ancestor "$checksum" FETCH_HEAD; then
    die "dittobench-api checksum $checksum is not in $ref history"
  fi
  printf '%s\n' "$verification_record" > "$verified_marker"
fi

if [ -n "$(
  git -c core.fileMode=false -C "$checkout" status \
    --porcelain=v1 --untracked-files=all --ignored=matching
)" ]; then
  die "cached dittobench-api checkout has local changes: $checkout"
fi

git -C "$checkout" checkout --quiet --detach "$checksum"

# Git's immutable tree is authoritative for executable modes. Some migrations,
# archive restores, and filesystems preserve contents but drop +x, which can
# make a later Docker build fail even though the cached checkout has the right
# commit. Repair mode-only drift, then fail closed if any other drift remains.
while IFS=' ' read -r -d '' tracked_mode tracked_path; do
  case "$tracked_mode" in
    100755) chmod 0755 "$checkout/$tracked_path" ;;
    100644) chmod 0644 "$checkout/$tracked_path" ;;
  esac
done < <(
  git -C "$checkout" ls-tree -r -z \
    --format='%(objectmode) %(path)' "$checksum"
)

if [ -n "$(
  git -c core.fileMode=true -C "$checkout" status \
    --porcelain=v1 --untracked-files=all --ignored=matching
)" ]; then
  die "cached dittobench-api checkout could not be normalized: $checkout"
fi

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
