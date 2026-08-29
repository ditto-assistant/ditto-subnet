#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
. preview/cloud/lib.sh
preview_require PREVIEW_PR GCP_PREVIEW_LEASE_BUCKET GCP_PREVIEW_ZONE
[[ "$PREVIEW_PR" =~ ^[1-9][0-9]*$ ]] || preview_die "invalid PR"

for slot in {0..7}; do
  uri="$(preview_lease_uri "$slot")"
  lease="$(gcloud storage cat "$uri" 2>/dev/null || true)"
  [ -n "$lease" ] || continue
  [ "$(jq -r '.pr // empty' <<<"$lease")" = "$PREVIEW_PR" ] || continue
  instance="$(preview_instance_name "$slot")"
  gcloud compute instances delete "$instance" --zone "$GCP_PREVIEW_ZONE" --quiet 2>/dev/null || true
  gcloud storage rm "$uri" 2>/dev/null || true
done
