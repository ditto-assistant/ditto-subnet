#!/usr/bin/env bash
set -euo pipefail
image="${1:?supply the locally built Node confinement fixture image}"
for probe in probe.cjs bridge-probe.cjs; do
docker run --rm --network none --read-only --ipc none \
  --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add KILL \
  --cap-add SETUID --cap-add SETGID --security-opt no-new-privileges \
  --pids-limit 64 --memory 256m --memory-swap 256m --cpus 1 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m \
  --tmpfs /workspace:rw,noexec,nosuid,nodev,size=1m,mode=0755 \
  --tmpfs /run/dittobench-grader:rw,noexec,nosuid,nodev,size=1m,mode=0700 \
  -e SYNTHETIC_SECRET=must-not-reach-child --entrypoint /usr/local/bin/node \
  "$image" "/opt/coding-node/$probe"
done
