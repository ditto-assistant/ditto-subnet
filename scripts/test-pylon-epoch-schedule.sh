#!/usr/bin/env bash
set -euo pipefail
image="${1:?supply the exact locally built or released Pylon image}"
repo_root="$(git rev-parse --show-toplevel)"
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --pids-limit 64 --memory 1g \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
  --env 'PYLON_IDENTITIES=[]' \
  --mount "type=bind,src=$repo_root/services/pylon/test_image.py,dst=/tmp/verify.py,readonly" \
  --entrypoint /app/pylon_service/.venv/bin/python "$image" -B /tmp/verify.py

# New receipt API uses the same exact image and an isolated writable /tmp DB.
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --pids-limit 64 --memory 1g \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
  --env 'PYLON_IDENTITIES=[]' \
  --mount "type=bind,src=$repo_root/services/pylon/test_receipt_image.py,dst=/tmp/receipt-tests.py,readonly" \
  --mount "type=bind,src=$repo_root/packages/ditto-screening-protocol/ditto_screening_protocol/weight_receipt.py,dst=/tmp/weight_receipt_contract.py,readonly" \
  --entrypoint /app/pylon_service/.venv/bin/python "$image" -B /tmp/receipt-tests.py
