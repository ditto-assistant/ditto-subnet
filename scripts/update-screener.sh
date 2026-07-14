#!/usr/bin/env bash
set -euo pipefail

# Deterministically update the dedicated production screener checkout, restart
# the worker, and prove that both the process and its platform credentials work.
# The GitHub deploy workflow runs this as root over IAP SSH.

SCREENER_ROOT="${SCREENER_ROOT:-/opt/ditto/screener}"
SCREENER_USER="${SCREENER_USER:-deploy}"
SCREENER_UNIT="${SCREENER_UNIT:-ditto-screener}"
SCREENER_BRANCH="${SCREENER_BRANCH:-main}"
SCREENER_EXPECTED_SHA="${SCREENER_EXPECTED_SHA:-}"
SCREENER_UV_BIN="${SCREENER_UV_BIN:-/usr/local/bin/uv}"

checkout="$SCREENER_ROOT/src"
venv="$checkout/.venv"
env_file="$SCREENER_ROOT/screener.env"

if [[ "${EUID}" -ne 0 ]]; then
  echo "update-screener.sh must run as root" >&2
  exit 1
fi

for path in "$checkout/.git" "$env_file" "$SCREENER_UV_BIN"; do
  if [[ ! -e "$path" ]]; then
    echo "required screener deployment path is missing: $path" >&2
    exit 1
  fi
done

deploy_ref="$SCREENER_BRANCH"
if [[ -n "$SCREENER_EXPECTED_SHA" ]]; then
  deploy_ref="$SCREENER_EXPECTED_SHA"
fi

echo "==> fetching $deploy_ref"
runuser -u "$SCREENER_USER" -- git -C "$checkout" fetch --prune origin "$deploy_ref"
resolved_sha="$(runuser -u "$SCREENER_USER" -- git -C "$checkout" rev-parse FETCH_HEAD)"
if [[ -n "$SCREENER_EXPECTED_SHA" && "$resolved_sha" != "$SCREENER_EXPECTED_SHA" ]]; then
  echo "$deploy_ref resolved to $resolved_sha, expected $SCREENER_EXPECTED_SHA" >&2
  exit 1
fi

echo "==> checking out $resolved_sha"
runuser -u "$SCREENER_USER" -- git -C "$checkout" reset --hard "$resolved_sha"

echo "==> syncing the frozen environment"
runuser -u "$SCREENER_USER" -- env UV_PROJECT_ENVIRONMENT="$venv" \
  "$SCREENER_UV_BIN" sync --frozen --project "$checkout"

echo "==> restarting $SCREENER_UNIT"
systemctl restart "$SCREENER_UNIT"
for attempt in $(seq 1 30); do
  if systemctl is-active --quiet "$SCREENER_UNIT"; then
    break
  fi
  if [[ "$attempt" -eq 30 ]]; then
    systemctl status "$SCREENER_UNIT" --no-pager >&2 || true
    exit 1
  fi
  sleep 2
done

# The screener has no public HTTP listener. Exercise its authenticated queue
# contract directly with the same env file to catch stale or missing secrets.
# Do not source the file: the mnemonic contains spaces and is intentionally not
# shell-quoted. Read only the three single-token fields this probe needs.
env_value() {
  local key="$1"
  sed -n "s/^${key}=//p" "$env_file" | tail -n 1
}

SCREENER_PLATFORM_API_URL="$(env_value SCREENER_PLATFORM_API_URL)"
SCREENER_API_TOKEN="$(env_value SCREENER_API_TOKEN)"
SCREENER_HOTKEY="$(env_value SCREENER_HOTKEY)"
: "${SCREENER_PLATFORM_API_URL:?missing SCREENER_PLATFORM_API_URL}"
: "${SCREENER_API_TOKEN:?missing SCREENER_API_TOKEN}"
: "${SCREENER_HOTKEY:?missing SCREENER_HOTKEY}"

# Feed the bearer header over stdin so the token never appears in `ps` output.
curl --fail --silent --show-error --config - \
  "$SCREENER_PLATFORM_API_URL/api/v1/screener/queue?limit=1" >/dev/null <<CURL_CONFIG
header = "Authorization: Bearer $SCREENER_API_TOKEN"
header = "X-Screener-Hotkey: $SCREENER_HOTKEY"
CURL_CONFIG

actual_sha="$(runuser -u "$SCREENER_USER" -- git -C "$checkout" rev-parse HEAD)"
echo "healthy: $SCREENER_UNIT active at $actual_sha; platform queue auth accepted"
