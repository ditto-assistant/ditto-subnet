#!/usr/bin/env bash

# Exit successfully when a newline-delimited path list contains a change that
# can alter the relay runtime. Tests live inside the ditto package tree, so a
# broad ditto/** match would unnecessarily roll critical relay infrastructure
# for test- or dashboard-only releases.
set -euo pipefail

while IFS= read -r path || [[ -n "$path" ]]; do
  case "$path" in
    ditto/tests/*)
      continue
      ;;
    ditto/* | alembic/* | pyproject.toml | uv.lock | scripts/ecosystem.config.js | scripts/build-relay-release.sh | scripts/deploy-relay-release.sh | .github/workflows/deploy.yml)
      exit 0
      ;;
  esac
done

exit 1
