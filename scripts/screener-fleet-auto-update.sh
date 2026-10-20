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
PROC_ROOT="${SCREENER_FLEET_PROC_ROOT:-/proc}"
# `rolling` (default) keeps idle workers claiming while busy ones finish their
# reviews; `drain-all` restores the whole-fleet drain for every release.
ROLLOUT_MODE="${SCREENER_FLEET_ROLLOUT_MODE:-rolling}"
# A rolled worker counts as started once one process has stayed active this
# long on the candidate; it must get there within the start window.
ROLL_SETTLE_SECONDS="${SCREENER_FLEET_ROLL_SETTLE_SECONDS:-30}"
ROLL_START_SECONDS="${SCREENER_FLEET_ROLL_START_SECONDS:-300}"
ANALYZER_CONTEXT_REL="src/workers/screener"
ANALYZER_DOCKERFILE_REL="$ANALYZER_CONTEXT_REL/deploy/l2-analyzer.Dockerfile"
# The release checkout being prepared. errexit skips RETURN traps, so the EXIT
# trap owns its removal; it is cleared once the checkout is promoted.
STAGING_DIR=''

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
    return 0
  fi
  local staging="${release_dir}.staging.$$"
  STAGING_DIR="$staging"
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
  run_as_service "$staging/worker-venv/bin/python" \
    "$staging/src/workers/screener/scripts/verify-installed-signing-contract.py"
  mv "$staging" "$release_dir"
  STAGING_DIR=''
}

cleanup_staging_on_exit() { [ -z "$STAGING_DIR" ] || rm -rf -- "$STAGING_DIR"; }

arm_staging_cleanup() {
  trap cleanup_staging_on_exit EXIT
  trap 'exit 143' TERM
  trap 'exit 130' INT
}

# Holding the update lock means no other updater owns a staging checkout, so
# anything left here was abandoned by a run that died before its EXIT trap.
sweep_staging() {
  local entry removed=0
  for entry in "$RELEASES_DIR"/*.staging.*; do
    [[ "${entry##*/}" =~ ^[0-9a-f]{40}\.staging\.[0-9]+$ ]] || continue
    if rm -rf -- "$entry"; then
      removed=$((removed + 1))
    else
      log "could not remove stale staging dir $entry"
    fi
  done
  [ "$removed" -eq 0 ] || log "removed $removed stale release staging dir(s)"
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

# A previous release may still have the producerless lane agent installed.
# Retire it before switching the worker release, including on rollback.
retire_fleet_agent() {
  timeout 60 "$SYSTEMCTL" stop ditto-screener-fleet-agent.service || true
  if "$SYSTEMCTL" is-active --quiet ditto-screener-fleet-agent.service; then
    die "retired fleet agent is still active"
  fi
  if "$SYSTEMCTL" is-enabled --quiet ditto-screener-fleet-agent.service; then
    "$SYSTEMCTL" disable ditto-screener-fleet-agent.service || \
      die "retired fleet agent could not be disabled"
  fi
}

stop_fleet() {
  local index decision bound state waiting
  : >"$HELD_WORKERS"
  DRAIN_STARTED_AT="$(date +%s)"
  write_drain_status draining
  retire_fleet_agent

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
  for index in $(seq 1 "$WORKER_PROCESSES"); do
    held_worker_still_running "$index" && continue
    "$SYSTEMCTL" is-active --quiet "ditto-screener-worker@$index.service" || failed=1
  done
  [ "$failed" -eq 0 ] || return 1
  write_drain_status active
}

# Once the drain has begun the retired lane agent is stopped and workers are
# draining. Any abort before a fleet start succeeds (a failed command under
# `set -e`, or systemd's TimeoutStartSec SIGTERM) must bring the node back on
# whatever release is current rather than leave it down until the next timer.
restore_fleet_after_abort() {
  local status=$? index
  trap - EXIT TERM INT
  set +e
  log "update aborted after the drain began (exit $status); restarting the fleet on the current release"
  for index in $(seq 1 "$WORKER_PROCESSES"); do
    # start, never restart: a held worker keeps finishing its review.
    "$SYSTEMCTL" start --no-block "ditto-screener-worker@$index.service"
  done
  write_drain_status aborted "exit $status"
  cleanup_staging_on_exit
  exit "$status"
}

