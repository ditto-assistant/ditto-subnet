#!/usr/bin/env bash
# down.sh — stop the Phase-1 real stack (container + host relay/terminator).
# Leaves postgres running (docker compose) unless FULL=1.
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"; P1="$ROOT/localstack/.run/phase1"
docker rm -f ds-broker >/dev/null 2>&1 || true
for s in tlsterm model-relay; do
  [ -f "$P1/$s.pid" ] && kill "$(cat "$P1/$s.pid")" 2>/dev/null || true; rm -f "$P1/$s.pid"
done
echo "stopped ds-broker, tlsterm, model-relay"
if [ "${FULL:-0}" = 1 ]; then
  ( cd "$ROOT/apps/platform" && POSTGRES_PORT=5442 docker compose --profile local down >/dev/null 2>&1 ) || true
  echo "stopped postgres"
fi
