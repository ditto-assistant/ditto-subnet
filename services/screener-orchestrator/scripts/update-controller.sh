#!/usr/bin/env bash
set -euo pipefail

# Exact-commit, rollback-capable deployment for the capacity controller and its
# sibling trusted builder. Provider credentials remain in the Ansible-owned
# mode-0600 files; this updater never reads or rewrites them.

CONTROLLER_ROOT="${SCREENER_CONTROLLER_ROOT:-/opt/ditto-subnet}"
CONTROLLER_USER="${SCREENER_CONTROLLER_USER:-deploy}"
CONTROLLER_GROUP="${SCREENER_CONTROLLER_GROUP:-ditto}"
CONTROLLER_EXPECTED_SHA="${SCREENER_CONTROLLER_EXPECTED_SHA:?missing SCREENER_CONTROLLER_EXPECTED_SHA}"
CONTROLLER_UV_BIN="${SCREENER_CONTROLLER_UV_BIN:-/usr/local/bin/uv}"
CONTROLLER_UNIT="${SCREENER_CONTROLLER_UNIT:-ditto-screener-capacity}"
BUILDER_UNIT="${SCREENER_BUILDER_UNIT:-ditto-image-builder}"
CONTROLLER_HEALTH_URL="${SCREENER_CONTROLLER_HEALTH_URL:-https://platform-api.heyditto.ai/api/v1/public/screener-capacity-watchdog?environment=prod}"

service_dir="$CONTROLLER_ROOT/services/screener-orchestrator"
venv="$service_dir/.venv"
state_dir="/var/lib/ditto-screener-capacity"
deployed_marker="$state_dir/deployed-sha"
lock_file="/run/lock/ditto-screener-capacity-deploy.lock"

if [[ "$EUID" -ne 0 ]]; then
  echo "update-controller.sh must run as root" >&2
  exit 1