arm_fleet_restore() {
  trap restore_fleet_after_abort EXIT
  trap 'exit 143' TERM
  trap 'exit 130' INT
}

disarm_fleet_restore() {
  arm_staging_cleanup
}

# Name of the releases/<sha> a live process started from. systemd resolves the
# `current` WorkingDirectory at exec, so /proc cwd pins the process's release.
process_release() {
  local pid="$1" releases path name
  releases="$(readlink -f "$RELEASES_DIR")" || return 1
  path="$(readlink -e "$PROC_ROOT/$pid/cwd")" || return 1
  case "$path" in
    "$releases"/*) name="${path#"$releases"/}"; name="${name%%/*}" ;;
    *) return 1 ;;
  esac
  [[ "$name" =~ ^[0-9a-f]{40}$ ]] || return 1
  printf '%s' "$name"
}

# Both build contexts hold byte-identical regular files for one COPY source.
analyzer_source_matches() {
  local left="$1" right="$2" pattern="$3" path
  local -a left_files right_files
  mapfile -t left_files < <(cd "$left" && compgen -G "$pattern" | LC_ALL=C sort)
  mapfile -t right_files < <(cd "$right" && compgen -G "$pattern" | LC_ALL=C sort)
  [ "${#left_files[@]}" -gt 0 ] || return 1
  [ "${left_files[*]}" = "${right_files[*]}" ] || return 1
  for path in "${left_files[@]}"; do
    [ -f "$left/$path" ] && [ ! -L "$left/$path" ] || return 1
    [ -f "$right/$path" ] && [ ! -L "$right/$path" ] || return 1
    cmp -s "$left/$path" "$right/$path" || return 1
  done
}

# Succeeds only when two releases provably build the analyzer from identical
# inputs: the same Dockerfile, .dockerignore, and every file a COPY names.
# Anything this parser does not understand counts as a difference.
analyzer_inputs_match() {
  local left="$1" right="$2" line token source count=0
  local -a words sources
  cmp -s "$left/$ANALYZER_DOCKERFILE_REL" "$right/$ANALYZER_DOCKERFILE_REL" || return 1
  if [ -e "$left/$ANALYZER_CONTEXT_REL/.dockerignore" ] || \
    [ -e "$right/$ANALYZER_CONTEXT_REL/.dockerignore" ]; then
    cmp -s "$left/$ANALYZER_CONTEXT_REL/.dockerignore" \
      "$right/$ANALYZER_CONTEXT_REL/.dockerignore" || return 1
  fi
  while IFS= read -r line || [ -n "$line" ]; do
    # A parser directive can change the line-continuation character.
    [[ ! "${line,,}" =~ ^#[[:space:]]*escape[[:space:]]*= ]] || return 1
    read -r -a words <<<"$line"
    [ "${#words[@]}" -gt 0 ] || continue
    case "${words[0]^^}" in
      ADD) return 1 ;;
      COPY) ;;
      *) continue ;;
    esac
    # A continued or heredoc COPY is not parsed; treat it as a difference.
    [[ ! "$line" =~ \\[[:space:]]*$ ]] || return 1
    [[ "$line" != *'<<'* ]] || return 1
    sources=()
    for token in "${words[@]:1}"; do
      case "$token" in
        --from|--from=*) return 1 ;;
        --*) continue ;;
      esac
      sources+=("$token")
    done
    [ "${#sources[@]}" -ge 2 ] || return 1
    unset 'sources[-1]'
    for source in "${sources[@]}"; do
      [[ "$source" =~ ^[A-Za-z0-9_][A-Za-z0-9_.*/-]*$ ]] || return 1
      [[ "$source" != *..* ]] || return 1
      analyzer_source_matches "$left/$ANALYZER_CONTEXT_REL" \
        "$right/$ANALYZER_CONTEXT_REL" "$source" || return 1
      count=$((count + 1))
    done
  done <"$left/$ANALYZER_DOCKERFILE_REL"
  [ "$count" -gt 0 ]
}

