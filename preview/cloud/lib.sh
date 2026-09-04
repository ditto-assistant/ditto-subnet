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
