#!/usr/bin/env bash
# Bounded IAP disk inventory for a Platform app VM. Read-only: df, du of known
# buckets, docker/journal/git summaries. Does not print .env or grow anything.
set -euo pipefail

readonly PROJECT="${DITTO_GCP_PROJECT:-ditto-app-dev}"
readonly ZONE="${DITTO_GCP_ZONE:-us-central1-a}"
readonly DEFAULT_INSTANCE="ditto-platform-prod"

usage() {
    cat >&2 <<'EOF'
Usage: inspect_platform_disk.sh [instance]

Examples:
  inspect_platform_disk.sh
  inspect_platform_disk.sh ditto-platform-dev
EOF
}

if [[ $# -gt 1 ]]; then
    usage
    exit 2
fi
if [[ $# -eq 1 && ( "$1" == "-h" || "$1" == "--help" ) ]]; then
    usage
    exit 0
fi

instance="${1:-$DEFAULT_INSTANCE}"
if [[ ! "$instance" =~ ^ditto-platform-(prod|dev)$ ]]; then
    echo "error: instance must be ditto-platform-prod or ditto-platform-dev" >&2
    exit 2
fi

command -v gcloud >/dev/null 2>&1 || {
    echo "error: gcloud is required" >&2
    exit 127
}

# Specific paths only. Do not `du /` — that hangs on a full 30G boot disk.
remote_command=$(
    cat <<'REMOTE'
set -euo pipefail
echo "=== df -hT / ==="
df -hT /
echo
echo "=== df -i / ==="
df -i /
echo
echo "=== known buckets ==="
for p in \
    /home/deploy/.cache \
    /home/deploy/.cache/uv \
    /home/deploy/.npm \
    /var/cache/apt \
    /var/log/journal \
    /tmp \
    /var/tmp \
    /opt/ditto-subnet/.git \
    /opt/ditto-subnet/apps/platform \
    /opt/ditto-subnet/apps/platform/.venv \
    /opt/ditto-subnet/apps/platform/logs \
    /opt/ditto-platform \
    /opt/ditto-platform/.venv \
    /opt/ditto-platform/logs \
    /opt/ditto-platform-relay \
    /opt/ditto-platform-relay/traces \
    /opt/ditto-platform-relay/traces/ready \
    /opt/ditto-platform-relay/traces/open \
    /opt/ditto-platform-relay/releases \
    /var/lib/docker
do
    if [ -e "$p" ]; then
        sudo -n du -sh "$p" 2>/dev/null || du -sh "$p" 2>/dev/null || true
    else
        echo "missing $p"
    fi
done
echo
echo "=== largest files under live platform logs ==="
if [ -d /opt/ditto-subnet/apps/platform/logs ]; then
    sudo -n find /opt/ditto-subnet/apps/platform/logs -type f -printf '%s %p\n' 2>/dev/null \
        | sort -n | tail -8 \
        | awk '{printf "%8.1fM  %s\n", $1/1048576, $2}'
fi
echo
echo "=== journal ==="
sudo -n journalctl --disk-usage 2>/dev/null || true
echo
echo "=== docker ==="
if command -v docker >/dev/null; then
    sudo -n docker system df 2>/dev/null || echo "docker present but df failed"
else
    echo "no docker"
fi
echo
echo "=== live git objects ==="
if [ -d /opt/ditto-subnet/.git ]; then
    sudo -n -u deploy env HOME=/home/deploy git -C /opt/ditto-subnet count-objects -vH 2>/dev/null || true
fi
REMOTE
)

gcloud compute ssh "$instance" \
    --project="$PROJECT" \
    --zone="$ZONE" \
    --tunnel-through-iap \
    --quiet \
    --command="$remote_command"
