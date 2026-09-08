#!/usr/bin/env bash
# Fixed-target online filesystem expansion after a reviewed cloud-disk grow.
# Preserves every PostgreSQL file, WAL segment, and diagnostic log.
set -euo pipefail
[[ $# == 1 && "$1" == 'GROW PLATFORM POSTGRES BOOT DISK TO 100GB' ]] || {
  echo 'exact PostgreSQL disk recovery confirmation required' >&2; exit 2;
}
readonly remote_command=$(cat <<'REMOTE'
set -euo pipefail
test "$(findmnt -n -o SOURCE /)" = /dev/sda1
test "$(findmnt -n -o FSTYPE /)" = ext4
test "$(sudo -n blockdev --getsize64 /dev/sda)" = 107374182400
sudo -n pg_lsclusters --no-header | awk '$1==17 && $2=="main" && $3==5432 && $6=="/var/lib/postgresql/17/main" {found=1} END {exit !found}'
lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS
df -h /
if ! command -v growpart >/dev/null; then
  sudo -n apt-get install -y --no-install-recommends cloud-guest-utils
fi
if plan=$(sudo -n growpart -N /dev/sda 1 2>&1); then
  printf '%s\n' "$plan"
  sudo -n growpart /dev/sda 1
else
  printf '%s\n' "$plan"
  [[ "$plan" == *NOCHANGE* ]]
  test "$(sudo -n blockdev --getsize64 /dev/sda1)" -gt 100000000000
fi
sudo -n resize2fs /dev/sda1
test "$(df -B1 --output=avail / | tail -1 | tr -d ' ')" -gt 10737418240
df -h /
for attempt in 1 2 3 4 5; do
  if pg_isready -h 127.0.0.1 -p 5432 -t 5; then exit 0; fi
  sleep 2
done
# Only after capacity is restored and readiness is still failing. A ready
# cluster never enters this branch; restart uses normal PostgreSQL WAL recovery.
sudo -n pg_ctlcluster --mode fast 17 main restart
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
  if pg_isready -h 127.0.0.1 -p 5432 -t 5; then exit 0; fi
  sleep 5
done
echo 'PostgreSQL still unavailable after capacity recovery; no further mutation attempted' >&2
exit 1
REMOTE
)
exec gcloud compute ssh ditto-pg-platform --project=ditto-app-dev --zone=us-central1-a --tunnel-through-iap --quiet --command="$remote_command"
