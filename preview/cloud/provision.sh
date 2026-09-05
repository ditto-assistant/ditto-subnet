#!/usr/bin/env bash
# Trusted-controller entrypoint. This file runs from the default branch only.
set -euo pipefail
cd "$(dirname "$0")/../.."
. preview/cloud/lib.sh

preview_require PREVIEW_PR PREVIEW_SHA PREVIEW_PROFILE GITHUB_REPOSITORY \
  GCP_PREVIEW_LEASE_BUCKET GCP_PREVIEW_SNAPSHOT_BUCKET GCP_PREVIEW_NETWORK \
  GCP_PREVIEW_SUBNETWORK GCP_PREVIEW_RUNTIME_SERVICE_ACCOUNT GCP_PREVIEW_ZONE
preview_validate_identity
test "$GITHUB_REPOSITORY" = "ditto-assistant/ditto-subnet" || preview_die "wrong repository"

lease_ttl_seconds="${PREVIEW_LEASE_TTL_SECONDS:-86400}"
[[ "$lease_ttl_seconds" =~ ^[0-9]+$ ]] || preview_die "invalid lease TTL"
[ "$lease_ttl_seconds" -ge 3600 ] && [ "$lease_ttl_seconds" -le 172800 ] || preview_die "lease TTL outside 1-48 hours"

head_json="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${PREVIEW_PR}")"
test "$(jq -r .state <<<"$head_json")" = open || preview_die "PR is not open"
test "$(jq -r .head.repo.full_name <<<"$head_json")" = "$GITHUB_REPOSITORY" || preview_die "fork previews are not allowed"
test "$(jq -r .head.sha <<<"$head_json")" = "$PREVIEW_SHA" || preview_die "stale PR head"

