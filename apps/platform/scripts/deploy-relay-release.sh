#!/usr/bin/env bash
# Install an immutable relay wheel release and roll the two PM2 slots one at a
# time. The ordinary Platform deploy owns migrations; this starts only after
# that workflow job has succeeded on this exact source commit.

set -euo pipefail

artifact_dir="${1:?usage: deploy-relay-release.sh <artifact-dir> <source-commit>}"
source_commit="${2:?usage: deploy-relay-release.sh <artifact-dir> <source-commit>}"
[[ "$source_commit" =~ ^[0-9a-f]{40}$ ]] || {
  echo "ERROR: source commit must be a full lowercase SHA" >&2
  exit 1
}

artifact_dir="$(cd "$artifact_dir" && pwd)"
state_root="${DITTO_RELAY_STATE_ROOT:-/opt/ditto-platform-relay}"
release_root="$state_root/releases"
release_dir="$release_root/$source_commit"
# Ansible owns the base env in the monorepo checkout; the Platform deployment
# owns its sibling override. /opt/ditto-platform is the retired pre-cutover root.
platform_env="${DITTO_RELAY_PLATFORM_ENV:-/opt/ditto-subnet/apps/platform/.env}"
deploy_env="${DITTO_RELAY_DEPLOY_ENV:-/opt/ditto-subnet/apps/platform/.env.deploy}"
canary_port="${DITTO_RELAY_CANARY_PORT:-8020}"
start_timeout="${DITTO_RELAY_START_TIMEOUT_SECONDS:-120}"

[ -r "$artifact_dir/source-commit" ] || { echo "ERROR: artifact has no source-commit" >&2; exit 1; }
[ "$(tr -d '[:space:]' < "$artifact_dir/source-commit")" = "$source_commit" ] || {
  echo "ERROR: artifact commit does not match $source_commit" >&2
  exit 1
}
[ -r "$artifact_dir/requirements.lock" ] || { echo "ERROR: artifact has no requirements.lock" >&2; exit 1; }
[ -r "$artifact_dir/ecosystem.config.js" ] || { echo "ERROR: artifact has no ecosystem config" >&2; exit 1; }
[ -r "$platform_env" ] || { echo "ERROR: missing $platform_env" >&2; exit 1; }
if grep -Eq 'file:|packages/ditto-screening-protocol|ditto-screening-protocol' \
  "$artifact_dir/requirements.lock"; then
  echo "ERROR: requirements.lock contains a local shared-package reference" >&2
  exit 1
fi

shopt -s nullglob
platform_wheels=("$artifact_dir"/ditto_platform-*.whl)
protocol_wheels=("$artifact_dir"/ditto_screening_protocol-*.whl)
shopt -u nullglob
[ "${#platform_wheels[@]}" -eq 1 ] || {
  echo "ERROR: artifact must contain exactly one platform wheel" >&2
  exit 1
}
[ "${#protocol_wheels[@]}" -eq 1 ] || {
  echo "ERROR: artifact must contain exactly one screening-protocol wheel" >&2
  exit 1
}

mkdir -p "$release_root" "$state_root/logs"
exec 9>"$state_root/deploy.lock"
flock -n 9 || { echo "ERROR: another relay release is active" >&2; exit 1; }

if [ ! -d "$release_dir" ]; then
  staging="$(mktemp -d "$release_root/.${source_commit}.XXXXXX")"
  cleanup_staging() { rm -rf -- "$staging"; }
  trap cleanup_staging EXIT
  uv venv --python 3.12 "$staging/.venv"
  uv pip install --python "$staging/.venv/bin/python" \
    --requirement "$artifact_dir/requirements.lock"
  uv pip install --python "$staging/.venv/bin/python" --no-deps \
    "${protocol_wheels[0]}"
  uv pip install --python "$staging/.venv/bin/python" --no-deps \
    "${platform_wheels[0]}"
  mkdir -p "$staging/scripts" "$staging/logs"
  cp "$artifact_dir/ecosystem.config.js" "$staging/scripts/ecosystem.config.js"
  mv "$staging" "$release_dir"
  trap - EXIT
fi

set -a
# shellcheck disable=SC1090
. "$platform_env"
if [ -r "$deploy_env" ]; then
  # shellcheck disable=SC1090
  . "$deploy_env"
fi
set +a
export DITTO_BUILD_COMMIT="$source_commit"
export DITTO_ROLE=relay
export POSTGRES_POOL_MIN_SIZE=5
export POSTGRES_POOL_MAX_SIZE=12
export DITTO_TAOSTATS_VALIDATOR_NAMES_URL=
export DITTO_TAOSTATS_API_KEY=

json_commit() {
  python3 -c 'import json,sys; print(json.load(sys.stdin).get("commit", ""))' 2>/dev/null
}

