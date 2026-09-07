#!/usr/bin/env bash
set -euo pipefail
image="${1:?supply the locally built synthetic test image}"
for scenario in pass visible stdout bytes wrong early-exit fake-report oversized hang hidden-read report-write fork threads exec setsid setuid capabilities environment tuple tuple-value package raises raises-subclass raises-return raises-exit raises-import unsupported count-mismatch; do
  docker run --rm --network none --read-only --ipc none \
    --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add KILL --cap-add SETUID --cap-add SETGID \
    --security-opt no-new-privileges --pids-limit 64 --memory 256m --memory-swap 256m --cpus 1 \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m \
    --tmpfs /workspace:rw,noexec,nosuid,nodev,size=1m,mode=0755 \
    --tmpfs /run/dittobench-grader:rw,noexec,nosuid,nodev,size=1m,mode=0700 \
    --tmpfs /run/dittobench-control:rw,noexec,nosuid,nodev,size=1m,mode=0700 \
    -e SYNTHETIC_SECRET=must-not-reach-child \
    --entrypoint /usr/local/bin/python3 "$image" -I /opt/probe.py "$scenario"
done
