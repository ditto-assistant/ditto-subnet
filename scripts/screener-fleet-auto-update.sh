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
L2_WORKSPACE_ROOT="${SCREENER_FLEET_L2_WORKSPACE_ROOT:-/var/lib/ditto-screener-l2-workspaces}"
EXECUTOR_GROUP="${SCREENER_FLEET_EXECUTOR_GROUP:-ditto-builder}"
UV_BIN="${SCREENER_FLEET_UV_BIN:-/usr/local/bin/uv}"
SYSTEMCTL="${SCREENER_FLEET_SYSTEMCTL:-systemctl}"
EXPECTED_FORMAT_VERSION=1
EXPECTED_UPDATE_PROTOCOL=1
MANAGED_FILE="$STATE_DIR/managed-release.env"
FAILED_CANDIDATE_FILE="$STATE_DIR/failed-candidate"
LOCK_FILE="$STATE_DIR/lock"
SELF_PATH="${SCREENER_FLEET_SELF_PATH:-$STATE_DIR/ditto-screener-fleet-auto-update}"
ROOTLESS_DOCKER_HOST="${SCREENER_FLEET_ROOTLESS_DOCKER_HOST:-unix:///run/ditto-screener-docker/docker.sock}"
L2_ANALYZER_ACTIVE="ditto-screener-l2-analyzer:active"
DRAIN_BOUND_SECONDS="${SCREENER_FLEET_DRAIN_BOUND_SECONDS:-4200}"
DRAIN_STATUS="$STATE_DIR/drain-status.env"
# Worker leases live beside the review journal, under the fleet state root.
# The updater's own STATE_DIR is that root's updater/ subdirectory.
FLEET_STATE_DIR="${SCREENER_FLEET_STATE_DIR:-$(dirname "$STATE_DIR")}"
if [ -n "${SCREENER_FLEET_DRAIN_PY:-}" ]; then
  DRAIN_PY="$SCREENER_FLEET_DRAIN_PY"
elif [ -f "$(dirname "$SELF_PATH")/screener-fleet-drain.py" ]; then
  DRAIN_PY="$(dirname "$SELF_PATH")/screener-fleet-drain.py"
else
  DRAIN_PY="$(dirname "$0")/screener-fleet-drain.py"
fi
HELD_WORKERS="$STATE_DIR/held-workers"
DRAIN_POLL_SECONDS="${SCREENER_FLEET_DRAIN_POLL_SECONDS:-3}"

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
run_rootless_as_service() {
  run_as_service env DOCKER_HOST="$ROOTLESS_DOCKER_HOST" "$@"
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

prepare_l2_analyzer() {
  local revision="$1" release_dir="$2"
  local candidate="ditto-screener-l2-analyzer:candidate-$revision" label
  run_rootless_as_service docker info --format '{{json .SecurityOptions}}' \
    | grep -q 'rootless' || die "rootless analyzer executor is unavailable"
  label="$(run_rootless_as_service docker image inspect --format \
    '{{ index .Config.Labels "ai.heyditto.screener.sha" }}' \
    "$candidate" 2>/dev/null || true)"
  if [ "$label" != "$revision" ]; then
    run_rootless_as_service docker build \
      --file "$release_dir/src/workers/screener/deploy/l2-analyzer.Dockerfile" \
      --label "ai.heyditto.screener.sha=$revision" \
      --tag "$candidate" \
      "$release_dir/src/workers/screener" >&2
  fi
  printf '%s' "$candidate"
}

write_release_env() {
  # The service user cannot read the root-only managed-release.env, so the
  # activated revision/version travel here too: the worker heartbeats them
  # (protocol v7) so Backroom can see fleet adoption from the heartbeat alone.
  local output="$1" builder="$2" revision="${3:-}" version="${4:-}" temporary
  temporary="${output}.tmp.$$"
  umask 077
  {
    printf 'SCREENER_FLEET_BUILDER_IMAGE=%s\n' "$builder"
    [ -z "$revision" ] || printf 'SCREENER_FLEET_REVISION=%s\n' "$revision"
    [ -z "$version" ] || printf 'SCREENER_FLEET_VERSION=%s\n' "$version"
    [ -z "$revision" ] || printf 'SCREENER_FLEET_ACTIVATED_AT=%s\n' "$(date +%s)"
  } >"$temporary"
  chown "$SERVICE_USER:$SERVICE_GROUP" "$temporary"
  chmod 0600 "$temporary"
  mv "$temporary" "$output"
}

write_drain_status() {
  local phase="$1" detail="${2:-}"
  umask 077
  printf 'PHASE=%s\nTARGET_REVISION=%s\nSTARTED_AT=%s\nDETAIL=%s\nUPDATED_AT=%s\n' \
    "$phase" "${TARGET_REVISION:-}" "${DRAIN_STARTED_AT:-}" "$detail" "$(date +%s)" \
    >"$DRAIN_STATUS"
}

worker_indexes() {
  "$SYSTEMCTL" list-units --all --type=service --plain --no-legend \
    'ditto-screener-worker@*.service' \
    | awk '$1 ~ /^ditto-screener-worker@[1-9][0-9]*\.service$/ {
        worker = $1
        sub(/^ditto-screener-worker@/, "", worker)
        sub(/\.service$/, "", worker)
        print worker
      }'
}