# Sets ROLLING_BLOCKER to why this activation must drain the whole fleet, or
# to nothing when a rolling activation is safe. The rootless
# ditto-screener-l2-analyzer:active tag is the only release artifact a running
# worker resolves after it starts, so rolling also requires every release a
# live worker still runs from to share the candidate's analyzer inputs.
rolling_check() {
  local revision="$1" previous="$2" indexes index pid state name
  local -A live=()
  ROLLING_BLOCKER=''
  if [ "$ROLLOUT_MODE" != rolling ]; then
    ROLLING_BLOCKER="rollout mode is $ROLLOUT_MODE"
    return 0
  fi
  if ! [[ "$previous" =~ ^releases/[0-9a-f]{40}$ ]] || \
    [ ! -d "$RELEASES_DIR/${previous#releases/}/src" ]; then
    ROLLING_BLOCKER='no previous release to roll from'
    return 0
  fi
  live["${previous#releases/}"]=1
  if ! indexes="$(worker_indexes)"; then
    ROLLING_BLOCKER='workers could not be listed'
    return 0
  fi
  for index in $indexes; do
    state="$(worker_state "$index")"
    worker_process_running "$state" || continue
    if [ "$index" -gt "$WORKER_PROCESSES" ]; then
      ROLLING_BLOCKER="worker $index is above the requested count"
      return 0
    fi
    pid="$(worker_main_pid "$index")"
    if ! [[ "$pid" =~ ^[1-9][0-9]*$ ]] || ! name="$(process_release "$pid")"; then
      ROLLING_BLOCKER="worker $index release is unknown"
      return 0
    fi
    live["$name"]=1
  done
  for name in "${!live[@]}"; do
    [ "$name" != "$revision" ] || continue
    if ! analyzer_inputs_match "$RELEASES_DIR/$name" "$RELEASES_DIR/$revision"; then
      ROLLING_BLOCKER="analyzer inputs differ from live release $name"
      return 0
    fi
  done
}

# Move every requested worker onto release $1 without a fleet stop. Each
# process that predates the roll gets SIGTERM on its main PID only: an idle
# worker exits at once and Restart=always starts it again from `current`,
# while a busy one posts its signed verdict first and takes no further claim.
# The updater never stops, kills, or restarts a running worker here, so idle
# workers keep claiming on the new release while reviews finish.
#
# Returns 0 when every worker is settled on the target or still finishing a
# pre-roll review, and with $2=1 at least one worker has settled on the
# target. Returns 1 when a worker cannot settle within the start window and 2
# when the drain bound passes first. Callers use `||`, so errexit is off here.
roll_workers() {
  local target="$1" require_started="$2" index pid state name now bound
  local started pending finishing
  local -A pre_pid=() seen_pid=() seen_at=() since=()
  for index in $(seq 1 "$WORKER_PROCESSES"); do
    pid="$(worker_main_pid "$index")"
    [[ "$pid" =~ ^[0-9]+$ ]] || pid=0
    pre_pid[$index]="$pid"
    [ "$pid" = 0 ] || signal_worker_main "$index"
  done
  bound=$(($(date +%s) + DRAIN_BOUND_SECONDS))
  while :; do
    now="$(date +%s)"
    started=0
    pending=0
    finishing=''
    : >"$HELD_WORKERS"
    for index in $(seq 1 "$WORKER_PROCESSES"); do
      state="$(worker_state "$index")"
      pid="$(worker_main_pid "$index")"
      [[ "$pid" =~ ^[0-9]+$ ]] || pid=0
      if worker_process_running "$state" && [ "$pid" != 0 ]; then
        if [ "$pid" = "${pre_pid[$index]}" ]; then
          # Idempotent for a draining worker; it finishes on its own release.
          signal_worker_main "$index"
          since[$index]=''
          finishing+=" $index:$(lease_decision "$index")"
          printf '%s %s\n' "$index" "$pid" >>"$HELD_WORKERS"
          continue
        fi
        if name="$(process_release "$pid")" && [ "$name" != "$target" ]; then
          # Started from another release; let it finish and come back.
          pre_pid[$index]="$pid"
          signal_worker_main "$index"
          since[$index]=''
          finishing+=" $index:$(lease_decision "$index")"
          printf '%s %s\n' "$index" "$pid" >>"$HELD_WORKERS"
          continue
        fi
        if [ "${seen_pid[$index]:-}" != "$pid" ]; then
          seen_pid[$index]="$pid"
          seen_at[$index]="$now"
        fi
        if [ -n "${name:-}" ] && [ "$state" = active ] && \
          [ $((now - ${seen_at[$index]})) -ge "$ROLL_SETTLE_SECONDS" ]; then
          started=$((started + 1))
          since[$index]=''
          continue
        fi
      else
        case "$state" in
          inactive|failed)
            "$SYSTEMCTL" start --no-block "ditto-screener-worker@$index.service" \
              >/dev/null 2>&1 || true
            ;;
        esac
      fi
      [ -n "${since[$index]:-}" ] || since[$index]="$now"
      if [ $((now - ${since[$index]})) -ge "$ROLL_START_SECONDS" ]; then
        log "worker $index did not start on release $target"
        return 1
      fi
      pending=1
    done
    if [ "$pending" -eq 0 ] && { [ "$require_started" -eq 0 ] || [ "$started" -gt 0 ]; }; then
      ROLL_FINISHING="${finishing# }"
      return 0
    fi
    [ "$now" -lt "$bound" ] || return 2
    write_drain_status rolling "started=$started finishing:${finishing:- none}"
    sleep "$DRAIN_POLL_SECONDS"
  done
}

