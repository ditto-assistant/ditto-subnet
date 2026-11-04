#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."

runtime_diagnostics() {
  status=$?
  if [ "$status" -ne 0 ]; then
    # startup.sh's own trap is gone by the time this file runs, so repeat the
    # sentinel the controller watches for on the serial console.
    echo "sn118-preview: startup failed with status ${status}" >&2
    docker compose -f preview/cloud/compose.yml ps >&2 || true
    docker compose -f preview/cloud/compose.yml logs --no-color --tail=200 >&2 || true
    docker compose -f preview/cloud/compose.yml --profile oauth-fixture logs --no-color --tail=200 oauth-fixture >&2 || true
  fi
  exit "$status"
}
trap runtime_diagnostics EXIT

[[ "${PREVIEW_SHA:-}" =~ ^[0-9a-f]{40}$ ]]
case "${PREVIEW_PROFILE:-}" in stack|stack-copy) ;; *) exit 2 ;; esac
PREVIEW_PR="$(jq -r '.pr // 0' /run/preview-config.json 2>/dev/null || echo 0)"
[[ "$PREVIEW_PR" =~ ^[0-9]+$ ]] || PREVIEW_PR=0
export PREVIEW_PR

ip="$(curl --fail --silent --show-error -H 'Metadata-Flavor: Google' \
  http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip)"
export PREVIEW_BASE_DOMAIN="${ip}.sslip.io"
export PREVIEW_ADMIN_TOKEN="$(openssl rand -hex 32)"
export PREVIEW_SESSION_SECRET="$(openssl rand -hex 32)"
export PREVIEW_POSTGRES_PASSWORD="$(openssl rand -hex 24)"

if [ "$PREVIEW_PROFILE" = stack-copy ]; then
  [ -n "${PREVIEW_SNAPSHOT_URL:-}" ] || { echo "stack-copy requires a snapshot URL" >&2; exit 2; }
  install -d -m 0700 preview/state
  curl --fail --location --silent --show-error "$PREVIEW_SNAPSHOT_URL" -o preview/state/sanitized.dump
  chmod 0600 preview/state/sanitized.dump
fi

docker compose -f preview/cloud/compose.yml build
docker compose -f preview/cloud/compose.yml up -d postgres minio
docker compose -f preview/cloud/compose.yml up -d minio-init

if [ "$PREVIEW_PROFILE" = stack-copy ]; then
  until docker compose -f preview/cloud/compose.yml exec -T postgres pg_isready -U ditto -d ditto_platform_preview; do sleep 2; done
  docker compose -f preview/cloud/compose.yml exec -T postgres \
    pg_restore --clean --if-exists --no-owner --no-privileges \
      -U ditto -d ditto_platform_preview < preview/state/sanitized.dump
fi

docker compose -f preview/cloud/compose.yml up -d platform dashboard backroom preview-control gateway
if [ "$PREVIEW_PR" = 3359 ] && [ "$PREVIEW_PROFILE" = stack ]; then
  docker compose -f preview/cloud/compose.yml up -d oauth-fixture
fi
docker compose -f preview/cloud/compose.yml ps
docker compose -f preview/cloud/compose.yml --profile oauth-fixture ps oauth-fixture
trap - EXIT
