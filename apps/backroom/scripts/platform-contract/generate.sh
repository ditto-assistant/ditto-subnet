#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKROOM_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MONOREPO_DIR="$(cd "${BACKROOM_DIR}/../.." && pwd)"
PLATFORM_DIR="${PLATFORM_DIR:-${MONOREPO_DIR}/apps/platform}"
PLATFORM_SHA="${PLATFORM_SHA:-$(git -C "${MONOREPO_DIR}" rev-parse HEAD)}"

mkdir -p src/generated .contract-sync

(
  cd "$PLATFORM_DIR"
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
  --arg sha "$PLATFORM_SHA" \
  --arg path "apps/platform" \
  '{repository: $repository, path: $path, sha: $sha}' \
  > src/generated/platform-api.meta.json
