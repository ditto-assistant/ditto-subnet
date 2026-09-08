#!/usr/bin/env bash
set -euo pipefail
image="${1:?supply the public Rust bridge fixture image}"
docker run --rm --network none --read-only --ipc none \
  --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add KILL --cap-add SETUID --cap-add SETGID \
  --security-opt no-new-privileges --pids-limit 256 --memory 1g --memory-swap 1g --cpus 2 \
  --tmpfs /scratch:rw,noexec,nosuid,nodev,size=512m \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=128m \
  --tmpfs /out:rw,noexec,nosuid,nodev,size=128m,uid=10001,gid=10001,mode=0700 \
  --tmpfs /run/dittobench-grader:rw,noexec,nosuid,nodev,size=1m,mode=0700 \
  -e SYNTHETIC_SECRET=must-not-reach-candidate \
  "$image"