lease_decision() {
  local index="$1" decision
  # A missing or crashing helper must not abort the drain after the fleet
  # agent has stopped. Treat it as an open lease: the drain bound still ends
  # the wait, and no worker is ever signalled beyond SIGTERM.
  decision="$(python3 "$DRAIN_PY" \
    --lease "$FLEET_STATE_DIR/workers/$index/active-lease.json" \
    --now "$(date +%s)" 2>/dev/null)" || decision=wait
  case "$decision" in
    ready|wait|held) printf '%s' "$decision" ;;
    *) printf 'wait' ;;
  esac
}

# "active" means a worker process is running. Once that process exits,
# Restart=always parks the unit in "activating" (auto-restart) for RestartSec
# and then starts it again from whatever release is current at that moment.
worker_state() {
  "$SYSTEMCTL" show -p ActiveState --value \
    "ditto-screener-worker@$1.service" 2>/dev/null || true
}

worker_main_pid() {
  "$SYSTEMCTL" show -p MainPID --value \
    "ditto-screener-worker@$1.service" 2>/dev/null || true
}

worker_process_running() {
  case "$1" in
    active|reloading|deactivating) return 0 ;;
    *) return 1 ;;
  esac
}

# Debian systemctl kill defaults to --kill-whom=all, which also signals Docker
# and L2 children in the unit cgroup. Only the Python main process should see
# SIGTERM so those children can finish and the worker can sign its verdict.
signal_worker_main() {
  "$SYSTEMCTL" kill --kill-whom=main -s SIGTERM \
    "ditto-screener-worker@$1.service" >/dev/null 2>&1 || true
}

# SIGTERM alone does not stop a unit: Restart=always would start the worker
# again on the old release before the symlink flips, and that fresh process
# would claim new work. A stop job cancels the pending restart. The updater
# runs under ProtectSystem=strict and cannot write a Restart=no drop-in, so
# this is the only runtime control. Issue it only when no worker process is
# running, so it can never signal an in-flight review.
cancel_worker_restart() {
  timeout 30 "$SYSTEMCTL" stop "ditto-screener-worker@$1.service" \
    >/dev/null 2>&1 || true
}

held_worker_still_running() {
  local index="$1" pid
  pid="$(worker_main_pid "$index")"
  [ -n "$pid" ] && [ "$pid" != 0 ] || return 1
  grep -qx "$index $pid" "$HELD_WORKERS" 2>/dev/null
}

