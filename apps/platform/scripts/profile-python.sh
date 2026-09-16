#!/usr/bin/env bash
# Record a bounded py-spy profile of a Platform process: a pm2 app, or
# ditto-api's unit when it runs under the dedicated identity. py-spy needs
# ptrace/root on the deployed VM; PM2 state belongs to the deploy user.

set -euo pipefail

usage() {
  cat <<'EOF'
usage: profile-python.sh [--app NAME] [--seconds 1..300] [--output /tmp/FILE]

Defaults: app=ditto-api, seconds=30, output=/tmp/<app>-<UTC>.speedscope.json
EOF
}

app="ditto-api"
seconds=30
output=""
owner="${DITTO_PLATFORM_OWNER:-deploy}"

while (($#)); do
  case "$1" in
    --app)
      app="${2:-}"
      shift 2
      ;;
    --seconds)
      seconds="${2:-}"
      shift 2
      ;;
    --output)
      output="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

[[ "$app" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "ERROR: unsafe app name" >&2; exit 2; }
[[ "$owner" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "ERROR: unsafe owner name" >&2; exit 2; }
[[ "$seconds" =~ ^[0-9]+$ ]] && ((seconds >= 1 && seconds <= 300)) || {
  echo "ERROR: --seconds must be between 1 and 300" >&2
  exit 2
}

# With the dedicated identity, ditto-api is ditto-platform-api.service, not a pm2
# app (DITTO_PLATFORM_API_SUPERVISOR=systemd in the Platform .env).
supervisor="${DITTO_PLATFORM_API_SUPERVISOR:-$(sed -n 's|^DITTO_PLATFORM_API_SUPERVISOR=||p' \
  "$(dirname "$0")/../.env" 2>/dev/null | tail -n 1)}"
if [[ "$app" == "ditto-api" && "${supervisor:-pm2}" == "systemd" ]]; then
  pid="$(systemctl show --property=MainPID --value ditto-platform-api.service)"
elif [[ "$(id -un)" == "$owner" ]]; then
  pid="$(pm2 pid "$app")"
else
  pid="$(sudo -u "$owner" -H pm2 pid "$app")"
fi
[[ "$pid" =~ ^[1-9][0-9]*$ ]] || {
  echo "ERROR: '$app' is not running (pid='$pid')" >&2
  exit 1
}

if [[ -z "$output" ]]; then
  output="/tmp/${app}-$(date -u +%Y%m%dT%H%M%SZ).speedscope.json"
fi
if [[ -e "$output" ]]; then
  echo "ERROR: refusing to overwrite existing output: $output" >&2
  exit 1
fi

spy=(/usr/local/bin/py-spy record
  --pid "$pid"
  --duration "$seconds"
  --format speedscope
  --output "$output")
if ((EUID == 0)); then
  "${spy[@]}"
else
  sudo "${spy[@]}"
fi

echo "$output"
