#!/usr/bin/env bash
set -euo pipefail

instance="${1:?usage: deploy-via-ssh.sh INSTANCE ZONE EXPECTED_SHA}"
zone="${2:?usage: deploy-via-ssh.sh INSTANCE ZONE EXPECTED_SHA}"
expected_sha="${3:?usage: deploy-via-ssh.sh INSTANCE ZONE EXPECTED_SHA}"
project="${GCP_PROJECT:?GCP_PROJECT is not set}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
updater="$script_dir/update-controller.sh"
ssh_attempts="${SCREENER_CONTROLLER_SSH_ATTEMPTS:-4}"
ssh_retry_delay="${SCREENER_CONTROLLER_SSH_RETRY_DELAY_SECONDS:-5}"

if [[ ! "$expected_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "expected SHA must be a full lowercase commit SHA" >&2
  exit 2
fi

if [[ ! "$ssh_attempts" =~ ^[1-9][0-9]*$ ]] || [[ ! "$ssh_retry_delay" =~ ^[0-9]+$ ]]; then
  echo "SSH retry settings must be non-negative integers with at least one attempt" >&2
  exit 2
fi

# Stream the reviewed updater over the same IAP SSH session that executes it.
# This lets the first release carrying an updater fix use that fix immediately,
# and avoids a second SSH connection racing OS Login key propagation. A newly
# registered ephemeral OS Login key can take a few seconds to reach sshd, so
# retry the complete single-session operation instead of failing the release on
# the first publickey rejection.
for ((attempt = 1; attempt <= ssh_attempts; attempt++)); do
  if gcloud compute ssh "$instance" \
    --project "$project" --zone "$zone" --tunnel-through-iap --quiet \
    --command "sudo -n env SCREENER_CONTROLLER_EXPECTED_SHA=$expected_sha /bin/bash -s" \
    <"$updater"; then
    exit 0
  fi
  if ((attempt == ssh_attempts)); then
    break
  fi
  echo "controller SSH attempt $attempt/$ssh_attempts failed; retrying in ${ssh_retry_delay}s" >&2
  sleep "$ssh_retry_delay"
done

echo "controller SSH failed after $ssh_attempts attempts" >&2
exit 1
