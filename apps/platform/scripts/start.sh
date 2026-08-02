#!/usr/bin/env bash
#
# Start the Ditto Platform API on a host:
#   1. load the Ansible-owned .env and optional deploy-owned .env.deploy
#   2. sync dependencies
#   3. bring up the Docker infra it needs and wait until healthy
#   4. apply database migrations
#   5. start the API under pm2 (logs -> ./logs, autorestart on)
#
# Docker services are env-driven via DITTO_COMPOSE_SERVICES (default: the full
# local stack "postgres minio pylon"). A deployed host sets it to "pylon" in its
# .env — there Postgres is the dedicated PG VM and object storage is GCS, so only
# the Pylon sidecar runs locally.
#
# Idempotent: safe to re-run. Use scripts/update.sh to deploy a new revision
# (that reload is a stop/start, not zero-downtime -- see ecosystem.config.js).

set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "ERROR: .env not found. Run: cp .env.example .env  (then fill it in)" >&2
  exit 1
fi

command -v uv  >/dev/null 2>&1 || { echo "ERROR: uv not installed"  >&2; exit 1; }
command -v pm2 >/dev/null 2>&1 || { echo "ERROR: pm2 not installed (npm i -g pm2)" >&2; exit 1; }

# Export the base environment, then let deploy-owned runtime values override it.
set -a
. ./.env
if [ -f .env.deploy ]; then
  . ./.env.deploy
fi
set +a

echo "==> syncing dependencies"
uv sync

# Which compose services to run. Named explicitly so profiled (local-only)
# services still start when requested; a deployed host narrows this to "pylon".
compose_services="${DITTO_COMPOSE_SERVICES:-postgres minio pylon}"
echo "==> bringing up infra ($compose_services)"
# shellcheck disable=SC2086
docker compose up -d --wait $compose_services
if printf '%s' " $compose_services " | grep -q ' minio '; then
  docker compose up -d minio-create-bucket
fi

echo "==> applying migrations"
uv run alembic upgrade head

mkdir -p logs

echo "==> starting API under pm2"
pm2 start scripts/ecosystem.config.js --update-env
pm2 save

echo ""
echo "API up on http://localhost:${API_PORT:-8000}  (docs: /docs)"
echo "  pm2 logs ditto-api   # tail logs"
echo "  pm2 status           # process state"
