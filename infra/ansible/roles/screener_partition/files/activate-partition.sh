#!/usr/bin/env bash
set -euo pipefail
# Serialize with the existing release updater. No active job is force-killed.
exec 9>/var/lib/ditto-screener-fleet/updater/lock
flock -n 9 || { echo 'Release update/drain already active' >&2; exit 1; }
ci_state=$(virsh --connect qemu:///system domstate ditto-screener-ci-1)
[[ "$ci_state" == 'shut off' ]] || { echo 'CI guest must be drained and powered off before screening expansion' >&2; exit 1; }
workers=(ditto-screener-worker@{1..4}.service)
restore() {
  systemctl start user@1005.service
  ready=false
  for _ in {1..30}; do
    if runuser -u ditto-builder -- env DOCKER_HOST=unix:///run/ditto-screener-docker/docker.sock docker info >/dev/null 2>&1; then ready=true; break; fi
    sleep 1
  done
  "$ready" || { echo 'Rootless daemon did not become ready' >&2; return 1; }
  # docker info succeeds even when runc cannot create a delegated cgroup.
  # This trusted image is already materialized by the primary release.
  runuser -u ditto-builder -- env DOCKER_HOST=unix:///run/ditto-screener-docker/docker.sock \
    docker run --rm --pull never --network none --memory 128m --pids-limit 32 \
    --entrypoint /bin/true ditto-screener-l2-analyzer:active || return 1
  systemctl start ditto-screener-fleet-agent.service ditto-screener-worker@{1..2}.service
}
trap restore EXIT
# Workers drain first while the lane agent can still finish their build/review.
pids=()
for unit in "${workers[@]}"; do
  systemctl stop "$unit" & pids+=("$!")
done
for pid in "${pids[@]}"; do wait "$pid"; done
systemctl stop ditto-screener-fleet-agent.service
# Refuse to interrupt any independent/rootless workload or orphan sandbox.
primary_vms=$(virsh --connect qemu:///system list --name)
if grep -Eq '^ditto-(build|smoke)-' <<<"$primary_vms"; then
  echo 'Primary VM remains active after drain' >&2; exit 1
fi
reviews=$(docker ps --filter name=ditto-source- --format '{{.ID}}')
if [ -n "$reviews" ]; then
  echo 'Primary source review remains active after drain' >&2; exit 1
fi
rootless=$(runuser -u ditto-builder -- env DOCKER_HOST=unix:///run/ditto-screener-docker/docker.sock docker ps --format '{{.ID}}')
if [ -n "$rootless" ]; then
  echo 'Rootless workload remains active after drain' >&2; exit 1
fi
systemctl stop user@1005.service
systemctl disable ditto-screener-worker@{3..4}.service
systemctl enable ditto-screener-worker@{1..2}.service
systemctl start dittoscreener.slice
restore
trap - EXIT
# Verify every service and descendant subtree enters the aggregate partition.
path=$(systemctl show user@1005.service -p ControlGroup --value)
[[ "$path" == /user.slice/user-1005.slice/user@1005.service ]] || { echo 'Wrong rootless user hierarchy' >&2; exit 1; }
for unit in ditto-screener-fleet-agent.service ditto-screener-worker@{1..2}.service; do
  path=$(systemctl show "$unit" -p ControlGroup --value)
  [[ "$path" == /dittoscreener.slice/* ]] || { echo "Wrong partition for $unit" >&2; exit 1; }
done
