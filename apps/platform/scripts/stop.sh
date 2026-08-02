#!/usr/bin/env bash
#
# Stop the Ditto Platform API process. Leaves Docker infra running; use
# `make stack-down` (or `docker compose down`) to stop postgres/minio/pylon.

set -euo pipefail
cd "$(dirname "$0")/.."

pm2 stop ditto-api || true
pm2 delete ditto-api || true
echo "ditto-api stopped. Docker infra still up (make stack-down to stop it)."
