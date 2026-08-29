#!/usr/bin/env bash
set -euo pipefail

project="ditto-app-dev"
mode="${1:-}"

usage() {
  echo "usage: $0 list | logs <ditto-screener-fleet-name> [lines] | journal <ditto-screener-fleet-name> [minutes] [lines]" >&2
  exit 64
}

if [[ "$mode" == "list" ]]; then
  gcloud compute instances list \
    --project="$project" \
    --filter='labels.env=prod AND labels.role=screener-fleet' \
    --format='table(name,zone.basename(),status,creationTimestamp,lastStartTimestamp)'
  exit 0
fi

[[ "$mode" == "logs" || "$mode" == "journal" ]] || usage
instance="${2:-}"
if [[ "$mode" == "logs" ]]; then
  minutes=15
  lines="${3:-500}"
else
  minutes="${3:-15}"
  lines="${4:-500}"
fi

[[ "$instance" =~ ^ditto-screener-fleet-[a-z0-9-]+$ ]] || usage
[[ "$minutes" =~ ^[0-9]+$ ]] && (( minutes >= 1 && minutes <= 1440 )) || usage
[[ "$lines" =~ ^[0-9]+$ ]] && (( lines >= 1 && lines <= 2000 )) || usage

zone="$({
  gcloud compute instances list \
    --project="$project" \
    --filter="name=$instance AND labels.env=prod AND labels.role=screener-fleet" \
    --format='value(zone.basename())'
} | head -n 1)"
[[ -n "$zone" ]] || {
  echo "production screener fleet instance not found: $instance" >&2
  exit 66
}

if [[ "$mode" == "logs" ]]; then
  remote_command="sudo tail -n ${lines} /opt/ditto/logs/ditto-screener.log"
else
  remote_command="sudo journalctl -u ditto-screener --since '${minutes} minutes ago' -n ${lines} --no-pager -o short-iso"
fi
gcloud compute ssh "$instance" \
  --project="$project" \
  --zone="$zone" \
  --tunnel-through-iap \
  --quiet \
  --command="$remote_command"
