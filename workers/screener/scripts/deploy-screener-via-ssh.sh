#!/usr/bin/env bash
set -euo pipefail

# Stream the updater over the same SSH connection that executes it. Using scp
# first makes gcloud establish a second session with a newly managed ephemeral
# SSH key; the follow-up connection can race guest-agent key propagation even
# though the upload succeeded.

instance="${1:?usage: deploy-screener-via-ssh.sh INSTANCE ZONE EXPECTED_SHA}"
zone="${2:?usage: deploy-screener-via-ssh.sh INSTANCE ZONE EXPECTED_SHA}"
expected_sha="${3:?usage: deploy-screener-via-ssh.sh INSTANCE ZONE EXPECTED_SHA}"
project="${GCP_PROJECT:?GCP_PROJECT is not set}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
updater="$script_dir/update-screener.sh"

if [[ ! "$expected_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "expected SHA must be a full lowercase commit SHA" >&2
  exit 2
fi

# exec preserves gcloud/SSH's exit status exactly. There is no remote temporary
# file to clean up, and there is deliberately no transport retry: after SSH has
# accepted stdin the updater may hold the deploy lock or be mutating the host.
exec gcloud compute ssh "$instance" \
  --project "$project" --zone "$zone" --tunnel-through-iap --quiet \
  --command "sudo -n env SCREENER_EXPECTED_SHA=$expected_sha /bin/bash -s" \
  <"$updater"
