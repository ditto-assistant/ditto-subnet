#!/usr/bin/env bash
#
# Stop the Ditto Platform API process. Leaves Docker infra running; use
# `make stack-down` (or `docker compose down`) to stop postgres/minio/pylon.
#
# With DITTO_PLATFORM_API_SUPERVISOR=systemd in .env (the dedicated ditto-api
# identity, infra/docs/coding-hosted-control-signer-v2.md) ditto-api belongs to
# ditto-platform-api.service, not pm2: stop and disable it through the one sudo
# command the deploy user has for that.

set -euo pipefail
cd "$(dirname "$0")/.."

supervisor="${DITTO_PLATFORM_API_SUPERVISOR:-$(sed -n 's|^DITTO_PLATFORM_API_SUPERVISOR=||p' .env 2>/dev/null | tail -n 1)}"
case "${supervisor:-pm2}" in
  pm2)
    pm2 stop ditto-api || true
    pm2 delete ditto-api || true
    echo "ditto-api stopped. Docker infra still up (make stack-down to stop it)."
    ;;
  systemd)
    sudo -n /usr/local/sbin/ditto-platform-api-release stop
    echo "ditto-platform-api.service stopped and disabled."
    ;;
  *)
    echo "ERROR: DITTO_PLATFORM_API_SUPERVISOR must be pm2 or systemd" >&2
    exit 1
    ;;
esac