fi
if [[ ! "$CONTROLLER_EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "expected SHA must be a full lowercase commit SHA" >&2
  exit 2
fi
for path in "$CONTROLLER_ROOT/.git" "$CONTROLLER_UV_BIN"; do
  if [[ ! -e "$path" ]]; then
    echo "required controller deployment path is missing: $path" >&2
    exit 1
  fi
done

exec {lock_fd}>"$lock_file"
if ! flock -w 1200 "$lock_fd"; then
  echo "could not acquire controller deploy lock within 20m" >&2
  exit 1
fi

as_deploy() {
  runuser -u "$CONTROLLER_USER" -- "$@"
}

health_epoch() {
  curl --fail --silent --show-error --max-time 10 "$CONTROLLER_HEALTH_URL" \
    | jq -er '.controller_epoch // empty'
}

verify_services() {
  local revision="$1"
  local prior_epoch="${2:-}"
  local consecutive=0 capacity_pid builder_pid health
  # A clean restart may need to wait for the previous 180-second Platform
  # writer lease to expire before its new epoch can acquire the fence.
  for _ in $(seq 1 150); do
    if systemctl is-active --quiet "$CONTROLLER_UNIT" \
      && systemctl is-active --quiet "$BUILDER_UNIT"; then
      capacity_pid="$(systemctl show --property MainPID --value "$CONTROLLER_UNIT")"
      builder_pid="$(systemctl show --property MainPID --value "$BUILDER_UNIT")"
      if [[ "$capacity_pid" =~ ^[1-9][0-9]*$ && "$builder_pid" =~ ^[1-9][0-9]*$ ]]; then
        health="$(curl --fail --silent --show-error --max-time 10 \
          "$CONTROLLER_HEALTH_URL" 2>/dev/null || true)"
        # Provider readiness controls operational fallback, not whether this
        # exact controller revision can be deployed or rolled back safely.
        if jq -e \
          --arg revision "$revision" \
          --arg prior_epoch "$prior_epoch" \
          '.controller_stale == false
            and .controller_source_sha == $revision
            and (.controller_epoch | type == "string" and length > 0)
            and ($prior_epoch == "" or .controller_epoch != $prior_epoch)' \
          >/dev/null 2>&1 <<<"$health"; then
          consecutive=$((consecutive + 1))
          if [[ "$consecutive" -ge 3 ]]; then
            return 0
          fi
        else
          consecutive=0
        fi
      else
        consecutive=0
      fi
    else
      consecutive=0
    fi
    sleep 2
  done
  systemctl --no-pager --full status "$CONTROLLER_UNIT" "$BUILDER_UNIT" >&2 || true
  return 1
}

activate_revision() {
  local revision="$1"
  local prior_epoch="${2:-}"
  systemctl stop "$BUILDER_UNIT" "$CONTROLLER_UNIT" || true
  as_deploy git -C "$CONTROLLER_ROOT" reset --hard "$revision" || return 1
  as_deploy env UV_PROJECT_ENVIRONMENT="$venv" \
    "$CONTROLLER_UV_BIN" sync --project "$service_dir" --frozen || return 1
  systemctl start "$CONTROLLER_UNIT" || return 1
  systemctl start "$BUILDER_UNIT" || return 1
  verify_services "$revision" "$prior_epoch" || return 1
  test "$(as_deploy git -C "$CONTROLLER_ROOT" rev-parse HEAD)" = "$revision" \
    || return 1
}

install -d -o "$CONTROLLER_USER" -g "$CONTROLLER_GROUP" -m 0700 "$state_dir"
as_deploy git -C "$CONTROLLER_ROOT" fetch --force --prune origin \
  refs/heads/main:refs/remotes/origin/main
as_deploy git -C "$CONTROLLER_ROOT" cat-file -e "$CONTROLLER_EXPECTED_SHA^{commit}"
if ! as_deploy git -C "$CONTROLLER_ROOT" merge-base --is-ancestor \
  "$CONTROLLER_EXPECTED_SHA" refs/remotes/origin/main; then
  echo "refusing to deploy a revision that is not on origin/main" >&2
  exit 1
fi

previous_sha="$(as_deploy git -C "$CONTROLLER_ROOT" rev-parse HEAD)"
previous_epoch="$(health_epoch 2>/dev/null || true)"
if [[ -s "$deployed_marker" ]]; then
  marker_sha="$(<"$deployed_marker")"
  if [[ "$marker_sha" =~ ^[0-9a-f]{40}$ ]] \
    && as_deploy git -C "$CONTROLLER_ROOT" cat-file -e \
      "$marker_sha^{commit}" 2>/dev/null; then
    previous_sha="$marker_sha"
  fi
fi

if [[ "$previous_sha" == "$CONTROLLER_EXPECTED_SHA" ]] \
  && verify_services "$CONTROLLER_EXPECTED_SHA"; then
  printf '%s\n' "$CONTROLLER_EXPECTED_SHA" >"$deployed_marker"
  chown "$CONTROLLER_USER:$CONTROLLER_GROUP" "$deployed_marker"
  chmod 0600 "$deployed_marker"
  echo "controller and builder already serve $CONTROLLER_EXPECTED_SHA"
  exit 0
fi

echo "deploying controller and builder at $CONTROLLER_EXPECTED_SHA"
if ! activate_revision "$CONTROLLER_EXPECTED_SHA" "$previous_epoch"; then
  echo "deployment failed; rolling back to $previous_sha" >&2
  if ! activate_revision "$previous_sha" "$previous_epoch"; then
    echo "rollback failed; both systemd unit statuses were printed above" >&2
    exit 2
  fi
  exit 1
fi

printf '%s\n' "$CONTROLLER_EXPECTED_SHA" >"$deployed_marker"
chown "$CONTROLLER_USER:$CONTROLLER_GROUP" "$deployed_marker"
chmod 0600 "$deployed_marker"
echo "controller and builder are active at $CONTROLLER_EXPECTED_SHA"
