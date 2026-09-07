#!/usr/bin/env bash
set -euo pipefail
image="${1:?supply the public compiled-bootstrap fixture image}"
docker run --rm --network none --read-only --ipc none \
  --cap-drop ALL --cap-add KILL --cap-add SETUID --cap-add SETGID \
  --security-opt no-new-privileges --pids-limit 64 --memory 256m \
  --memory-swap 256m --cpus 1 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
  --tmpfs /run/dittobench-grader:rw,noexec,nosuid,nodev,size=1m,mode=0700 \
  --tmpfs /run/dittobench-control:rw,noexec,nosuid,nodev,size=1m,mode=0700 \
  -e SYNTHETIC_SECRET=must-not-reach-child "$image"

# Without CAP_KILL the parent must reject before starting a different-UID child.
docker run --rm --network none --read-only --ipc none \
  --cap-drop ALL --cap-add SETUID --cap-add SETGID \
  --security-opt no-new-privileges --pids-limit 64 --memory 256m \
  --memory-swap 256m --cpus 1 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
  --tmpfs /run/dittobench-grader:rw,noexec,nosuid,nodev,size=1m,mode=0700 \
  --tmpfs /run/dittobench-control:rw,noexec,nosuid,nodev,size=1m,mode=0700 \
  "$image" parent-refusal