wait_for_release() {
  local port="$1" name="$2" response served deadline
  deadline=$((SECONDS + start_timeout))
  while [ "$SECONDS" -lt "$deadline" ]; do
    response="$(curl -fsS --max-time 5 "http://127.0.0.1:$port/health" 2>/dev/null || true)"
    if [ -n "$response" ]; then
      served="$(printf '%s' "$response" | json_commit || true)"
      if [ "$served" = "$source_commit" ]; then
        echo "    $name healthy on $port at $source_commit"
        return 0
      fi
    fi
    sleep 2
  done
  echo "ERROR: $name did not serve $source_commit on port $port" >&2
  return 1
}

health_commit() {
  local port="$1"
  curl -fsS --max-time 5 "http://127.0.0.1:$port/health" 2>/dev/null \
    | json_commit || true
}

wait_for_any_health() {
  local port="$1" name="$2" served deadline
  deadline=$((SECONDS + start_timeout))
  while [ "$SECONDS" -lt "$deadline" ]; do
    served="$(health_commit "$port")"
    if [ -n "$served" ]; then
      echo "    $name restored on $port at $served" >&2
      return 0
    fi
    sleep 2
  done
  echo "ERROR: $name did not recover on port $port" >&2
  return 1
}

# Prove the artifact can import, connect, and answer before either serving slot
# is touched. The canary is never behind Caddy.
canary_log="$state_root/logs/canary-$source_commit.log"
"$release_dir/.venv/bin/python" -m ditto.api_server --port "$canary_port" \
  >"$canary_log" 2>&1 &
canary_pid=$!
stop_canary() {
  kill -TERM "$canary_pid" >/dev/null 2>&1 || true
  wait "$canary_pid" >/dev/null 2>&1 || true
}
trap stop_canary EXIT
if ! wait_for_release "$canary_port" relay-canary; then
  tail -n 80 "$canary_log" >&2 || true
  exit 1
fi
stop_canary
trap - EXIT

roll_slot() {
  local name="$1" port="$2" sibling_port="$3"
  local old_state old_cwd old_commit rollback_commit sibling_commit
  sibling_commit="$(health_commit "$sibling_port")"
  [ -n "$sibling_commit" ] || {
    echo "ERROR: refusing to roll $name while its sibling is unhealthy" >&2
    return 1
  }
  old_state="$(pm2 jlist 2>/dev/null | python3 -c '
import json, sys
name = sys.argv[1]
for app in json.load(sys.stdin):
    if app.get("name") == name:
        env = app.get("pm2_env") or {}
        print(env.get("pm_cwd", ""))
        break
' "$name" 2>/dev/null || true)"
  old_cwd="${old_state%%$'\n'*}"
  old_commit="$(health_commit "$port")"

  restore_slot() {
    [ -n "$old_cwd" ] && [ -n "$old_commit" ] || return 1
    echo "==> restoring $name from $old_cwd" >&2
    pm2 delete "$name" >/dev/null 2>&1 || true
    rollback_commit=""
    case "$old_cwd" in
      "$release_root"/*) rollback_commit="$old_commit" ;;
    esac
    DITTO_BUILD_COMMIT="$rollback_commit" pm2 start \
      "$old_cwd/scripts/ecosystem.config.js" --only "$name" --update-env >/dev/null
    wait_for_any_health "$port" "$name"
  }

  echo "==> rolling $name (the sibling remains in service)"
  pm2 delete "$name" >/dev/null 2>&1 || true
  if ! pm2 start "$release_dir/scripts/ecosystem.config.js" \
      --only "$name" --update-env >/dev/null; then
    echo "ERROR: PM2 could not start $name; sibling remains available" >&2
    restore_slot || true
    return 1
  fi
  if ! wait_for_release "$port" "$name"; then
    pm2 logs "$name" --lines 80 --nostream >&2 || true
    restore_slot || true
    return 1
  fi
  printf '%s\n' "$source_commit" > "$state_root/$name.commit"
}

previous_commit="$(health_commit 8010)"
[ "$previous_commit" = "$(health_commit 8011)" ] || previous_commit=""

roll_slot ditto-api-relay-1 8010 8011
roll_slot ditto-api-relay-2 8011 8010
pm2 save >/dev/null

# Keep the active release and one wheel-based rollback release. Names are
# validated full SHAs and children are resolved under the script-owned root
# before deletion, so this can never widen into /opt or a user directory.
python3 - "$release_root" "$source_commit" "$previous_commit" <<'PY'
from pathlib import Path
import re
import shutil
import sys

root = Path(sys.argv[1]).resolve()
keep = {sys.argv[2], sys.argv[3]}
for child in root.iterdir():
    if (
        child.is_dir()
        and re.fullmatch(r"[0-9a-f]{40}", child.name)
        and child.name not in keep
        and child.resolve().parent == root
    ):
        shutil.rmtree(child)
PY

echo "relay-release-commit=$source_commit"
