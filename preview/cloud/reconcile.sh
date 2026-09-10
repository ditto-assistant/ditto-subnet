#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
. preview/cloud/lib.sh
preview_require GCP_PREVIEW_LEASE_BUCKET GCP_PREVIEW_ZONE GITHUB_REPOSITORY

# A preview lives for as long as its PR is open, capped absolutely by the lease
# TTL, and is clipped this long after the PR closes or merges. Retiring the
# instant a PR closes is cheaper, but it destroys the environment out from under
# whoever was still looking at it.
grace_seconds="${PREVIEW_CLOSED_GRACE_SECONDS:-14400}"
[[ "$grace_seconds" =~ ^[0-9]+$ ]] || preview_die "invalid closed grace"
[ "$grace_seconds" -le 86400 ] || preview_die "closed grace exceeds 24 hours"

now="$(preview_now_epoch)"
for slot in {0..7}; do
  uri="$(preview_lease_uri "$slot")"
  lease="$(gcloud storage cat "$uri" 2>/dev/null || true)"
  [ -n "$lease" ] || continue
  pr="$(jq -r '.pr // empty' <<<"$lease")"
  expires="$(jq -r '.expires_at_epoch // 0' <<<"$lease")"

  # The absolute cap. Nothing outlives its lease, open PR or not.
  if ! [[ "$expires" =~ ^[0-9]+$ ]] || [ "$expires" -lt "$now" ]; then
    PREVIEW_PR="$pr" preview/cloud/retire.sh
    continue
  fi

  state=missing
  closed_at=''
  if pr_json="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${pr}" 2>/dev/null)"; then
    state="$(jq -r '.state // "missing"' <<<"$pr_json")"
    closed_at="$(jq -r '.closed_at // empty' <<<"$pr_json")"
  fi
  [ "$state" != open ] || continue

  # Fail closed: an unreadable API answer, a deleted PR, or a closed PR with no
  # closed_at earns no grace and is retired now, exactly as before.
  closed_epoch=''
  if [ "$state" != missing ] && [ -n "$closed_at" ]; then
    closed_epoch="$(preview_epoch_from_iso8601 "$closed_at" || true)"
  fi
  if [ -n "$closed_epoch" ] && [ "$(( closed_epoch + grace_seconds ))" -gt "$now" ]; then
    continue
  fi
  PREVIEW_PR="$pr" preview/cloud/retire.sh
done
