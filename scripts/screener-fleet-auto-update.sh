#!/usr/bin/env bash
# Pull, authenticate, stage, drain, and atomically activate a screener-fleet release.
set -euo pipefail

FLEET_ROOT="${SCREENER_FLEET_ROOT:-/opt/ditto/screener-fleet}"
RELEASES_DIR="${SCREENER_FLEET_RELEASES_DIR:-$FLEET_ROOT/releases}"
CURRENT_LINK="${SCREENER_FLEET_CURRENT_LINK:-$FLEET_ROOT/current}"
STATE_DIR="${SCREENER_FLEET_UPDATE_STATE_DIR:-/var/lib/ditto-screener-fleet/updater}"
CONFIG_DIR="${SCREENER_FLEET_CONFIG_DIR:-/etc/ditto-screener-fleet}"
RELEASE_ENV="${SCREENER_FLEET_RELEASE_ENV:-$CONFIG_DIR/release.env}"
REPOSITORY_URL="${SCREENER_FLEET_REPOSITORY_URL:-https://github.com/ditto-assistant/ditto-subnet.git}"
DESCRIPTOR_REPOSITORY="ghcr.io/ditto-assistant/ditto-subnet-stack"
RELEASE_CHANNEL="${SCREENER_FLEET_RELEASE_CHANNEL:-$DESCRIPTOR_REPOSITORY:screener-fleet-stable-1}"
SERVICE_USER="${SCREENER_FLEET_USER:-ditto-screener}"
SERVICE_GROUP="${SCREENER_FLEET_GROUP:-ditto-screener}"
WORKER_PROCESSES="${SCREENER_FLEET_WORKER_PROCESSES:-8}"
UV_BIN="${SCREENER_FLEET_UV_BIN:-/usr/local/bin/uv}"
SYSTEMCTL="${SCREENER_FLEET_SYSTEMCTL:-systemctl}"
EXPECTED_FORMAT_VERSION=1
EXPECTED_UPDATE_PROTOCOL=1
MANAGED_FILE="$STATE_DIR/managed-release.env"
FAILED_CANDIDATE_FILE="$STATE_DIR/failed-candidate"
LOCK_FILE="$STATE_DIR/lock"

log() { printf 'screener-fleet-auto-update: %s\n' "$*" >&2; }
die() { log "error: $*"; exit 1; }
manifest_value() {
  awk -F= -v key="$2" '$1 == key {print substr($0,index($0,"=")+1); exit}' "$1"
}
is_descriptor_digest() {
  [[ "$1" =~ ^ghcr\.io/ditto-assistant/ditto-subnet-stack@sha256:[0-9a-f]{64}$ ]]
}
is_builder_digest() {
  [[ "$1" =~ ^us-central1-docker\.pkg\.dev/ditto-app-dev/ditto-public-builders/submission-builder@sha256:[0-9a-f]{64}$ ]]
}
run_as_service() {
  setpriv --reuid="$SERVICE_USER" --regid="$SERVICE_GROUP" --init-groups -- \
    env HOME="$FLEET_ROOT" "$@"
}

validate_manifest() {
  local file="$1" line key count=0 seen='|' allowed
  allowed=' FLEET_FORMAT_VERSION FLEET_VERSION FLEET_REVISION FLEET_UPDATE_PROTOCOL SUBMISSION_BUILDER_IMAGE '
  [ -f "$file" ] && [ ! -L "$file" ] || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || continue
    [[ "$line" =~ ^([A-Z][A-Z0-9_]*)=([^[:space:]]+)$ ]] || return 1
    key="${BASH_REMATCH[1]}"
    [[ "$allowed" == *" $key "* ]] || return 1
    [[ "$seen" != *"|$key|"* ]] || return 1
    seen="${seen}${key}|"
    count=$((count + 1))
  done <"$file"
  [ "$count" -eq 5 ] || return 1
  for key in $allowed; do [[ "$seen" == *"|$key|"* ]] || return 1; done
  [ "$(manifest_value "$file" FLEET_FORMAT_VERSION)" = "$EXPECTED_FORMAT_VERSION" ] || return 1
  [[ "$(manifest_value "$file" FLEET_VERSION)" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
  [[ "$(manifest_value "$file" FLEET_REVISION)" =~ ^[0-9a-f]{40}$ ]] || return 1
  [ "$(manifest_value "$file" FLEET_UPDATE_PROTOCOL)" = "$EXPECTED_UPDATE_PROTOCOL" ] || return 1
  is_builder_digest "$(manifest_value "$file" SUBMISSION_BUILDER_IMAGE)"
}

verify_descriptor_labels() {
  local image="$1" manifest="$2" label
  label="$(docker image inspect --format '{{ index .Config.Labels "io.heyditto.screener.fleet-release" }}' "$image")"
  [ "$label" = true ] || return 1
  [ "$(docker image inspect --format '{{ index .Config.Labels "io.heyditto.screener.fleet-update-protocol" }}' "$image")" = "$EXPECTED_UPDATE_PROTOCOL" ] || return 1
  [ "$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.version" }}' "$image")" = "$(manifest_value "$manifest" FLEET_VERSION)" ] || return 1
  [ "$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image")" = "$(manifest_value "$manifest" FLEET_REVISION)" ]
}

resolve_descriptor() {
  local exact
  docker pull --platform linux/amd64 "$RELEASE_CHANNEL" >/dev/null
  exact="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$RELEASE_CHANNEL" \
    | awk -v repo="$DESCRIPTOR_REPOSITORY@" 'index($0, repo) == 1 {print; exit}')"
  is_descriptor_digest "$exact" || die "release channel did not resolve to an exact Ditto descriptor"
  printf '%s' "$exact"
}