# Put back the link, analyzer tag, and release env captured before a rolling
# activation. Never stops or kills a worker.
restore_previous_release() {
  local status=0
  if ! ln -sfn "$PREV_TARGET" "$NEW_LINK" || ! mv -Tf "$NEW_LINK" "$CURRENT_LINK"; then
    status=1
  fi
  if [ -n "$PREV_L2_IMAGE" ]; then
    run_rootless_as_service docker tag "$PREV_L2_IMAGE" "$L2_ANALYZER_ACTIVE" || status=1
  else
    run_rootless_as_service docker image rm --force "$L2_ANALYZER_ACTIVE" \
      >/dev/null 2>&1 || true
  fi
  if [ -n "$PREV_BUILDER" ]; then
    write_release_env "$RELEASE_ENV" "$PREV_BUILDER" "$PREV_REVISION" \
      "$PREV_VERSION" || status=1
  fi
  return "$status"
}

# An abort after the rolling flip (a failed command under `set -e`, or
# systemd's TimeoutStartSec SIGTERM) restores the previous release. Every
# worker is asked, by SIGTERM to its main process only, to come back on it once
# any review it holds is finished.
restore_roll_after_abort() {
  local status=$? index
  trap - EXIT TERM INT
  set +e
  log "rolling update aborted (exit $status); restoring the previous release"
  restore_previous_release || log "previous release could not be fully restored"
  for index in $(seq 1 "$WORKER_PROCESSES"); do
    signal_worker_main "$index"
    "$SYSTEMCTL" start --no-block "ditto-screener-worker@$index.service"
  done
  write_drain_status aborted "exit $status"
  cleanup_staging_on_exit
  exit "$status"
}

arm_roll_restore() {
  trap restore_roll_after_abort EXIT
  trap 'exit 143' TERM
  trap 'exit 130' INT
}

