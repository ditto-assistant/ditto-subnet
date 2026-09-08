#!/usr/bin/env bash
set -euo pipefail
image="${1:?supply the Rust supervisor fixture image}"
for scenario in pass visible wrong panic hang fake-report fake-api compile-fail compiler-private-read wrong-api input-mismatch authority-mismatch count-mismatch unsupported report-symlink threads; do
  docker run --rm --network none --read-only --ipc none \
    --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add KILL --cap-add SETUID --cap-add SETGID \
    --security-opt no-new-privileges --pids-limit 256 --memory 1g --memory-swap 1g --cpus 2 \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=384m \
    --tmpfs /out:rw,noexec,nosuid,nodev,size=128m,uid=10001,gid=10001,mode=0700 \
    --tmpfs /workspace:rw,noexec,nosuid,nodev,size=8m,mode=0700 \
    --tmpfs /run/dittobench-grader:rw,noexec,nosuid,nodev,size=1m,mode=0700 \
    --tmpfs /run/dittobench-control:rw,noexec,nosuid,nodev,size=1m,mode=0700 \
    --entrypoint /usr/bin/python3 "$image" -I /opt/probe.py "$scenario"
done
