#!/usr/bin/env bash
# Bring up one independent SN118 validator worker.
#
# Usage:
#   cp .env.example .env                   # then fill it in
#   ./scripts/run-validator.sh [env-file]  # default: .env
#
# The worker is stateless: run it under a supervisor (systemd/pm2) with
# restart-on-exit. Run exactly ONE instance per hotkey (two double-submit
# weights); independent validators each use a DISTINCT registered hotkey.
# Keep secrets (mnemonic/wallet, Pylon token) out of git.
set -euo pipefail

ENV_FILE="${1:-.env}"
if [ ! -f "$ENV_FILE" ]; then
  echo "error: env file '$ENV_FILE' not found." >&2
  echo "copy .env.example to '$ENV_FILE' and fill it in." >&2
  exit 1
fi

# Export every assignment in the env file into the process environment.
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

: "${VALIDATOR_HOTKEY:?set VALIDATOR_HOTKEY in $ENV_FILE}"
: "${VALIDATOR_PLATFORM_API_URL:?set VALIDATOR_PLATFORM_API_URL in $ENV_FILE}"

echo "starting validator hotkey=$VALIDATOR_HOTKEY netuid=${NETUID:-118} platform=$VALIDATOR_PLATFORM_API_URL"
exec uv run python -m ditto.validator
