#!/usr/bin/env bash
set -euo pipefail

instance="${1:?usage: deploy-via-ssh.sh INSTANCE ZONE EXPECTED_SHA}"
zone="${2:?usage: deploy-via-ssh.sh INSTANCE ZONE EXPECTED_SHA}"
expected_sha="${3:?usage: deploy-via-ssh.sh INSTANCE ZONE EXPECTED_SHA}"
project="${GCP_PROJECT:?GCP_PROJECT is not set}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
updater="$script_dir/update-controller.sh"

if [[ ! "$expected_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "expected SHA must be a full lowercase commit SHA" >&2
  exit 2
fi

# Stream the reviewed updater over the same IAP SSH session that executes it.
# This lets the first release carrying an updater fix use that fix immediately,
# and avoids a second SSH connection racing OS Login key propagation.
exec gcloud compute ssh "$instance" \
  --project "$project" --zone "$zone" --tunnel-through-iap --quiet \
  --command "sudo -n env SCREENER_CONTROLLER_EXPECTED_SHA=$expected_sha /bin/bash -s" \
  <"$updater"
