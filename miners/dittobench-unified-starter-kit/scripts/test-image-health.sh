#!/usr/bin/env bash
set -euo pipefail

image="${1:?usage: test-image-health.sh <image>}"
name="dittobench-unified-health-$$"

cleanup() {
  docker rm --force "$name" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run --detach --rm \
  --name "$name" \
  --publish 127.0.0.1::8080 \
  --env DITTOBENCH_PROVIDER=platform \
  --env DITTOBENCH_INFERENCE_BASE_URL=http://127.0.0.1:9 \
  "$image" >/dev/null

port="$(docker port "$name" 8080/tcp | sed -n 's/.*:\([0-9][0-9]*\)$/\1/p')"
if [[ -z "$port" ]]; then
  docker logs "$name" >&2 || true
  echo "unified image did not publish port 8080" >&2
  exit 1
fi

python3 - "$port" <<'PY'
import json
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

port = sys.argv[1]
expected_normal = {"status": "ok", "capabilities": ["case_scoped_inference_v1"]}
expected_coding = {
    "status": "ok",
    "supported_coding_contract_versions": [1],
    "capabilities": [
        "scoped_memory_seed_v1",
        "coding_runner_tools_v1",
        "case_scoped_inference_v1",
    ],
}

last_error: Exception | None = None
for _ in range(40):
    try:
        with urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
            normal = json.load(response)
        with urlopen(f"http://127.0.0.1:{port}/coding/health", timeout=1) as response:
            coding = json.load(response)
        if normal != expected_normal:
            raise AssertionError(f"unexpected normal health: {normal!r}")
        if coding != expected_coding:
            raise AssertionError(f"unexpected coding health: {coding!r}")
        break
    except (AssertionError, OSError, URLError, ValueError) as error:
        last_error = error
        time.sleep(0.25)
else:
    raise SystemExit(f"unified image did not become healthy: {last_error}")
PY