extract_manifest() {
  local image="$1" output="$2" container
  container="$(docker create --platform linux/amd64 "$image")"
  trap 'docker rm -f "$container" >/dev/null 2>&1 || true' RETURN
  docker cp "$container:/release/manifest.env" "$output"
  docker rm "$container" >/dev/null
  trap - RETURN
}

prepare_release() {
  local revision="$1" release_dir="$2"
  if [ -e "$release_dir" ]; then
    [ -d "$release_dir/src/.git" ] || die "existing release path is invalid: $release_dir"
    [ "$(run_as_service git -C "$release_dir/src" rev-parse HEAD)" = "$revision" ] || \
      die "existing release checkout does not match $revision"
    [ -x "$release_dir/worker-venv/bin/ditto-screener" ] || \
      die "existing worker environment is incomplete"
    [ -x "$release_dir/orchestrator-venv/bin/python" ] || \
      die "existing orchestrator environment is incomplete"
    return 0
  fi
  local staging="${release_dir}.staging.$$"
  cleanup_staging() { rm -rf -- "$staging"; }
  trap cleanup_staging RETURN
  install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0750 "$staging"
  if ! run_as_service git clone --filter=blob:none --no-checkout "$REPOSITORY_URL" "$staging/src"; then
    return 1
  fi
  run_as_service git -C "$staging/src" fetch --force origin \
    refs/heads/main:refs/remotes/origin/main
  run_as_service git -C "$staging/src" cat-file -e "$revision^{commit}"
  run_as_service git -C "$staging/src" merge-base --is-ancestor \
    "$revision" refs/remotes/origin/main
  run_as_service git -C "$staging/src" checkout --detach "$revision"
  [ "$(run_as_service git -C "$staging/src" rev-parse HEAD)" = "$revision" ]
  run_as_service "$UV_BIN" venv --relocatable "$staging/worker-venv"
  run_as_service env UV_PROJECT_ENVIRONMENT="$staging/worker-venv" \
    "$UV_BIN" sync --frozen --no-editable --project "$staging/src/workers/screener"
  run_as_service "$UV_BIN" venv --relocatable "$staging/orchestrator-venv"
  run_as_service env UV_PROJECT_ENVIRONMENT="$staging/orchestrator-venv" \
    "$UV_BIN" sync --frozen --no-editable \
      --project "$staging/src/services/screener-orchestrator"
  run_as_service "$staging/worker-venv/bin/python" \
    "$staging/src/workers/screener/scripts/verify-installed-signing-contract.py"
  mv "$staging" "$release_dir"
  trap - RETURN
}

write_release_env() {
  local output="$1" builder="$2" temporary
  temporary="${output}.tmp.$$"
  umask 077
  printf 'SCREENER_FLEET_BUILDER_IMAGE=%s\n' "$builder" >"$temporary"
  chown "$SERVICE_USER:$SERVICE_GROUP" "$temporary"
  chmod 0600 "$temporary"
  mv "$temporary" "$output"
}

stop_fleet() {
  local pids=() index
  "$SYSTEMCTL" stop ditto-screener-fleet-agent.service & pids+=("$!")
  for index in $(seq 1 "$WORKER_PROCESSES"); do
    "$SYSTEMCTL" stop "ditto-screener-worker@$index.service" & pids+=("$!")
  done
  for index in "${pids[@]}"; do wait "$index"; done
}