# Rolling activation: flip the release, then let each worker move over as soon
# as it is idle. The previous-release state comes from remember_previous_release.
roll_release() {
  local revision="$1" builder="$2" exact="$3" l2_candidate="$4" version="$5"
  local index result=0
  DRAIN_STARTED_AT="$(date +%s)"
  : >"$HELD_WORKERS"
  write_drain_status rolling "from ${PREV_TARGET#releases/}"
  retire_fleet_agent
  ensure_worker_state
  for index in $(seq 1 "$WORKER_PROCESSES"); do
    "$SYSTEMCTL" enable "ditto-screener-worker@$index.service"
  done
  # rolling_check refused any running worker above the requested count. A stop
  # job is only ever issued to a unit with no worker process; one that started
  # since that check ends this run before anything has changed.
  for index in $(worker_indexes); do
    if [ "$index" -gt "$WORKER_PROCESSES" ]; then
      if worker_process_running "$(worker_state "$index")"; then
        die "worker $index above the requested count started during activation"
      fi
      cancel_worker_restart "$index"
      "$SYSTEMCTL" disable "ditto-screener-worker@$index.service"
    fi
  done
  arm_roll_restore
  run_rootless_as_service docker tag "$l2_candidate" "$L2_ANALYZER_ACTIVE"
  mv -Tf "$NEW_LINK" "$CURRENT_LINK"
  write_release_env "$RELEASE_ENV" "$builder" "$revision" "$version"
  ROLL_FINISHING=''
  roll_workers "$revision" 1 || result=$?
  if [ "$result" -eq 0 ]; then
    disarm_fleet_restore
    if [ -n "$ROLL_FINISHING" ]; then
      write_drain_status active "finishing on the previous release: $ROLL_FINISHING"
      log "workers still finishing a review on the previous release: $ROLL_FINISHING"
    else
      write_drain_status active
    fi
    return 0
  fi
  if [ "$result" -eq 1 ]; then
    log "candidate failed to start; restoring the previous release"
  else
    log "no worker started on the candidate before the drain bound; restoring the previous release"
  fi
  restore_previous_release || die "previous release could not be restored"
  roll_workers "${PREV_TARGET#releases/}" 0 || \
    die "candidate and rollback release both failed to start"
  disarm_fleet_restore
  if [ "$result" -eq 1 ]; then
    printf '%s\n' "$exact" >"$FAILED_CANDIDATE_FILE"
    write_drain_status rolled_back "candidate failed to start"
  else
    write_drain_status rolled_back "no worker became idle before the drain bound; retried next run"
  fi
  return 1
}

# Keep the activated release, the previous one (the next update's rollback
# target), and any release a live worker still runs from: a held review keeps
# its pre-flip checkout until it exits. Workers start in `current`, so their
# resolved /proc cwd and exe name the release they started on. Any doubt
# about a live worker skips pruning for this run. Callers use `||`, which
# disables errexit here, so every failure is handled explicitly.
prune_releases() {
  local previous="$1" keep=' ' target releases indexes index pid link path entry name
  case "$previous" in
    none) ;;
    releases/*)
      if ! [[ "$previous" =~ ^releases/[0-9a-f]{40}$ ]]; then
        log "previous release is unknown; not pruning"
        return 0
      fi
      keep+="${previous##*/} "
      ;;
    *) log "previous release is unknown; not pruning"; return 0 ;;
  esac
  target="$(readlink "$CURRENT_LINK")" || { log "current release is unreadable; not pruning"; return 0; }
  if ! [[ "$target" =~ ^releases/[0-9a-f]{40}$ ]]; then
    log "current release is invalid; not pruning"
    return 0
  fi
  keep+="${target##*/} "
  releases="$(readlink -f "$RELEASES_DIR")" || return 1
  indexes="$(worker_indexes)" || { log "workers could not be listed; not pruning"; return 0; }
  for index in $indexes; do
    pid="$("$SYSTEMCTL" show -p MainPID --value "ditto-screener-worker@$index.service")" || pid=''
    if ! [[ "$pid" =~ ^[0-9]+$ ]]; then
      log "worker $index MainPID is unknown; not pruning"
      return 0
    fi
    [ "$pid" != 0 ] || continue
    for link in cwd exe; do
      if ! path="$(readlink -e "$PROC_ROOT/$pid/$link")"; then
        log "worker $index process $pid $link is unreadable; not pruning"
        return 0
      fi
      case "$path" in
        "$releases"/*)
          name="${path#"$releases"/}"
          keep+="${name%%/*} "
          ;;
      esac
    done
  done
  for entry in "$RELEASES_DIR"/*; do
    name="${entry##*/}"
    [[ "$name" =~ ^[0-9a-f]{40}$ ]] || continue
    [[ "$keep" != *" $name "* ]] || continue
    if rm -rf -- "$entry"; then
      log "pruned superseded release $name"
    else
      log "prune failed: $entry"
    fi
  done
}

