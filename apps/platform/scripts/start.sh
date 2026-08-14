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
command -v node >/dev/null 2>&1 || { echo "ERROR: node not installed" >&2; exit 1; }

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
# ditto-api-relay-* run the Go model-relay binary, which exists only inside a
# relay release dir (/opt/ditto-platform-relay/releases/<sha>/), never in this
# checkout. Starting them from here would crash-loop both slots, so this
# script — like update.sh — starts every app EXCEPT the relay slots; relays
# are rolled exclusively by deploy-relay-release.sh.
non_relay_apps="$(node -e '
  const names = require("./scripts/ecosystem.config.js").apps
    .map((app) => app.name)
    .filter((name) => !name.startsWith("ditto-api-relay-"));
  if (names.length === 0) {
    console.error("ERROR: no non-relay apps found in ecosystem.config.js");
    process.exit(1);
  }
  process.stdout.write(names.join(","));
')"
pm2 start scripts/ecosystem.config.js --only "$non_relay_apps" --update-env
pm2 save

# Say OUT LOUD which slots this script did not start, and whether they are
# actually running. After `pm2 kill` (or any boot where the pm2 dump is gone)
# both relay slots stay absent and Caddy 502s every /api/v1/inference/*
# request — a state otherwise distinguishable from success only by reading
# `pm2 ls`. This script cannot start them itself (the binary lives only in a
# release dir and the relay env is owned by deploy-relay-release.sh), so it
# names the recovery path instead of staying silent.
relay_apps="$(node -e '
  process.stdout.write(require("./scripts/ecosystem.config.js").apps
    .map((app) => app.name)
    .filter((name) => name.startsWith("ditto-api-relay-"))
    .join(" "));
')"
relay_state_root="${DITTO_RELAY_STATE_ROOT:-/opt/ditto-platform-relay}"
running_apps="$(pm2 jlist 2>/dev/null | node -e '
  let data = "";
  process.stdin.on("data", (chunk) => (data += chunk));
  process.stdin.on("end", () => {
    let apps = [];
    try { apps = JSON.parse(data); } catch {}
    process.stdout.write(
      apps
        .filter((app) => (app.pm2_env || {}).status === "online")
        .map((app) => app.name)
        .join(" "),
    );
  });
')"
relay_down=""
for relay in $relay_apps; do
  case " $running_apps " in
    *" $relay "*) ;;
    *) relay_down="$relay_down $relay" ;;
  esac
done
if [ -n "$relay_down" ]; then
  echo ""
  echo "WARNING: relay slot(s) NOT started by this script and not running:$relay_down" >&2
  echo "  The Go model-relay binary exists only in a release dir under" >&2
  echo "  $relay_state_root/releases/<sha>/ and is rolled exclusively by" >&2
  echo "  deploy-relay-release.sh. Until a relay release is (re)deployed," >&2
  echo "  Caddy will 502 every /api/v1/inference/* request." >&2
  latest_release="$(ls -1t "$relay_state_root/releases" 2>/dev/null | head -n 1 || true)"
  if [ -n "$latest_release" ]; then
    echo "  Newest installed release: $relay_state_root/releases/$latest_release" >&2
    echo "  Recover by re-dispatching the platform-deploy relay-release job for" >&2
    echo "  that commit (or a newer release)." >&2
  else
    echo "  No installed releases found under $relay_state_root/releases;" >&2
    echo "  dispatch a platform deploy with deploy_relay=true." >&2
  fi
fi

echo ""
echo "API up on http://localhost:${API_PORT:-8000}  (docs: /docs)"
echo "  pm2 logs ditto-api   # tail logs"
echo "  pm2 status           # process state"