stop_fleet() {
  local index decision bound state waiting
  : >"$HELD_WORKERS"
  DRAIN_STARTED_AT="$(date +%s)"
  write_drain_status draining
  # Stop claiming lane work. The stop job below follows the unit's KillMode;
  # this signal itself must not reach the agent's Docker children.
  "$SYSTEMCTL" kill --kill-whom=main -s SIGTERM \
    ditto-screener-fleet-agent.service >/dev/null 2>&1 || true
  timeout 60 "$SYSTEMCTL" stop ditto-screener-fleet-agent.service || true

  for index in $(worker_indexes); do
    signal_worker_main "$index"
  done
  bound=$((DRAIN_STARTED_AT + DRAIN_BOUND_SECONDS))
  while :; do
    waiting=0
    for index in $(worker_indexes); do
      state="$(worker_state "$index")"
      if ! worker_process_running "$state"; then
        cancel_worker_restart "$index"
        continue
      fi
      # Idempotent for a draining worker. It also stops a process that
      # Restart=always started between polls from claiming a second review.
      signal_worker_main "$index"
      decision="$(lease_decision "$index")"
      [ "$decision" != held ] || continue
      waiting=1
      write_drain_status draining "worker $index $decision"
    done
    [ "$waiting" -eq 1 ] || break
    [ "$(date +%s)" -lt "$bound" ] || break
    sleep "$DRAIN_POLL_SECONDS"
  done
  for index in $(worker_indexes); do
    state="$(worker_state "$index")"
    if ! worker_process_running "$state"; then
      cancel_worker_restart "$index"
      continue
    fi
    # Never escalate. The process already has SIGTERM, so it takes no new
    # claim; Restart=always brings it back on the activated release once its
    # review ends. start_fleet recognizes it by MainPID and leaves it alone.
    printf '%s %s\n' "$index" "$(worker_main_pid "$index")" >>"$HELD_WORKERS"
    write_drain_status held "worker $index kept without escalation"
    log "worker $index still holds a signed review; leaving it running"
    if [ "$index" -gt "$WORKER_PROCESSES" ]; then
      # Outside the requested count: queue a stop so the review can finish
      # but Restart=always cannot bring this worker back. KillMode=mixed
      # sends that stop's SIGTERM to the main process only.
      "$SYSTEMCTL" stop --no-block "ditto-screener-worker@$index.service" \
        >/dev/null 2>&1 || true
    fi
  done
  if [ -s "$HELD_WORKERS" ]; then
    write_drain_status held "active reviews kept"
  else
    write_drain_status drained
  fi

  "$SYSTEMCTL" stop ditto-screener-fleet-agent.service >/dev/null 2>&1 || true

  # Ansible normally reconciles this at converge time. The self-updater must
  # enforce the same bound too: release delivery is deliberately pull-based,
  # and it must be safe even when no Ansible run follows the canary change.
  for index in $(worker_indexes); do
    if [ "$index" -gt "$WORKER_PROCESSES" ]; then
      "$SYSTEMCTL" disable "ditto-screener-worker@$index.service"
    fi
  done
}

ensure_worker_state() {
  local index
  # The updater itself owns release convergence. A later scale-up must not
  # require an Ansible run merely to create paths systemd bind-mounts before
  # it can exec a worker.
  install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0700 "$STATE_DIR/workers"
  install -d -o "$SERVICE_USER" -g "$EXECUTOR_GROUP" -m 0770 "$L2_WORKSPACE_ROOT"
  for index in $(seq 1 "$WORKER_PROCESSES"); do
    install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0700 \
      "$STATE_DIR/workers/$index"
    install -d -o "$SERVICE_USER" -g "$EXECUTOR_GROUP" -m 0770 \
      "$L2_WORKSPACE_ROOT/$index"
  done
}

# Callers test this function with `if !`, which disables `set -e` inside it,
# so every failure must be counted explicitly.
start_fleet() {
  local index failed=0
  ensure_worker_state || return 1
  "$SYSTEMCTL" start ditto-screener-fleet-agent.service || failed=1
  for index in $(seq 1 "$WORKER_PROCESSES"); do
    # Re-enable the declared set too, so a previous smaller canary cannot
    # leave a later intentional scale-up stopped until an Ansible converge.
    "$SYSTEMCTL" enable "ditto-screener-worker@$index.service" || failed=1
    if held_worker_still_running "$index"; then
      log "worker $index is finishing a signed review; it restarts on this release when it exits"
      continue
    fi
    # Restart, not start: any other process still running here was started
    # before the symlink flipped and would keep serving the old release.
    "$SYSTEMCTL" restart "ditto-screener-worker@$index.service" || failed=1
  done
  sleep "${SCREENER_FLEET_START_SETTLE_SECONDS:-5}"
  "$SYSTEMCTL" is-active --quiet ditto-screener-fleet-agent.service || failed=1
  for index in $(seq 1 "$WORKER_PROCESSES"); do
    held_worker_still_running "$index" && continue
    "$SYSTEMCTL" is-active --quiet "ditto-screener-worker@$index.service" || failed=1
  done
  [ "$failed" -eq 0 ] || return 1
  write_drain_status active
}

# Once the drain has begun the fleet agent is stopped and workers are
# draining. Any abort before a fleet start succeeds (a failed command under
# `set -e`, or systemd's TimeoutStartSec SIGTERM) must bring the node back on
# whatever release is current rather than leave it down until the next timer.
restore_fleet_after_abort() {
  local status=$? index
  trap - EXIT TERM INT
  set +e
  log "update aborted after the drain began (exit $status); restarting the fleet on the current release"
  "$SYSTEMCTL" start --no-block ditto-screener-fleet-agent.service
  for index in $(seq 1 "$WORKER_PROCESSES"); do
    # start, never restart: a held worker keeps finishing its review.
    "$SYSTEMCTL" start --no-block "ditto-screener-worker@$index.service"
  done
  write_drain_status aborted "exit $status"
  exit "$status"
}

arm_fleet_restore() {
  trap restore_fleet_after_abort EXIT
  trap 'exit 143' TERM
  trap 'exit 130' INT
}

