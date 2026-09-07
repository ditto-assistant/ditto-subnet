#!/usr/bin/env bash
set -euo pipefail
image="${1:?supply the locally built synthetic Node test image}"
for scenario in pass visible bytes commonjs esm javascript cts mts parent-env wrong stdout fake-report early-exit hang oversized wrong-nonce hidden-read report-write spawn signal setuid capabilities environment worker-hang nonfinite opaque unsupported count-mismatch relative-suite typed-inputs expected-rejection expected-throw rejection-fulfilled rejection-sync-throw rejection-import-fail rejection-exit callback-cache callback-sync callback-double callback-forgery callback-identity; do
  docker run --rm --network none --read-only --ipc none \
    --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add KILL --cap-add SETUID --cap-add SETGID \
    --security-opt no-new-privileges --pids-limit 64 --memory 256m --memory-swap 256m --cpus 1 \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m \
    --tmpfs /workspace:rw,noexec,nosuid,nodev,size=1m,mode=0755 \
    --tmpfs /run/dittobench-grader:rw,noexec,nosuid,nodev,size=1m,mode=0700 \
    --tmpfs /run/dittobench-control:rw,noexec,nosuid,nodev,size=1m,mode=0700 \
    -e SYNTHETIC_SECRET=must-not-reach-child \
    --entrypoint /usr/local/bin/node "$image" /opt/probe.cjs "$scenario"
done
