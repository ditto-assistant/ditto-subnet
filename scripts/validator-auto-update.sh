#!/usr/bin/env bash
# Discover a compatible validator release, drain safely, and replace only the
# explicitly labelled ditto-subnet Compose service. No other service is pulled,
# stopped, recreated, or inspected for update eligibility.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE="${DITTO_VALIDATOR_COMPOSE:-$ROOT_DIR/scripts/validator-compose.sh}"
ENV_FILE="${DITTO_SUBNET_ENV_FILE:-$ROOT_DIR/.env}"
STATE_DIR="${DITTO_VALIDATOR_UPDATE_STATE_DIR:-$ROOT_DIR/.validator-update}"
LAST_UPDATE_FILE="$STATE_DIR/last-update.env"
FAILED_CANDIDATE_FILE="$STATE_DIR/failed-candidate"
TRANSACTION_FILE="$STATE_DIR/transaction.env"
LOCK_DIR="$STATE_DIR/lock"
IMAGE_REPOSITORY="ghcr.io/ditto-assistant/ditto-subnet-validator"
CANDIDATE_CHANNEL="$IMAGE_REPOSITORY:compat-1"
EXPECTED_SOURCE="https://github.com/ditto-assistant/ditto-subnet"
EXPECTED_COMPATIBILITY_EPOCH="1"
EXPECTED_HEARTBEAT_PROTOCOL="4"
EXPECTED_UPDATE_PROTOCOL="1"
EXPECTED_COMPOSE_SCHEMA="1"
RUNTIME_STATE_PATH="/tmp/ditto-validator-update-state.json"
LOCK_HELD=false
DRAINED_CONTAINER=""
OLD_CONTAINER_STOPPED=false
TRANSACTION_COMPLETE=false
ROLLBACK_REF=""
ROLLBACK_IMAGE_ID=""
CLEANUP_READY_TIMEOUT=180
CLEANUP_CHECK_SECONDS=5

log() {
  printf 'validator-auto-update: %s\n' "$*" >&2
}

die() {
  log "error: $*"
  exit 1
}

setting() {
  local name="$1" default="$2" value="${!1-}" line=""
  if [ -z "$value" ] && [ -f "$ENV_FILE" ]; then
    line="$(
      awk -v key="$name" '
        index($0, key "=") == 1 { value = substr($0, length(key) + 2) }
        END { if (value != "") print value }
      ' "$ENV_FILE"
    )"
    value="$line"
  fi
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  if [[ "$value" == \"*\" && "$value" == *\" ]]; then
    value="${value:1:${#value}-2}"
  elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s' "${value:-$default}"
}

is_true() {
  case "$1" in
    1 | true | TRUE | True | yes | YES | Yes) return 0 ;;
    *) return 1 ;;
  esac
}

require_positive_integer() {
  local name="$1" value="$2"
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || die "$name must be a positive integer"
}

docker_image_label() {
  local image="$1" label="$2" value
  value="$(
    docker image inspect --format "{{ index .Config.Labels \"$label\" }}" \
      "$image" 2>/dev/null || true
  )"
  [ "$value" = "<no value>" ] && value=""
  printf '%s' "$value"
}

docker_container_label() {
  local container="$1" label="$2" value
  value="$(
    docker inspect --format "{{ index .Config.Labels \"$label\" }}" \
      "$container" 2>/dev/null || true
  )"
  [ "$value" = "<no value>" ] && value=""
  printf '%s' "$value"
}