start_fleet() {
  local index
  "$SYSTEMCTL" start ditto-screener-fleet-agent.service
  for index in $(seq 1 "$WORKER_PROCESSES"); do
    "$SYSTEMCTL" start "ditto-screener-worker@$index.service"
  done
  sleep 5
  "$SYSTEMCTL" is-active --quiet ditto-screener-fleet-agent.service
  for index in $(seq 1 "$WORKER_PROCESSES"); do
    "$SYSTEMCTL" is-active --quiet "ditto-screener-worker@$index.service"
  done
}

activate_release() {
  local revision="$1" builder="$2" exact="$3" release_dir
  release_dir="$RELEASES_DIR/$revision"
  local old_target='' old_builder='' new_link="$FLEET_ROOT/.current.$$"
  [ ! -L "$CURRENT_LINK" ] || old_target="$(readlink "$CURRENT_LINK")"
  [ ! -f "$RELEASE_ENV" ] || old_builder="$(manifest_value "$RELEASE_ENV" SCREENER_FLEET_BUILDER_IMAGE)"
  install -o root -g root -m 0755 \
    "$release_dir/src/scripts/screener-fleet-auto-update.sh" \
    /usr/local/sbin/ditto-screener-fleet-auto-update
  ln -s "releases/$revision" "$new_link"
  stop_fleet
  mv -Tf "$new_link" "$CURRENT_LINK"
  write_release_env "$RELEASE_ENV" "$builder"
  if ! start_fleet; then
    log "candidate failed to start; restoring the previous release"
    stop_fleet || true
    if [ -n "$old_target" ]; then
      ln -s "$old_target" "$new_link"
      mv -Tf "$new_link" "$CURRENT_LINK"
    fi
    [ -z "$old_builder" ] || write_release_env "$RELEASE_ENV" "$old_builder"
    start_fleet || die "candidate and rollback release both failed to start"
    printf '%s\n' "$exact" >"$FAILED_CANDIDATE_FILE"
    return 1
  fi
  umask 077
  printf 'DESCRIPTOR=%s\nREVISION=%s\nVERSION=%s\nBUILDER_IMAGE=%s\nUPDATED_AT=%s\n' \
    "$exact" "$revision" "$(manifest_value "$STATE_DIR/candidate.env" FLEET_VERSION)" \
    "$builder" "$(date +%s)" >"$MANAGED_FILE"
  rm -f "$FAILED_CANDIDATE_FILE"
  log "activated $revision from authenticated descriptor $exact"
}

[ "$(id -u)" -eq 0 ] || die "run as root"
[[ "$WORKER_PROCESSES" =~ ^[1-9][0-9]*$ ]] || die "worker process count is invalid"
id "$SERVICE_USER" >/dev/null 2>&1 || die "service user does not exist"
for command in cosign docker git setpriv "$UV_BIN" "$SYSTEMCTL"; do
  if ! command -v "$command" >/dev/null 2>&1; then
    if [ "$command" = docker ]; then
      die "required command is unavailable: docker (install the Docker CLI; Debian 13 package: docker-cli)"
    fi
    die "required command is unavailable: $command"
  fi
done
install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0750 "$RELEASES_DIR"
install -d -o root -g root -m 0700 "$STATE_DIR"
exec {lock_fd}>"$LOCK_FILE"
flock -n "$lock_fd" || { log "another update is active"; exit 0; }

exact="$(resolve_descriptor)"
if [ -f "$FAILED_CANDIDATE_FILE" ] && [ "$(cat "$FAILED_CANDIDATE_FILE")" = "$exact" ]; then
  die "candidate is suppressed after a failed activation; remove $FAILED_CANDIDATE_FILE to retry"
fi
if [ -f "$MANAGED_FILE" ] && [ "$(manifest_value "$MANAGED_FILE" DESCRIPTOR)" = "$exact" ]; then
  log "already running authenticated descriptor $exact"
  exit 0
fi

cosign verify \
  --certificate-identity-regexp '^https://github.com/ditto-assistant/ditto-subnet/.github/workflows/release.yml@refs/heads/main$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  "$exact" >/dev/null
candidate="$STATE_DIR/candidate.env"
rm -f "$candidate"
extract_manifest "$exact" "$candidate"
validate_manifest "$candidate" || die "release manifest failed its closed schema"
verify_descriptor_labels "$exact" "$candidate" || die "descriptor labels do not match the manifest"
revision="$(manifest_value "$candidate" FLEET_REVISION)"
builder="$(manifest_value "$candidate" SUBMISSION_BUILDER_IMAGE)"
prepare_release "$revision" "$RELEASES_DIR/$revision"
activate_release "$revision" "$builder" "$exact"
