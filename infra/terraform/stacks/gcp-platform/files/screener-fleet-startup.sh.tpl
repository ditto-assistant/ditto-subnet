#!/usr/bin/env bash
# Startup script for prod screener FLEET instances (rendered by Terraform —
# see screener-fleet.tf). Deliberately thin: fetch the read-only deploy key,
# clone ditto-subnet, and hand off to the worker-owned bootstrap script so the
# provisioning logic is versioned with the worker it provisions.
#
# Runs on every boot; scripts/bootstrap-screener.sh is idempotent and exits
# fast once its marker file exists.
set -euo pipefail

exec > >(tee -a /var/log/screener-bootstrap.log) 2>&1
echo "==> screener fleet bootstrap $(date -u +%FT%TZ)"

# A stock Debian image ships /opt but not /opt/ditto; create the tree before the
# marker check and the clone below both reference children of it.
install -d -m 0755 /opt/ditto

if [[ -f /opt/ditto/.screener-bootstrapped ]]; then
  echo "already bootstrapped; nothing to do"
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl ca-certificates

# Read-only deploy key for the private repo (GCE Debian images ship gcloud;
# the VM's attached SA has secretAccessor on the deploy-key secret).
install -d -m 0700 /root/.ssh
gcloud secrets versions access latest \
  --project="${project}" --secret="${deploy_key_secret}" \
  >/root/.ssh/screener_deploy_key
chmod 0600 /root/.ssh/screener_deploy_key
# Pinned github.com host keys (from https://api.github.com/meta ssh_keys), NOT
# ssh-keyscan: the root clone below fetches the bootstrap code, so a TOFU
# keyscan would let a network attacker serve a malicious checkout to root.
cat >/root/.ssh/known_hosts <<'KNOWN_HOSTS'
github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl
github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=
github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXmPUWZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/wsjk=
KNOWN_HOSTS
chmod 0644 /root/.ssh/known_hosts

if [[ ! "${git_revision}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "refusing to boot without an immutable released source SHA" >&2
  exit 2
fi
rm -rf /opt/ditto/bootstrap-src
install -d -m 0755 /opt/ditto/bootstrap-src
export GIT_SSH_COMMAND="ssh -i /root/.ssh/screener_deploy_key -o UserKnownHostsFile=/root/.ssh/known_hosts"
git -C /opt/ditto/bootstrap-src init --quiet
git -C /opt/ditto/bootstrap-src remote add origin "${repo_url}"
git -C /opt/ditto/bootstrap-src fetch --depth 1 origin "${git_revision}"
resolved_revision="$(git -C /opt/ditto/bootstrap-src rev-parse FETCH_HEAD)"
test "$resolved_revision" = "${git_revision}"
git -C /opt/ditto/bootstrap-src checkout --detach "$resolved_revision"

# The root clone above is finished, and everything past this point runs as the
# unprivileged screener user. Drop the root-only ssh settings rather than
# letting them leak across the handoff: bootstrap-screener.sh clones via
# `runuser -u deploy`, which preserves the environment, so an inherited
# GIT_SSH_COMMAND pointed at /root/.ssh/* makes that clone fail closed.
unset GIT_SSH_COMMAND

exec env \
  SCREENER_GCP_PROJECT="${project}" \
  SCREENER_PLATFORM_API_URL="${platform_api_url}" \
  SCREENER_HOTKEY="${screener_hotkey}" \
  NETUID="${netuid}" \
  SCREENER_MNEMONIC_SECRET="${mnemonic_secret}" \
  SCREENER_API_TOKEN_SECRET="${api_token_secret}" \
  SCREENER_READINESS_PORT="${readiness_port}" \
  SCREENER_DEPLOY_KEY_FILE=/root/.ssh/screener_deploy_key \
  SCREENER_REPOSITORY_URL="${repo_url}" \
  SCREENER_EXPECTED_SHA="${git_revision}" \
  /opt/ditto/bootstrap-src/workers/screener/scripts/bootstrap-screener.sh
