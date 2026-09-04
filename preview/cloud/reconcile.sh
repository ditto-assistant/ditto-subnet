#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
. preview/cloud/lib.sh
preview_require GCP_PREVIEW_LEASE_BUCKET GCP_PREVIEW_ZONE GITHUB_REPOSITORY

now="$(preview_now_epoch)"
for slot in {0..7}; do
  uri="$(preview_lease_uri "$slot")"
  lease="$(gcloud storage cat "$uri" 2>/dev/null || true)"
  [ -n "$lease" ] || continue
  pr="$(jq -r '.pr // empty' <<<"$lease")"
  expires="$(jq -r '.expires_at_epoch // 0' <<<"$lease")"
  state="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${pr}" --jq .state 2>/dev/null || echo missing)"
  if [ "$state" != open ] || ! [[ "$expires" =~ ^[0-9]+$ ]] || [ "$expires" -lt "$now" ]; then
    PREVIEW_PR="$pr" preview/cloud/retire.sh
  fi
done
