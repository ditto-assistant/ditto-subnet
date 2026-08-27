#!/usr/bin/env bash
# Reclaim only caches and temp on a Platform app VM. Mutating. Requires the
# exact confirmation string. Never touches traces, Postgres, .env, live logs,
# venvs, docker images, or the git working tree.
set -euo pipefail

readonly PROJECT="${DITTO_GCP_PROJECT:-ditto-app-dev}"
readonly ZONE="${DITTO_GCP_ZONE:-us-central1-a}"
readonly DEFAULT_INSTANCE="ditto-platform-prod"
readonly CONFIRMATION="RECLAIM PLATFORM DISK CACHES"

usage() {
    cat >&2 <<'EOF'
Usage: reclaim_platform_disk_caches.sh "RECLAIM PLATFORM DISK CACHES" [instance]

Clears apt archives, journal vacuum to 80M, /home/deploy/.cache/uv,
/home/deploy/.npm/_cacache, and git gc on /opt/ditto-subnet.

Does not truncate pm2 logs, delete /opt/ditto-platform-relay/traces,
or grow the boot disk. Inspect first:

  .agents/skills/gcloud-ditto-readonly/scripts/inspect_platform_disk.sh
EOF
}

if [[ $# -lt 1 || $# -gt 2 ]]; then
    usage
    exit 2
fi
if [[ "$1" == "-h" || "$1" == "--help" ]]; then
    usage
    exit 0
fi
if [[ "$1" != "$CONFIRMATION" ]]; then
    echo "error: confirmation must be exactly: $CONFIRMATION" >&2
    exit 2
fi

instance="${2:-$DEFAULT_INSTANCE}"
if [[ ! "$instance" =~ ^ditto-platform-(prod|dev)$ ]]; then
    echo "error: instance must be ditto-platform-prod or ditto-platform-dev" >&2
    exit 2
fi

command -v gcloud >/dev/null 2>&1 || {
    echo "error: gcloud is required" >&2
    exit 127
}

# `uv cache prune` as deploy can fail when the SSH user's uv.toml is unreadable.
# Delete the deploy-owned cache directory instead.
remote_command=$(
    cat <<'REMOTE'
set -euo pipefail
echo "=== df before ==="
df -h /
echo
sudo -n apt-get clean
echo "apt-get clean ok"
sudo -n journalctl --vacuum-size=80M
if [ -d /home/deploy/.cache/uv ]; then
    sudo -n rm -rf /home/deploy/.cache/uv
    echo "removed /home/deploy/.cache/uv"
fi
if [ -d /home/deploy/.npm/_cacache ]; then
    sudo -n rm -rf /home/deploy/.npm/_cacache
    echo "removed /home/deploy/.npm/_cacache"
fi
if [ -d /opt/ditto-subnet/.git ]; then
    sudo -n -u deploy env HOME=/home/deploy git -C /opt/ditto-subnet gc --prune=now
    echo "git gc /opt/ditto-subnet ok"
fi
echo
echo "=== df after ==="
df -h /
REMOTE
)

gcloud compute ssh "$instance" \
    --project="$PROJECT" \
    --zone="$ZONE" \
    --tunnel-through-iap \
    --quiet \
    --command="$remote_command"
