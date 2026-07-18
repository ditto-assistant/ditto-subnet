#!/usr/bin/env bash
# Execute the immutable Compose model carried by a validated validator-stack
# descriptor. This is intentionally separate from validator-compose.sh: local
# source builds and the legacy validator-only updater keep their old boundary.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${DITTO_SUBNET_ENV_FILE:-$ROOT_DIR/.env}"
STATE_DIR="${DITTO_VALIDATOR_STACK_UPDATE_STATE_DIR:-$ROOT_DIR/.validator-stack-update}"

die() {
  printf 'validator-stack-compose: error: %s\n' "$*" >&2
  exit 1
}

require_absolute_path() {
  case "$2" in
    /*) ;;
    *) die "$1 must be an absolute path" ;;
  esac
  case "$2" in
    *$'\n'* | *$'\r'*) die "$1 must not contain a newline" ;;
  esac
}

[ "$#" -ge 2 ] || die "usage: $0 <validated-release-directory> <docker compose arguments>"
release_dir="$1"
shift
require_absolute_path release_dir "$release_dir"
[ -d "$release_dir" ] || die "release directory does not exist: $release_dir"
[ ! -L "$release_dir" ] || die "release directory must not be a symbolic link"
release_dir="$(cd "$release_dir" && pwd -P)"
[ -d "$STATE_DIR" ] && [ ! -L "$STATE_DIR" ] || die "validator stack state directory is unavailable"
STATE_DIR="$(cd "$STATE_DIR" && pwd -P)"
case "$release_dir" in
  "$STATE_DIR/current" | "$STATE_DIR/previous" | "$STATE_DIR/staged") ;;
  *) die "release directory is not updater-validated state" ;;
esac
compose_file="$release_dir/compose.yml"
manifest_file="$release_dir/manifest.env"
[ -f "$compose_file" ] && [ ! -L "$compose_file" ] || die "validated release has no regular compose.yml"
[ -f "$manifest_file" ] && [ ! -L "$manifest_file" ] || die "validated release has no regular manifest.env"
[ -f "$release_dir/.descriptor-ref" ] && [ ! -L "$release_dir/.descriptor-ref" ] || die "validated release has no regular .descriptor-ref"
[ -f "$ENV_FILE" ] || die "validator environment file does not exist: $ENV_FILE"

descriptor_ref="$(cat "$release_dir/.descriptor-ref")"
[[ "$descriptor_ref" =~ ^ghcr\.io/ditto-assistant/ditto-subnet-stack@sha256:[0-9a-f]{64}$ ]] || die "validated release descriptor reference is malformed"
case "$descriptor_ref" in *$'\n'* | *$'\r'*) die "validated release descriptor reference must be one line";; esac

# The installed current release must remain bound to the digest atomically
# recorded by the updater. Candidate/previous directories are instead bound by
# their root-owned, updater-created .descriptor-ref during a transaction.
current_dir="$STATE_DIR/current"
if [ "$release_dir" = "$current_dir" ]; then
  managed_file="$STATE_DIR/managed-release.env"
  managed_ref=""
  if [ -f "$managed_file" ] && [ ! -L "$managed_file" ]; then
    managed_ref="$(awk -F= '$1 == "STACK_RELEASE" { print substr($0, index($0, "=") + 1) }' "$managed_file")"
  fi
  transaction_file="$STATE_DIR/transaction.env"
  transaction_ref=""
  if [ -f "$transaction_file" ] && [ ! -L "$transaction_file" ]; then
    transaction_phase="$(awk -F= '$1 == "PHASE" { print substr($0, index($0, "=") + 1) }' "$transaction_file")"
    case "$transaction_phase" in
      committed | migration_started)
        transaction_ref="$(awk -F= '$1 == "CANDIDATE_RELEASE" { print substr($0, index($0, "=") + 1) }' "$transaction_file")"
        ;;
    esac
  fi
  { [ "$managed_ref" = "$descriptor_ref" ] || [ "$transaction_ref" = "$descriptor_ref" ]; } || die "managed current release descriptor does not match installed state"
fi

allowed_keys=' STACK_FORMAT_VERSION STACK_VERSION STACK_REVISION DITTOBENCH_REVISION COMPATIBILITY_EPOCH UPDATE_PROTOCOL COMPOSE_SCHEMA HEARTBEAT_PROTOCOL VALIDATOR_IMAGE SANDBOX_DOCKER_IMAGE DITTOBENCH_API_IMAGE MODEL_RELAY_IMAGE PYLON_IMAGE OLLAMA_IMAGE '
seen_keys='|'
while IFS= read -r line || [ -n "$line" ]; do
  [ -n "$line" ] || continue
  [[ "$line" =~ ^([A-Z][A-Z0-9_]*)=([^[:space:]]+)$ ]] || die "manifest contains a malformed line"
  key="${BASH_REMATCH[1]}"
  value="${BASH_REMATCH[2]}"
  [[ "$allowed_keys" == *" $key "* ]] || die "manifest contains unknown key $key"
  [[ "$seen_keys" != *"|$key|"* ]] || die "manifest contains duplicate key $key"
  seen_keys="${seen_keys}${key}|"
done <"$manifest_file"

for key in $allowed_keys; do
  [[ "$seen_keys" == *"|$key|"* ]] || die "manifest is missing $key"
done

manifest_value() {
  awk -F= -v key="$1" '$1==key { print substr($0,index($0,"=")+1); exit }' "$manifest_file"
}

export STACK_FORMAT_VERSION="$(manifest_value STACK_FORMAT_VERSION)"
export STACK_VERSION="$(manifest_value STACK_VERSION)"
export STACK_REVISION="$(manifest_value STACK_REVISION)"
export DITTOBENCH_REVISION="$(manifest_value DITTOBENCH_REVISION)"
export COMPATIBILITY_EPOCH="$(manifest_value COMPATIBILITY_EPOCH)"
export UPDATE_PROTOCOL="$(manifest_value UPDATE_PROTOCOL)"
export COMPOSE_SCHEMA="$(manifest_value COMPOSE_SCHEMA)"
export HEARTBEAT_PROTOCOL="$(manifest_value HEARTBEAT_PROTOCOL)"
export VALIDATOR_IMAGE="$(manifest_value VALIDATOR_IMAGE)"
export SANDBOX_DOCKER_IMAGE="$(manifest_value SANDBOX_DOCKER_IMAGE)"
export DITTOBENCH_API_IMAGE="$(manifest_value DITTOBENCH_API_IMAGE)"
export MODEL_RELAY_IMAGE="$(manifest_value MODEL_RELAY_IMAGE)"
export PYLON_IMAGE="$(manifest_value PYLON_IMAGE)"
export OLLAMA_IMAGE="$(manifest_value OLLAMA_IMAGE)"
export VALIDATOR_STACK_DESCRIPTOR_REF="$descriptor_ref"

wallets_dir="${DITTO_BITTENSOR_WALLETS_DIR:-}"
if [ -z "$wallets_dir" ]; then
  wallets_dir="$(awk -F= '$1 == "DITTO_BITTENSOR_WALLETS_DIR" { print substr($0, index($0, "=") + 1) }' "$ENV_FILE" | tail -1)"
fi
wallets_dir="${wallets_dir:-$HOME/.bittensor/wallets}"
require_absolute_path DITTO_BITTENSOR_WALLETS_DIR "$wallets_dir"
export DITTO_BITTENSOR_WALLETS_DIR="$wallets_dir"

command -v docker >/dev/null 2>&1 || die "Docker is not installed"
docker info >/dev/null 2>&1 || die "Docker Engine is not reachable"
compose_version="$(docker compose version --short 2>/dev/null)" || die "Docker Compose plugin v2 or newer is required"
compose_version="${compose_version#v}"
compose_major="${compose_version%%.*}"
[[ "$compose_major" =~ ^[0-9]+$ ]] && [ "$compose_major" -ge 2 ] || die "Docker Compose plugin v2 or newer is required"

if [ "${DITTO_ALLOW_MANAGED_STACK_MUTATION:-false}" != "true" ]; then
  for argument in "$@"; do
    case "$argument" in
      up | down | create | restart | start | stop | kill | rm | run | pause | unpause | pull | build)
        die "managed stack mutation must run through validator-stack-auto-update.sh"
        ;;
    esac
  done
fi

exec docker compose --project-directory "$ROOT_DIR" --env-file "$ENV_FILE" \
  -f "$compose_file" "$@"