# Moving :active leaves the previous analyzer image untagged. Remove only
# untagged analyzer images; Docker keeps any image a container still uses.
prune_analyzer_images() {
  run_rootless_as_service docker image prune --force --filter dangling=true \
    --filter label=ai.heyditto.screener.sha >/dev/null
}

prune_activated_release() {
  local previous revision target
  previous="$(manifest_value "$MANAGED_FILE" PREVIOUS_RELEASE)"
  revision="$(manifest_value "$MANAGED_FILE" REVISION)"
  target="$(readlink "$CURRENT_LINK")" || target=''
  if [[ "$revision" =~ ^[0-9a-f]{40}$ ]] && [ "$target" = "releases/$revision" ]; then
    prune_releases "$previous" || log "release pruning failed"
  else
    log "managed and current releases differ; not pruning releases"
  fi
  prune_analyzer_images || log "analyzer image pruning failed"
}

# Capture what a rollback must restore: the link, analyzer image, and env.
remember_previous_release() {
  PREV_TARGET=''
  PREV_BUILDER=''
  PREV_REVISION=''
  PREV_VERSION=''
  [ ! -L "$CURRENT_LINK" ] || PREV_TARGET="$(readlink "$CURRENT_LINK")"
  if [ -f "$RELEASE_ENV" ]; then
    PREV_BUILDER="$(manifest_value "$RELEASE_ENV" SCREENER_FLEET_BUILDER_IMAGE)"
    PREV_REVISION="$(manifest_value "$RELEASE_ENV" SCREENER_FLEET_REVISION)"
    PREV_VERSION="$(manifest_value "$RELEASE_ENV" SCREENER_FLEET_VERSION)"
  fi
  PREV_L2_IMAGE="$(run_rootless_as_service docker image inspect --format '{{.Id}}' \
    "$L2_ANALYZER_ACTIVE" 2>/dev/null || true)"
  NEW_LINK="$FLEET_ROOT/.current.$$"
}