slot=''
completed=false
lease_file=''
metadata_file=''
cleanup() {
  rm -f "$lease_file" "$metadata_file"
  if [ "$completed" != true ] && [ -n "$slot" ]; then
    gcloud compute instances delete "$(preview_instance_name "$slot")" --zone "$GCP_PREVIEW_ZONE" --quiet >/dev/null 2>&1 || true
    gcloud storage rm "$(preview_lease_uri "$slot")" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
for candidate in {0..7}; do
  uri="$(preview_lease_uri "$candidate")"
  if current="$(gcloud storage cat "$uri" 2>/dev/null)"; then
    if [ "$(jq -r '.pr // empty' <<<"$current")" = "$PREVIEW_PR" ]; then
      slot="$candidate"
      break
    fi
    expires_at="$(jq -r '.expires_at_epoch // 0' <<<"$current")"
    if [[ "$expires_at" =~ ^[0-9]+$ ]] && [ "$expires_at" -lt "$(preview_now_epoch)" ]; then
      stale_instance="$(preview_instance_name "$candidate")"
      gcloud compute instances delete "$stale_instance" --zone "$GCP_PREVIEW_ZONE" --quiet >&2 || true
      gcloud storage rm "$uri" >&2 || true
    else
      continue
    fi
  fi

  lease_file="$(mktemp)"
  jq -n \
    --argjson slot "$candidate" \
    --argjson pr "$PREVIEW_PR" \
    --arg sha "$PREVIEW_SHA" \
    --arg profile "$PREVIEW_PROFILE" \
    --argjson created_at_epoch "$(preview_now_epoch)" \
    --argjson expires_at_epoch "$(( $(preview_now_epoch) + lease_ttl_seconds ))" \
    '{schema:1,slot:$slot,pr:$pr,sha:$sha,profile:$profile,created_at_epoch:$created_at_epoch,expires_at_epoch:$expires_at_epoch}' \
    >"$lease_file"
  if gcloud storage cp --if-generation-match=0 "$lease_file" "$uri" >/dev/null 2>&1; then
    slot="$candidate"
    break
  fi
done
[ -n "$slot" ] || preview_die "all 8 preview slots are active"

instance="$(preview_instance_name "$slot")"
gcloud compute instances delete "$instance" --zone "$GCP_PREVIEW_ZONE" --quiet 2>/dev/null || true

snapshot_url=''
if [ "$PREVIEW_PROFILE" = stack-copy ]; then
  snapshot_object="$(gcloud storage ls "gs://${GCP_PREVIEW_SNAPSHOT_BUCKET}/sanitized/*.dump" | sort | tail -n 1)"
  [ -n "$snapshot_object" ] || preview_die "no sanitized snapshot is available"
  snapshot_region="${GCP_PREVIEW_ZONE%-*}"
  snapshot_url="$(gcloud storage sign-url "$snapshot_object" --duration=2h --region="$snapshot_region" --impersonate-service-account="${GCP_PREVIEW_CONTROLLER_SERVICE_ACCOUNT:?}" --format='value(signed_url)')"
fi

metadata_file="$(mktemp)"
jq -n \
  --argjson pr "$PREVIEW_PR" \
  --arg sha "$PREVIEW_SHA" \
  --arg profile "$PREVIEW_PROFILE" \
  --arg snapshot_url "$snapshot_url" \
  '{pr:$pr,sha:$sha,profile:$profile,snapshot_url:$snapshot_url}' >"$metadata_file"

gcloud compute instances create "$instance" \
  --zone "$GCP_PREVIEW_ZONE" \
  --machine-type "${GCP_PREVIEW_MACHINE_TYPE:-e2-standard-8}" \
  --boot-disk-size "${GCP_PREVIEW_DISK_SIZE:-100GB}" \
  --boot-disk-type pd-balanced \
  --image-family ubuntu-2404-lts-amd64 \
  --image-project ubuntu-os-cloud \
  --network "$GCP_PREVIEW_NETWORK" \
  --subnet "$GCP_PREVIEW_SUBNETWORK" \
  --service-account "$GCP_PREVIEW_RUNTIME_SERVICE_ACCOUNT" \
  --scopes cloud-platform \
  --tags sn118-preview \
  --labels "system=sn118-preview,preview-slot=${slot},preview-pr=${PREVIEW_PR}" \
  --metadata-from-file startup-script=preview/cloud/startup.sh,preview-config="$metadata_file" \
  --shielded-secure-boot \
  --shielded-vtpm \
  --shielded-integrity-monitoring \
  --quiet >&2

ip="$(gcloud compute instances describe "$instance" --zone "$GCP_PREVIEW_ZONE" --format='value(networkInterfaces[0].accessConfigs[0].natIP)')"
[ -n "$ip" ] || preview_die "preview VM has no public IP"
base="${ip}.sslip.io"
dashboard_url="https://dashboard.${base}"
platform_url="https://platform.${base}"
backroom_url="https://backroom.${base}"

ready=false
for _ in {1..90}; do
  if curl --fail --silent --show-error --max-time 5 "$platform_url/__preview/health" \
    | jq -e --arg sha "$PREVIEW_SHA" '.status == "ok" and .sha == $sha' >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 10
done

lease_file="$(mktemp)"
jq -n \
  --argjson slot "$slot" --argjson pr "$PREVIEW_PR" --arg sha "$PREVIEW_SHA" \
  --arg profile "$PREVIEW_PROFILE" --arg instance "$instance" --arg ip "$ip" \
  --arg dashboard_url "$dashboard_url" --arg platform_url "$platform_url" --arg backroom_url "$backroom_url" \
  --argjson ready "$ready" --argjson created_at_epoch "$(preview_now_epoch)" \
  --argjson expires_at_epoch "$(( $(preview_now_epoch) + lease_ttl_seconds ))" \
  '{schema:1,slot:$slot,pr:$pr,sha:$sha,profile:$profile,instance:$instance,ip:$ip,dashboard_url:$dashboard_url,platform_url:$platform_url,backroom_url:$backroom_url,ready:$ready,created_at_epoch:$created_at_epoch,expires_at_epoch:$expires_at_epoch}' \
  >"$lease_file"
gcloud storage cp "$lease_file" "$(preview_lease_uri "$slot")" >&2

jq -n --argjson slot "$slot" --argjson ready "$ready" \
  --arg dashboard_url "$dashboard_url" --arg platform_url "$platform_url" --arg backroom_url "$backroom_url" \
  '{slot:$slot,ready:$ready,dashboard_url:$dashboard_url,platform_url:$platform_url,backroom_url:$backroom_url}'
[ "$ready" = true ] || preview_die "preview did not become healthy within 15 minutes"
completed=true