disarm_fleet_restore() {
  trap - EXIT TERM INT
}

activate_release() {
  local revision="$1" builder="$2" exact="$3" l2_candidate="$4" release_dir
  release_dir="$RELEASES_DIR/$revision"
  local old_target='' old_builder='' old_l2_image='' new_link="$FLEET_ROOT/.current.$$"
  [ ! -L "$CURRENT_LINK" ] || old_target="$(readlink "$CURRENT_LINK")"
  [ ! -f "$RELEASE_ENV" ] || old_builder="$(manifest_value "$RELEASE_ENV" SCREENER_FLEET_BUILDER_IMAGE)"
  local old_revision="" old_version=""
  [ ! -f "$RELEASE_ENV" ] || old_revision="$(manifest_value "$RELEASE_ENV" SCREENER_FLEET_REVISION)"
  [ ! -f "$RELEASE_ENV" ] || old_version="$(manifest_value "$RELEASE_ENV" SCREENER_FLEET_VERSION)"
  old_l2_image="$(run_rootless_as_service docker image inspect --format '{{.Id}}' \
    "$L2_ANALYZER_ACTIVE" 2>/dev/null || true)"
  install -o root -g root -m 0755 \
    "$release_dir/src/scripts/screener-fleet-auto-update.sh" \
    "$SELF_PATH"
  install -o root -g root -m 0755 \
    "$release_dir/src/scripts/screener-fleet-drain.py" \
    "$(dirname "$SELF_PATH")/screener-fleet-drain.py"
  install -o root -g root -m 0755 \
    "$release_dir/src/scripts/screener-fleet-release-hold.sh" \
    "$(dirname "$SELF_PATH")/screener-fleet-release-hold"
  ln -s "releases/$revision" "$new_link"
  TARGET_REVISION="$revision"
  arm_fleet_restore
  stop_fleet
  run_rootless_as_service docker tag "$l2_candidate" "$L2_ANALYZER_ACTIVE"
  mv -Tf "$new_link" "$CURRENT_LINK"
  write_release_env "$RELEASE_ENV" "$builder" "$revision" \
    "$(manifest_value "$STATE_DIR/candidate.env" FLEET_VERSION)"
  if ! start_fleet; then
    log "candidate failed to start; restoring the previous release"
    stop_fleet || true
    if [ -n "$old_target" ]; then
      ln -s "$old_target" "$new_link"
      mv -Tf "$new_link" "$CURRENT_LINK"
    fi
    if [ -n "$old_l2_image" ]; then
      run_rootless_as_service docker tag "$old_l2_image" "$L2_ANALYZER_ACTIVE"
    else
      run_rootless_as_service docker image rm --force "$L2_ANALYZER_ACTIVE" \
        >/dev/null 2>&1 || true
    fi
    [ -z "$old_builder" ] || write_release_env "$RELEASE_ENV" "$old_builder" \
      "$old_revision" "$old_version"
    start_fleet || die "candidate and rollback release both failed to start"
    disarm_fleet_restore
    printf '%s\n' "$exact" >"$FAILED_CANDIDATE_FILE"
    return 1
  fi
  disarm_fleet_restore
  umask 077
  printf 'DESCRIPTOR=%s\nREVISION=%s\nVERSION=%s\nBUILDER_IMAGE=%s\nUPDATED_AT=%s\n' \
    "$exact" "$revision" "$(manifest_value "$STATE_DIR/candidate.env" FLEET_VERSION)" \
    "$builder" "$(date +%s)" >"$MANAGED_FILE"
  rm -f "$FAILED_CANDIDATE_FILE"
  run_rootless_as_service docker image rm "$l2_candidate" >/dev/null 2>&1 || true
  log "activated $revision from authenticated descriptor $exact"
}

# Test-only entrypoints: exercise the real drain and start logic against a
# fake systemctl. Production units never set this variable.
case "${SCREENER_FLEET_TEST_ENTRYPOINT:-}" in
  '') ;;
  stop_fleet)
    arm_fleet_restore
    stop_fleet
    disarm_fleet_restore
    exit 0
    ;;
  start_fleet)
    start_fleet || exit 1
    exit 0
    ;;
  *) die "unknown test entrypoint" ;;
esac
[ "$(id -u)" -eq 0 ] || die "run as root"
[[ "$SELF_PATH" = "$STATE_DIR/"* ]] || \
  die "self-update path must stay inside the updater state directory"
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
l2_candidate="$(prepare_l2_analyzer "$revision" "$RELEASES_DIR/$revision")"
activate_release "$revision" "$builder" "$exact" "$l2_candidate"
