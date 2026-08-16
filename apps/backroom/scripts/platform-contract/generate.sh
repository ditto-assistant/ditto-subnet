#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKROOM_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MONOREPO_DIR="$(cd "${BACKROOM_DIR}/../.." && pwd)"
PLATFORM_DIR="${PLATFORM_DIR:-${MONOREPO_DIR}/apps/platform}"

mkdir -p src/generated .contract-sync

(
  cd "$PLATFORM_DIR"
  # ditto-screening-protocol is a path dependency installed as a BUILT copy, so
  # plain `uv sync` reports "Audited" and leaves the previously-built copy in
  # place after its source changes. Dumping the schema against that stale copy
  # does not fail -- it emits a schema for the older models, so the regenerated
  # client silently LOSES whatever the package gained. That is how a v11
  # rollout regenerated `bench_version: 9 | 10` back down to `9` in a diff that
  # otherwise read as a clean additive change. Deployment already reinstalls for
  # this same reason (apps/platform/scripts/update.sh, workers/screener/scripts).
  uv sync --reinstall-package ditto-screening-protocol >/dev/null
  uv run python - <<'PY' > "${OLDPWD}/.contract-sync/platform-openapi.json"
import json

from ditto.api_server import create_api_server
from ditto.tests.api_server.conftest import make_api_server_config

print(json.dumps(create_api_server(make_api_server_config()).openapi(), sort_keys=True))
PY
)

pnpm exec openapi-typescript .contract-sync/platform-openapi.json \
  --output src/generated/platform-api.ts

jq -n \
  --arg repository "ditto-assistant/ditto-subnet" \
  --arg path "apps/platform" \
  '{repository: $repository, path: $path, contract: "same-monorepo-release"}' \
  > src/generated/platform-api.meta.json
