#!/usr/bin/env bash
set -euo pipefail

preview_die() {
  echo "preview: $*" >&2
  exit 2
}

preview_require() {
  local name
  for name in "$@"; do
    [ -n "${!name:-}" ] || preview_die "missing required environment variable: $name"
  done
}

preview_validate_identity() {
  [[ "${PREVIEW_PR:-}" =~ ^[1-9][0-9]*$ ]] || preview_die "PREVIEW_PR must be a positive integer"
  [[ "${PREVIEW_SHA:-}" =~ ^[0-9a-f]{40}$ ]] || preview_die "PREVIEW_SHA must be an exact commit"
  case "${PREVIEW_PROFILE:-}" in
    stack|stack-copy) ;;
    *) preview_die "PREVIEW_PROFILE must be stack or stack-copy" ;;
  esac
}

preview_instance_name() {
  printf 'sn118-preview-%s\n' "$1"
}

preview_lease_uri() {
  printf 'gs://%s/slots/%s.json\n' "$GCP_PREVIEW_LEASE_BUCKET" "$1"
}

preview_now_epoch() {
  date -u +%s
}

# GitHub timestamps are RFC3339 UTC. This only ever runs on the ubuntu-24.04
# controller runner, so GNU date is available; on a Mac it returns non-zero and
# the only caller degrades to "no grace", which is the fail-closed direction.
preview_epoch_from_iso8601() {
  local value="$1"
  [[ "$value" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] || return 1
  date -u -d "$value" +%s 2>/dev/null
}