validate_release_image() {
  local image="$1" label value
  for label in \
    io.heyditto.validator-service \
    io.heyditto.validator.compatibility-epoch \
    io.heyditto.validator.heartbeat-protocol \
    io.heyditto.validator.update-protocol \
    io.heyditto.validator.compose-schema \
    org.opencontainers.image.source \
    org.opencontainers.image.version \
    org.opencontainers.image.revision; do
    value="$(docker_image_label "$image" "$label")"
    [ -n "$value" ] || return 1
  done
  [ "$(docker_image_label "$image" io.heyditto.validator-service)" = "true" ] || return 1
  [ "$(docker_image_label "$image" io.heyditto.validator.compatibility-epoch)" = "$EXPECTED_COMPATIBILITY_EPOCH" ] || return 1
  [ "$(docker_image_label "$image" io.heyditto.validator.heartbeat-protocol)" = "$EXPECTED_HEARTBEAT_PROTOCOL" ] || return 1
  [ "$(docker_image_label "$image" io.heyditto.validator.update-protocol)" = "$EXPECTED_UPDATE_PROTOCOL" ] || return 1
  [ "$(docker_image_label "$image" io.heyditto.validator.compose-schema)" = "$EXPECTED_COMPOSE_SCHEMA" ] || return 1
  [ "$(docker_image_label "$image" org.opencontainers.image.source)" = "$EXPECTED_SOURCE" ] || return 1
  value="$(docker_image_label "$image" org.opencontainers.image.version)"
  [[ "$value" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
  value="$(docker_image_label "$image" org.opencontainers.image.revision)"
  [[ "$value" =~ ^[0-9a-f]{40}$ ]] || return 1
}

semver_greater() {
  local candidate="$1" current="$2" c_major c_minor c_patch o_major o_minor o_patch
  IFS=. read -r c_major c_minor c_patch <<<"$candidate"
  IFS=. read -r o_major o_minor o_patch <<<"$current"
  if ((10#$c_major != 10#$o_major)); then
    ((10#$c_major > 10#$o_major))
  elif ((10#$c_minor != 10#$o_minor)); then
    ((10#$c_minor > 10#$o_minor))
  else
    ((10#$c_patch > 10#$o_patch))
  fi
}

semver_same_release_line() {
  local candidate="$1" current="$2" c_major c_minor _ o_major o_minor
  IFS=. read -r c_major c_minor _ <<<"$candidate"
  IFS=. read -r o_major o_minor _ <<<"$current"
  [ "$c_major" = "$o_major" ] && [ "$c_minor" = "$o_minor" ]
}

target_container() {
  "$COMPOSE" ps -q ditto-subnet
}

assert_scoped_container() {
  local container="$1"
  [ "$(docker_container_label "$container" com.docker.compose.service)" = "ditto-subnet" ] || \
    die "target is not the ditto-subnet Compose service"
  [ "$(docker_container_label "$container" io.heyditto.validator.auto-update-target)" = "true" ] || \
    die "ditto-subnet is not explicitly labelled as an update target"
}

recover_quiescent_target() {
  local container state
  container="$(target_container)"
  [ -n "$container" ] || return 0
  assert_scoped_container "$container"
  state="$(runtime_state "$container")"
  if state_is_drained "$state"; then
    log "recovering a quiescent validator left by an interrupted update"
    resume_and_verify "$container" "$ready_timeout" "$check_seconds" || \
      die "quiescent validator could not be resumed"
  fi
}

runtime_state() {
  docker exec "$1" sh -c "cat '$RUNTIME_STATE_PATH'" 2>/dev/null || true
}

state_is_ready() {
  local state="$1"
  [[ "$state" == *\"compatibility_epoch\":$EXPECTED_COMPATIBILITY_EPOCH* ]] &&
    [[ "$state" == *\"heartbeat_protocol\":$EXPECTED_HEARTBEAT_PROTOCOL* ]] &&
    [[ "$state" == *'"platform_accepted":true'* ]] &&
    [[ "$state" == *\"update_protocol\":$EXPECTED_UPDATE_PROTOCOL* ]] &&
    { [[ "$state" == *'"state":"ready"'* ]] || [[ "$state" == *'"state":"working"'* ]]; }
}

state_is_drained() {
  local state="$1"
  [[ "$state" == *\"compatibility_epoch\":$EXPECTED_COMPATIBILITY_EPOCH* ]] &&
  [[ "$state" == *\"heartbeat_protocol\":$EXPECTED_HEARTBEAT_PROTOCOL* ]] &&
    [[ "$state" == *'"platform_accepted":true'* ]] &&
    [[ "$state" == *\"update_protocol\":$EXPECTED_UPDATE_PROTOCOL* ]] &&
    [[ "$state" == *'"state":"drained"'* ]]
}

resume_and_verify() {
  local container="$1" timeout="$2" check_seconds="$3" deadline state running resumed=false
  deadline=$((SECONDS + timeout))
  while ((SECONDS < deadline)); do
    running="$(docker inspect --format '{{ .State.Running }}' "$container" 2>/dev/null || true)"
    [ "$running" = "true" ] || return 1
    if docker kill --signal=USR2 "$container" >/dev/null 2>&1; then
      resumed=true
    fi
    state="$(runtime_state "$container")"
    if [ "$resumed" = "true" ] && state_is_ready "$state"; then
      return 0
    fi
    sleep "$check_seconds"
  done
  return 1
}

request_bounded_drain() {
  local container="$1" timeout="$2" check_seconds="$3" deadline state
  log "requesting a cooperative drain (timeout ${timeout}s)"
  # Track from signal delivery, not only from drain acknowledgement. TERM or a
  # power loss while the benchmark is still working must still cancel USR1.
  DRAINED_CONTAINER="$container"
  docker kill --signal=USR1 "$container" >/dev/null
  deadline=$((SECONDS + timeout))
  while ((SECONDS < deadline)); do
    state="$(runtime_state "$container")"
    if state_is_drained "$state"; then
      log "validator acknowledged drained state"
      DRAINED_CONTAINER="$container"
      return 0
    fi
    if [ "$(docker inspect --format '{{ .State.Running }}' "$container" 2>/dev/null || true)" != "true" ]; then
      die "validator exited before acknowledging a safe drain"
    fi
    sleep "$check_seconds"
  done
  log "drain deadline expired; cancelling the update and resuming the old validator"
  if ! resume_and_verify "$container" "$ready_timeout" "$check_seconds"; then
    die "could not verify that the timed-out validator resumed; inspect it immediately"
  fi
  DRAINED_CONTAINER=""
  return 75
}

deploy_only_validator() {
  local image="$1"
  DITTO_SUBNET_IMAGE="$image" VALIDATOR_START_DRAINED=true "$COMPOSE" up -d \
    --no-deps --no-build --pull never --force-recreate ditto-subnet
}

wait_until_quiescent_ready() {
  local expected_image_id="$1" timeout="$2" check_seconds="$3"
  local deadline container running actual_image state
  deadline=$((SECONDS + timeout))
  while ((SECONDS < deadline)); do
    container="$(target_container 2>/dev/null || true)"
    if [ -n "$container" ]; then
      running="$(docker inspect --format '{{ .State.Running }}' "$container" 2>/dev/null || true)"
      actual_image="$(docker inspect --format '{{ .Image }}' "$container" 2>/dev/null || true)"
      state="$(runtime_state "$container")"
      if [ "$running" = "true" ] && [ "$actual_image" = "$expected_image_id" ] && state_is_drained "$state"; then
        return 0
      fi
    fi
    sleep "$check_seconds"
  done
  return 1
}

record_success() {
  local previous="$1" current="$2" version="$3" revision="$4"
  mkdir -p "$STATE_DIR"
  umask 077
  {
    printf 'PREVIOUS_IMAGE=%s\n' "$previous"
    printf 'CURRENT_IMAGE=%s\n' "$current"
    printf 'CURRENT_VERSION=%s\n' "$version"
    printf 'CURRENT_REVISION=%s\n' "$revision"
  } >"$LAST_UPDATE_FILE"
}

record_transaction() {
  local phase="$1" previous="$2" previous_id="$3" current="$4" current_id="$5"
  local version="$6" revision="$7" temporary="$TRANSACTION_FILE.tmp"
  umask 077
  {
    printf 'PHASE=%s\n' "$phase"
    printf 'PREVIOUS_IMAGE=%s\n' "$previous"
    printf 'PREVIOUS_IMAGE_ID=%s\n' "$previous_id"
    printf 'CURRENT_IMAGE=%s\n' "$current"
    printf 'CURRENT_IMAGE_ID=%s\n' "$current_id"
    printf 'CURRENT_VERSION=%s\n' "$version"
    printf 'CURRENT_REVISION=%s\n' "$revision"
  } >"$temporary"
  mv "$temporary" "$TRANSACTION_FILE"
}

transaction_value() {
  local key="$1"
  awk -F= -v key="$key" '$1 == key { print substr($0, index($0, "=") + 1); exit }' \
    "$TRANSACTION_FILE"
}

ensure_recorded_image_resumed() {
  local image="$1" image_id="$2" container running actual state
  validate_release_image "$image" || die "recorded recovery image is unavailable or untrusted: $image"
  container="$(target_container 2>/dev/null || true)"
  if [ -n "$container" ]; then
    assert_scoped_container "$container"
    running="$(docker inspect --format '{{ .State.Running }}' "$container" 2>/dev/null || true)"
    actual="$(docker inspect --format '{{ .Image }}' "$container" 2>/dev/null || true)"
    if [ "$running" = "true" ] && [ "$actual" = "$image_id" ]; then
      state="$(runtime_state "$container")"
      state_is_ready "$state" && return 0
      if state_is_drained "$state"; then
        resume_and_verify "$container" "$ready_timeout" "$check_seconds" && return 0
      fi
    fi
  fi
  deploy_only_validator "$image"
  wait_until_quiescent_ready "$image_id" "$ready_timeout" "$check_seconds" || \
    die "recorded recovery image did not become quiescent and healthy: $image"
  container="$(target_container)"
  resume_and_verify "$container" "$ready_timeout" "$check_seconds" || \
    die "recorded recovery image could not be resumed: $image"
}

cancel_prepared_drain() {
  local image="$1" image_id="$2" container running actual
  container="$(target_container 2>/dev/null || true)"
  if [ -n "$container" ]; then
    assert_scoped_container "$container"
    running="$(docker inspect --format '{{ .State.Running }}' "$container" 2>/dev/null || true)"
    actual="$(docker inspect --format '{{ .Image }}' "$container" 2>/dev/null || true)"
    if [ "$running" = "true" ] && [ "$actual" = "$image_id" ]; then
      # Always deliver USR2. A pre-ack worker may still report working even
      # though its in-memory drain Event is set.
      resume_and_verify "$container" "$ready_timeout" "$check_seconds" || \
        die "prepared transaction drain could not be cancelled"
      return 0
    fi
  fi
  ensure_recorded_image_resumed "$image" "$image_id"
}

recover_interrupted_transaction() {
  local phase previous previous_id current current_id version revision
  [ -f "$TRANSACTION_FILE" ] || return 0
  phase="$(transaction_value PHASE)"
  previous="$(transaction_value PREVIOUS_IMAGE)"
  previous_id="$(transaction_value PREVIOUS_IMAGE_ID)"
  current="$(transaction_value CURRENT_IMAGE)"
  current_id="$(transaction_value CURRENT_IMAGE_ID)"
  version="$(transaction_value CURRENT_VERSION)"
  revision="$(transaction_value CURRENT_REVISION)"
  [ -n "$phase" ] && [ -n "$previous" ] && [ -n "$previous_id" ] && \
    [ -n "$current" ] && [ -n "$current_id" ] || \
    die "update transaction journal is incomplete; refusing automatic recovery"
  log "recovering interrupted update transaction (phase $phase)"
  case "$phase" in
    prepared)
      cancel_prepared_drain "$previous" "$previous_id"
      ;;
    stopped | candidate_ready)
      ensure_recorded_image_resumed "$previous" "$previous_id"
      ;;
    rollback_ready)
      ensure_recorded_image_resumed "$previous" "$previous_id"
      printf '%s\n' "$current" >"$FAILED_CANDIDATE_FILE"
      ;;
    committed)
      [ -n "$version" ] && [ -n "$revision" ] || \
        die "committed update journal lacks release identity"
      ensure_recorded_image_resumed "$current" "$current_id"
      record_success "$previous" "$current" "$version" "$revision"
      ;;
    *) die "unknown update transaction phase: $phase" ;;
  esac
  rm -f "$TRANSACTION_FILE"
}

perform_replacement() {
  local candidate_ref="$1" allow_downgrade="$2" drain_timeout="$3" ready_timeout="$4" check_seconds="$5"
  local container current_state current_image_id current_version candidate_image_id candidate_version
  local candidate_revision rollback_ref short_id

  container="$(target_container)"
  [ -n "$container" ] || die "ditto-subnet is not running"
  assert_scoped_container "$container"

  current_state="$(runtime_state "$container")"
  if state_is_drained "$current_state"; then
    log "recovering a previously committed quiescent validator before checking updates"
    resume_and_verify "$container" "$ready_timeout" "$check_seconds" || \
      die "quiescent validator could not be resumed"
    current_state="$(runtime_state "$container")"
  fi
  if ! state_is_ready "$current_state"; then
    log "validator has not published an operational, platform-accepted state; deferring update"
    return 0
  fi

  current_image_id="$(docker inspect --format '{{ .Image }}' "$container")"
  if ! validate_release_image "$current_image_id"; then
    die "running validator is a local/legacy build without trusted update metadata; manually migrate once with the registry image before enabling automatic updates"
  fi
  validate_release_image "$candidate_ref" || die "candidate image metadata is missing or incompatible"

  current_version="$(docker_image_label "$current_image_id" org.opencontainers.image.version)"
  candidate_image_id="$(docker image inspect --format '{{ .Id }}' "$candidate_ref")"
  candidate_version="$(docker_image_label "$candidate_ref" org.opencontainers.image.version)"
  candidate_revision="$(docker_image_label "$candidate_ref" org.opencontainers.image.revision)"

  if [ "$candidate_image_id" = "$current_image_id" ]; then
    log "already running validator $current_version ($current_image_id)"
    return 0
  fi
  if [ "$allow_downgrade" != "true" ] && ! semver_same_release_line "$candidate_version" "$current_version"; then
    die "candidate $candidate_version crosses the running $current_version major/minor release line; supervised migration required"
  fi
  if [ "$allow_downgrade" != "true" ] && ! semver_greater "$candidate_version" "$current_version"; then
    die "candidate $candidate_version is not newer than running $current_version; refusing mutable-tag replacement or downgrade"
  fi

  short_id="${current_image_id#sha256:}"
  rollback_ref="ditto-subnet-validator-rollback:${current_version//./-}-${short_id:0:12}"
  # Prepare rollback before asking the live process to drain. Any failure here
  # leaves it working normally.
  docker image tag "$current_image_id" "$rollback_ref"
  ROLLBACK_REF="$rollback_ref"
  ROLLBACK_IMAGE_ID="$current_image_id"
  record_transaction prepared "$rollback_ref" "$current_image_id" \
    "$candidate_ref" "$candidate_image_id" "$candidate_version" "$candidate_revision"

  if ! request_bounded_drain "$container" "$drain_timeout" "$check_seconds"; then
    rm -f "$TRANSACTION_FILE"
    return 0
  fi

  OLD_CONTAINER_STOPPED=true
  if ! docker stop --time 30 "$container" >/dev/null; then
    die "could not stop the drained validator"
  fi
  DRAINED_CONTAINER=""
  record_transaction stopped "$rollback_ref" "$current_image_id" \
    "$candidate_ref" "$candidate_image_id" "$candidate_version" "$candidate_revision"

  log "deploying only ditto-subnet as $candidate_ref"
  if ! deploy_only_validator "$candidate_ref" || ! wait_until_quiescent_ready "$candidate_image_id" "$ready_timeout" "$check_seconds"; then
    log "candidate failed readiness; restoring $rollback_ref"
    if ! deploy_only_validator "$rollback_ref" || ! wait_until_quiescent_ready "$current_image_id" "$ready_timeout" "$check_seconds"; then
      die "candidate and automatic rollback both failed; inspect Compose logs immediately"
    fi
    record_transaction rollback_ready "$rollback_ref" "$current_image_id" \
      "$candidate_ref" "$candidate_image_id" "$candidate_version" "$candidate_revision"
    OLD_CONTAINER_STOPPED=false
    TRANSACTION_COMPLETE=true
    container="$(target_container)"
    DRAINED_CONTAINER="$container"
    resume_and_verify "$container" "$ready_timeout" "$check_seconds" || \
      die "previous image was restored quiescently but could not be resumed"
    DRAINED_CONTAINER=""
    if [ "$allow_downgrade" != "true" ]; then
      printf '%s\n' "$candidate_ref" >"$FAILED_CANDIDATE_FILE"
    fi
    rm -f "$TRANSACTION_FILE"
    die "candidate failed readiness and the previous image was restored"
  fi

  record_transaction candidate_ready "$rollback_ref" "$current_image_id" \
    "$candidate_ref" "$candidate_image_id" "$candidate_version" "$candidate_revision"
  record_transaction committed "$rollback_ref" "$current_image_id" \
    "$candidate_ref" "$candidate_image_id" "$candidate_version" "$candidate_revision"
  record_success "$rollback_ref" "$candidate_ref" "$candidate_version" "$candidate_revision"
  OLD_CONTAINER_STOPPED=false
  TRANSACTION_COMPLETE=true
  container="$(target_container)"
  DRAINED_CONTAINER="$container"
  resume_and_verify "$container" "$ready_timeout" "$check_seconds" || \
    die "candidate was committed quiescently but could not be resumed"
  DRAINED_CONTAINER=""
  rm -f "$TRANSACTION_FILE"
  log "updated validator $current_version -> $candidate_version; retained rollback image $rollback_ref"
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  set +e
  if [ -n "$DRAINED_CONTAINER" ]; then
    log "interrupted after drain; resuming the original validator"
    if resume_and_verify "$DRAINED_CONTAINER" "$CLEANUP_READY_TIMEOUT" "$CLEANUP_CHECK_SECONDS"; then
      if [ "$(docker inspect --format '{{ .State.Running }}' "$DRAINED_CONTAINER" 2>/dev/null || true)" = "true" ]; then
        # docker stop can fail without stopping the process. Once resume is
        # verified, recreating it would introduce the interruption we avoided.
        OLD_CONTAINER_STOPPED=false
        if [ -f "$TRANSACTION_FILE" ] && \
          [ "$(transaction_value PHASE)" = "prepared" ]; then
          rm -f "$TRANSACTION_FILE"
        fi
      fi
    else
      log "CRITICAL: could not verify resume for $DRAINED_CONTAINER"
    fi
  fi
  if [ "$OLD_CONTAINER_STOPPED" = "true" ] && [ "$TRANSACTION_COMPLETE" != "true" ]; then
    log "interrupted during replacement; restoring $ROLLBACK_REF"
    if ! deploy_only_validator "$ROLLBACK_REF" || \
      ! wait_until_quiescent_ready "$ROLLBACK_IMAGE_ID" "$CLEANUP_READY_TIMEOUT" "$CLEANUP_CHECK_SECONDS"; then
      log "CRITICAL: interrupted replacement rollback failed; inspect Compose immediately"
    else
      OLD_CONTAINER_STOPPED=false
      TRANSACTION_COMPLETE=true
      restored_container="$(target_container 2>/dev/null || true)"
      if [ -z "$restored_container" ] || \
        ! resume_and_verify "$restored_container" "$CLEANUP_READY_TIMEOUT" "$CLEANUP_CHECK_SECONDS"; then
        log "CRITICAL: restored validator is quiescent but could not be resumed"
      fi
    fi
  fi
  if [ "$LOCK_HELD" = "true" ]; then
    rm -f "$LOCK_DIR/pid"
    rmdir "$LOCK_DIR" 2>/dev/null || true
  fi
  exit "$status"
}

handle_interrupt() {
  log "received termination request; running transaction cleanup"
  exit 143
}

acquire_lock() {
  local old_pid command
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    printf '%s\n' "$$" >"$LOCK_DIR/pid"
    LOCK_HELD=true
    return 0
  fi
  old_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [[ "$old_pid" =~ ^[1-9][0-9]*$ ]]; then
    command="$(ps -p "$old_pid" -o command= 2>/dev/null || true)"
    if [[ "$command" == *validator-auto-update.sh* ]]; then
      die "another validator update operation is already running (pid $old_pid)"
    fi
  fi
  log "removing stale updater lock"
  rm -f "$LOCK_DIR/pid"
  rmdir "$LOCK_DIR" 2>/dev/null || die "update lock exists but is not recoverable"
  mkdir "$LOCK_DIR"
  printf '%s\n' "$$" >"$LOCK_DIR/pid"
  LOCK_HELD=true
}

show_status() {
  local enabled container image_id version revision state
  enabled="$(setting VALIDATOR_AUTO_UPDATE false)"
  printf 'enabled=%s\n' "$enabled"
  printf 'channel=%s\n' "$CANDIDATE_CHANNEL"
  container="$(target_container 2>/dev/null || true)"
  if [ -z "$container" ]; then
    printf 'container=not-running\n'
    return 0
  fi
  image_id="$(docker inspect --format '{{ .Image }}' "$container")"
  version="$(docker_image_label "$image_id" org.opencontainers.image.version)"
  revision="$(docker_image_label "$image_id" org.opencontainers.image.revision)"
  state="$(runtime_state "$container")"
  printf 'container=%s\nimage=%s\nversion=%s\nrevision=%s\nruntime_state=%s\n' \
    "$container" "$image_id" "${version:-unknown}" "${revision:-unknown}" "${state:-unavailable}"
  [ ! -f "$LAST_UPDATE_FILE" ] || cat "$LAST_UPDATE_FILE"
  [ ! -f "$FAILED_CANDIDATE_FILE" ] || \
    printf 'FAILED_CANDIDATE=%s\n' "$(cat "$FAILED_CANDIDATE_FILE")"
}

mode="${1:-run}"
case "$mode" in
  run | rollback | status) ;;
  *) die "usage: $0 [run|status|rollback]" ;;
esac

command -v docker >/dev/null 2>&1 || die "Docker is not installed"
[ -x "$COMPOSE" ] || die "validator Compose wrapper is not executable"

if [ "$mode" = "status" ]; then
  show_status
  exit 0
fi

mkdir -p "$STATE_DIR"
acquire_lock
trap cleanup EXIT
trap handle_interrupt INT TERM

drain_timeout="$(setting VALIDATOR_AUTO_UPDATE_DRAIN_TIMEOUT_SECONDS 4800)"
ready_timeout="$(setting VALIDATOR_AUTO_UPDATE_READY_TIMEOUT_SECONDS 180)"
check_seconds="$(setting VALIDATOR_AUTO_UPDATE_CHECK_SECONDS 5)"
require_positive_integer VALIDATOR_AUTO_UPDATE_DRAIN_TIMEOUT_SECONDS "$drain_timeout"
require_positive_integer VALIDATOR_AUTO_UPDATE_READY_TIMEOUT_SECONDS "$ready_timeout"
require_positive_integer VALIDATOR_AUTO_UPDATE_CHECK_SECONDS "$check_seconds"
CLEANUP_READY_TIMEOUT="$ready_timeout"
CLEANUP_CHECK_SECONDS="$check_seconds"

if [ "$mode" = "rollback" ]; then
  if is_true "$(setting VALIDATOR_AUTO_UPDATE false)"; then
    die "set VALIDATOR_AUTO_UPDATE=false and stop the timer before manual rollback"
  fi
  recover_interrupted_transaction
  [ -f "$LAST_UPDATE_FILE" ] || die "no retained rollback image has been recorded"
  rollback_image="$(awk -F= '$1 == "PREVIOUS_IMAGE" { print substr($0, index($0, "=") + 1) }' "$LAST_UPDATE_FILE")"
  [ -n "$rollback_image" ] || die "recorded rollback image is empty"
  perform_replacement "$rollback_image" true "$drain_timeout" "$ready_timeout" "$check_seconds"
  exit 0
fi

if ! is_true "$(setting VALIDATOR_AUTO_UPDATE false)"; then
  log "disabled (set VALIDATOR_AUTO_UPDATE=true to opt in)"
  exit 0
fi

recover_interrupted_transaction
recover_quiescent_target
log "checking $CANDIDATE_CHANNEL"
docker pull "$CANDIDATE_CHANNEL" >/dev/null
candidate_digest_ref="$(
  docker image inspect --format '{{ range .RepoDigests }}{{ println . }}{{ end }}' \
    "$CANDIDATE_CHANNEL" | awk -v prefix="$IMAGE_REPOSITORY@" 'index($0, prefix) == 1 { print; exit }'
)"
[ -n "$candidate_digest_ref" ] || die "candidate did not resolve to an immutable registry digest"
if [ -f "$FAILED_CANDIDATE_FILE" ] && [ "$(cat "$FAILED_CANDIDATE_FILE")" = "$candidate_digest_ref" ]; then
  log "candidate is suppressed after a failed readiness rollback; waiting for the compatibility channel to advance"
  exit 0
fi
perform_replacement "$candidate_digest_ref" false "$drain_timeout" "$ready_timeout" "$check_seconds"
rm -f "$FAILED_CANDIDATE_FILE"
