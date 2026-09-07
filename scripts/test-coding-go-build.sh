#!/usr/bin/env bash
set -euo pipefail
image="${1:?supply the public Go build/launch fixture image}"
docker run --rm --network none --read-only --ipc none \
  --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add KILL --cap-add SETUID --cap-add SETGID \
  --security-opt no-new-privileges --pids-limit 256 --memory 1g --memory-swap 1g --cpus 2 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=512m \
  --tmpfs /run/dittobench-grader:rw,noexec,nosuid,nodev,size=1m,mode=0700 \
  --tmpfs /run/dittobench-control:rw,noexec,nosuid,nodev,size=1m,mode=0700 \
  "$image"