activate_release() {
  local revision="$1" builder="$2" exact="$3" l2_candidate="$4" release_dir version
  release_dir="$RELEASES_DIR/$revision"
  version="$(manifest_value "$STATE_DIR/candidate.env" FLEET_VERSION)"
  remember_previous_release
  install -o root -g root -m 0755 \
    "$release_dir/src/scripts/screener-fleet-auto-update.sh" \
    "$SELF_PATH"
  install -o root -g root -m 0755 \
    "$release_dir/src/scripts/screener-fleet-drain.py" \
    "$(dirname "$SELF_PATH")/screener-fleet-drain.py"
  install -o root -g root -m 0755 \
    "$release_dir/src/scripts/screener-fleet-release-hold.sh" \
    "$(dirname "$SELF_PATH")/screener-fleet-release-hold"
  ln -s "releases/$revision" "$NEW_LINK"
  TARGET_REVISION="$revision"
  rolling_check "$revision" "$PREV_TARGET"
  if [ -z "$ROLLING_BLOCKER" ]; then
    log "rolling activation: idle workers move to $revision while reviews finish"
    # A plain call keeps errexit; a rollback returns 1 and ends this run.
    roll_release "$revision" "$builder" "$exact" "$l2_candidate" "$version"
  else
    log "draining every worker before activation: $ROLLING_BLOCKER"
    arm_fleet_restore
    stop_fleet
    run_rootless_as_service docker tag "$l2_candidate" "$L2_ANALYZER_ACTIVE"
    mv -Tf "$NEW_LINK" "$CURRENT_LINK"
    write_release_env "$RELEASE_ENV" "$builder" "$revision" "$version"
    if ! start_fleet; then
      log "candidate failed to start; restoring the previous release"
      stop_fleet || true
      if [ -n "$PREV_TARGET" ]; then
        ln -s "$PREV_TARGET" "$NEW_LINK"
        mv -Tf "$NEW_LINK" "$CURRENT_LINK"
      fi
      if [ -n "$PREV_L2_IMAGE" ]; then
        run_rootless_as_service docker tag "$PREV_L2_IMAGE" "$L2_ANALYZER_ACTIVE"
      else
        run_rootless_as_service docker image rm --force "$L2_ANALYZER_ACTIVE" \
          >/dev/null 2>&1 || true
      fi
      [ -z "$PREV_BUILDER" ] || write_release_env "$RELEASE_ENV" "$PREV_BUILDER" \
        "$PREV_REVISION" "$PREV_VERSION"
      start_fleet || die "candidate and rollback release both failed to start"
      disarm_fleet_restore
      printf '%s\n' "$exact" >"$FAILED_CANDIDATE_FILE"
      return 1
    fi
    disarm_fleet_restore
  fi
  umask 077
  printf 'DESCRIPTOR=%s\nREVISION=%s\nVERSION=%s\nBUILDER_IMAGE=%s\nPREVIOUS_RELEASE=%s\nUPDATED_AT=%s\n' \
    "$exact" "$revision" "$version" \
    "$builder" "${PREV_TARGET:-none}" "$(date +%s)" >"$MANAGED_FILE"
  rm -f "$FAILED_CANDIDATE_FILE"
  run_rootless_as_service docker image rm "$l2_candidate" >/dev/null 2>&1 || true
  log "activated $revision from authenticated descriptor $exact"
  prune_activated_release
}

# Test-only entrypoints: exercise the real drain, start, preparation, and
# pruning logic against fake commands. Production units never set this variable.
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
  prepare_release)
    arm_staging_cleanup
    prepare_release "$SCREENER_FLEET_TEST_REVISION" \
      "$RELEASES_DIR/$SCREENER_FLEET_TEST_REVISION"
    exit 0
    ;;
  sweep_staging)
    sweep_staging
    exit 0
    ;;
  prune_releases)
    prune_releases "${SCREENER_FLEET_TEST_PREVIOUS:-}" || exit 1
    exit 0
    ;;
  prune_analyzer_images)
    prune_analyzer_images
    exit 0
    ;;
  prune_activated_release)
    prune_activated_release
    exit 0
    ;;
  rolling_check)
    rolling_check "$SCREENER_FLEET_TEST_REVISION" "${SCREENER_FLEET_TEST_PREVIOUS:-}"
    printf '%s\n' "${ROLLING_BLOCKER:-eligible}"
    exit 0
    ;;
  roll_release)
    arm_staging_cleanup
    remember_previous_release
    ln -s "releases/$SCREENER_FLEET_TEST_REVISION" "$NEW_LINK"
    TARGET_REVISION="$SCREENER_FLEET_TEST_REVISION"
    roll_release "$SCREENER_FLEET_TEST_REVISION" "$SCREENER_FLEET_TEST_BUILDER" \
      "$SCREENER_FLEET_TEST_DESCRIPTOR" \
      "ditto-screener-l2-analyzer:candidate-$SCREENER_FLEET_TEST_REVISION" 1.2.3
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
arm_staging_cleanup
sweep_staging

exact="$(resolve_descriptor)"
if [ -f "$FAILED_CANDIDATE_FILE" ] && [ "$(cat "$FAILED_CANDIDATE_FILE")" = "$exact" ]; then
  die "candidate is suppressed after a failed activation; remove $FAILED_CANDIDATE_FILE to retry"
fi
if [ -f "$MANAGED_FILE" ] && [ "$(manifest_value "$MANAGED_FILE" DESCRIPTOR)" = "$exact" ]; then
  prune_activated_release
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
