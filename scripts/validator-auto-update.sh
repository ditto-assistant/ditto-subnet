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
MANAGED_IMAGE_FILE="$STATE_DIR/managed-image.env"
LOCK_FILE="$STATE_DIR/lock"
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
RESUME_SIGNAL_DELIVERED=false
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

is_registry_digest() {
  [[ "$1" =~ ^ghcr\.io/ditto-assistant/ditto-subnet-validator@sha256:[0-9a-f]{64}$ ]]
}

require_positive_integer() {
  local name="$1" value="$2"
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || die "$name must be a positive integer"
}

show_timeout_budget() {
  local check_seconds drain_timeout ready_timeout
  drain_timeout="$(setting VALIDATOR_AUTO_UPDATE_DRAIN_TIMEOUT_SECONDS 4800)"
  ready_timeout="$(setting VALIDATOR_AUTO_UPDATE_READY_TIMEOUT_SECONDS 180)"
  check_seconds="$(setting VALIDATOR_AUTO_UPDATE_CHECK_SECONDS 5)"
  require_positive_integer VALIDATOR_AUTO_UPDATE_DRAIN_TIMEOUT_SECONDS "$drain_timeout"
  require_positive_integer VALIDATOR_AUTO_UPDATE_READY_TIMEOUT_SECONDS "$ready_timeout"
  require_positive_integer VALIDATOR_AUTO_UPDATE_CHECK_SECONDS "$check_seconds"

  # Start covers the drain, candidate verification, automatic rollback, and a
  # conservative extra readiness window. Stop covers cleanup's direct resume
  # plus rollback readiness and rollback resume, with bounded Docker overhead.
  printf 'DRAIN_TIMEOUT_SECONDS=%s\n' "$drain_timeout"
  printf 'READY_TIMEOUT_SECONDS=%s\n' "$ready_timeout"
  printf 'CHECK_SECONDS=%s\n' "$check_seconds"
  printf 'TIMEOUT_START_SECONDS=%d\n' \
    "$((drain_timeout + 6 * ready_timeout + 6 * check_seconds + 600))"
  printf 'TIMEOUT_STOP_SECONDS=%d\n' \
    "$((3 * ready_timeout + 3 * check_seconds + 300))"
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
  local container state resume_status
  container="$(target_container)"
  [ -n "$container" ] || return 0
  assert_scoped_container "$container"
  state="$(runtime_state "$container")"
  if state_is_drained "$state"; then
    log "recovering a quiescent validator left by an interrupted update"
    DRAINED_CONTAINER="$container"
    if resume_and_verify "$container" "$ready_timeout" "$check_seconds"; then
      DRAINED_CONTAINER=""
    else
      resume_status=$?
      if [ "$resume_status" -eq 76 ]; then
        DRAINED_CONTAINER=""
        die "quiescent validator may be working after USR2; readiness is unverified"
      fi
      die "quiescent validator could not be resumed"
    fi
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
  local container="$1" timeout="$2" check_seconds="$3" deadline state running
  local initial_state any_signal_delivered=false
  RESUME_SIGNAL_DELIVERED=false
  initial_state="$(runtime_state "$container")"
  deadline=$((SECONDS + timeout))
  while ((SECONDS < deadline)); do
    running="$(docker inspect --format '{{ .State.Running }}' "$container" 2>/dev/null || true)"
    if [ "$running" != "true" ]; then
      if [ "$any_signal_delivered" = "true" ] || \
        [ "$RESUME_SIGNAL_DELIVERED" = "true" ]; then
        return 76
      fi
      return 1
    fi
    # Treat the signal as ambiguous before entering the subprocess. Bash may
    # dispatch TERM after Docker delivered USR2 but before this command returns;
    # cleanup must already know that recreating could interrupt resumed work.
    RESUME_SIGNAL_DELIVERED=true
    if docker kill --signal=USR2 "$container" >/dev/null 2>&1; then
      any_signal_delivered=true
    fi
    state="$(runtime_state "$container")"
    if state_is_ready "$state" && \
      { [ "$any_signal_delivered" = "true" ] || state_is_drained "$initial_state"; }; then
      # A transition from drained to ready is positive proof that USR2 took
      # effect even if the Docker CLI lost the daemon response and exited
      # nonzero. Never recreate after that observed transition.
      RESUME_SIGNAL_DELIVERED=true
      return 0
    fi
    sleep "$check_seconds"
  done
  state="$(runtime_state "$container")"
  running="$(docker inspect --format '{{ .State.Running }}' "$container" 2>/dev/null || true)"
  if [ "$any_signal_delivered" = "false" ] && [ "$running" = "true" ] && \
    state_is_drained "$state"; then
    # The container remained explicitly quiescent for the full bounded window;
    # this is the only positive proof that failed Docker calls did not resume it.
    RESUME_SIGNAL_DELIVERED=false
    return 1
  fi
  if [ "$RESUME_SIGNAL_DELIVERED" = "true" ]; then
    # Work may already have resumed even if the updater-visible state could not
    # be refreshed. Callers must not recreate this container without a new,
    # explicit drained acknowledgement.
    return 76
  fi
  return 1
}

request_bounded_drain() {
  local container="$1" timeout="$2" check_seconds="$3" deadline state
  log "requesting a cooperative drain (timeout ${timeout}s)"
  # Track from signal delivery, not only from drain acknowledgement. TERM or a
  # power loss while the benchmark is still working must still cancel USR1.
  DRAINED_CONTAINER="$container"
  if ! docker kill --signal=USR1 "$container" >/dev/null; then
    die "could not request cooperative drain from validator"
  fi
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
  local image="$1" bootstrap_token
  bootstrap_token="$(od -An -N16 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n')"
  [[ "$bootstrap_token" =~ ^[0-9a-f]{32}$ ]] || \
    die "could not generate a 128-bit validator bootstrap token"
  DITTO_ALLOW_MANAGED_VALIDATOR_MUTATION=true \
    DITTO_SUBNET_IMAGE="$image" \
    VALIDATOR_BOOTSTRAP_TOKEN="$bootstrap_token" \
    VALIDATOR_START_DRAINED=true "$COMPOSE" up -d \
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
  record_managed_image "$current"
}

record_managed_image() {
  local image="$1" temporary="$MANAGED_IMAGE_FILE.tmp"
  if ! is_registry_digest "$image" &&
    [[ ! "$image" =~ ^ditto-subnet-validator-rollback:[0-9a-z-]+$ ]]; then
    die "refusing to persist a mutable or unscoped managed image: $image"
  fi
  mkdir -p "$STATE_DIR"
  umask 077
  printf 'DITTO_SUBNET_IMAGE=%s\n' "$image" >"$temporary"
  mv "$temporary" "$MANAGED_IMAGE_FILE"
}

managed_image_ref() {
  local line image
  [ -f "$MANAGED_IMAGE_FILE" ] || \
    die "managed validator mode is not adopted; follow the supervised registry migration"
  [ "$(awk 'NF { count++ } END { print count + 0 }' "$MANAGED_IMAGE_FILE")" -eq 1 ] || \
    die "managed image state must contain exactly one non-empty line"
  line="$(awk 'NF { print; exit }' "$MANAGED_IMAGE_FILE")"
  case "$line" in
    DITTO_SUBNET_IMAGE=*) image="${line#DITTO_SUBNET_IMAGE=}" ;;
    *) die "managed image state is malformed" ;;
  esac
  if ! is_registry_digest "$image" &&
    [[ ! "$image" =~ ^ditto-subnet-validator-rollback:[0-9a-z-]+$ ]]; then
    die "managed image state contains an unsafe image reference"
  fi
  printf '%s' "$image"
}

adopt_running_image() {
  local image="$1" expected_image_id container actual_image_id state
  if is_true "$(setting VALIDATOR_AUTO_UPDATE false)"; then
    die "set VALIDATOR_AUTO_UPDATE=false and stop the timer before adopting an image"
  fi
  is_registry_digest "$image" || \
    die "adopt requires an immutable $IMAGE_REPOSITORY registry digest"
  [ ! -f "$TRANSACTION_FILE" ] || \
    die "an update transaction is pending; recover it before adopting managed mode"

  docker pull "$image" >/dev/null
  validate_release_image "$image" || \
    die "adopted image metadata is missing or incompatible"
  expected_image_id="$(docker image inspect --format '{{ .Id }}' "$image")"
  container="$(target_container)"
  [ -n "$container" ] || die "ditto-subnet is not running"
  assert_scoped_container "$container"
  actual_image_id="$(docker inspect --format '{{ .Image }}' "$container")"
  [ "$actual_image_id" = "$expected_image_id" ] || \
    die "running validator does not match the requested immutable digest"
  state="$(runtime_state "$container")"
  state_is_ready "$state" || \
    die "running validator is not operational and freshly platform-accepted"
  record_managed_image "$image"
  log "adopted managed validator image $image"
}

record_transaction() {
  local phase="$1" previous="$2" previous_id="$3" current="$4" current_id="$5"
  local version="$6" revision="$7" suppress_candidate="${8:-true}"
  local temporary="$TRANSACTION_FILE.tmp"
  umask 077
  {
    printf 'PHASE=%s\n' "$phase"
    printf 'PREVIOUS_IMAGE=%s\n' "$previous"
    printf 'PREVIOUS_IMAGE_ID=%s\n' "$previous_id"
    printf 'CURRENT_IMAGE=%s\n' "$current"
    printf 'CURRENT_IMAGE_ID=%s\n' "$current_id"
    printf 'CURRENT_VERSION=%s\n' "$version"
    printf 'CURRENT_REVISION=%s\n' "$revision"
    printf 'SUPPRESS_CANDIDATE=%s\n' "$suppress_candidate"
  } >"$temporary"
  mv "$temporary" "$TRANSACTION_FILE"
}

transaction_value() {
  local key="$1"
  awk -F= -v key="$key" '$1 == key { print substr($0, index($0, "=") + 1); exit }' \
    "$TRANSACTION_FILE"
}

resume_existing_recorded_image() {
  local image="$1" image_id="$2" timeout="$3" interval="$4"
  local container running actual state resume_status
  validate_release_image "$image" || die "recorded recovery image is unavailable or untrusted: $image"
  container="$(target_container 2>/dev/null || true)"
  [ -n "$container" ] || return 1
  assert_scoped_container "$container"
  running="$(docker inspect --format '{{ .State.Running }}' "$container" 2>/dev/null || true)"
  actual="$(docker inspect --format '{{ .Image }}' "$container" 2>/dev/null || true)"
  [ "$running" = "true" ] || return 1
  state="$(runtime_state "$container")"
  if [ "$actual" = "$image_id" ]; then
    state_is_ready "$state" && return 0
    if state_is_drained "$state"; then
      if resume_and_verify "$container" "$timeout" "$interval"; then
        return 0
      else
        resume_status=$?
      fi
      return "$resume_status"
    fi
    # A running recorded image with unavailable or transitional state may
    # already own work. It is not safe to recreate without a drained proof.
    return 77
  fi
  # A different running image is replaceable only when it explicitly proves
  # quiescence. This covers candidate_ready and rollback_pending recovery.
  state_is_drained "$state" && return 1
  return 77
}

ensure_recorded_image_resumed() {
  local image="$1" image_id="$2" container resume_status
  if resume_existing_recorded_image \
    "$image" "$image_id" "$ready_timeout" "$check_seconds"; then
    return 0
  else
    resume_status=$?
  fi
  if [ "$resume_status" -eq 76 ] || [ "$resume_status" -eq 77 ]; then
    return "$resume_status"
  fi
  deploy_only_validator "$image"
  wait_until_quiescent_ready "$image_id" "$ready_timeout" "$check_seconds" || \
    die "recorded recovery image did not become quiescent and healthy: $image"
  container="$(target_container)"
  if resume_and_verify "$container" "$ready_timeout" "$check_seconds"; then
    return 0
  else
    resume_status=$?
  fi
  return "$resume_status"
}

resume_or_restore_prepared_image() {
  local image="$1" image_id="$2" container running actual resume_status
  if cancel_prepared_drain "$image" "$image_id"; then
    return 0
  else
    resume_status=$?
  fi
  if [ "$resume_status" -eq 76 ] || [ "$resume_status" -eq 77 ] || \
    [ "$RESUME_SIGNAL_DELIVERED" = "true" ]; then
    return "$resume_status"
  fi

  # A crash can occur after Docker stopped the old container but before the
  # stopped journal phase was persisted. Recreate only when the target is
  # absent or is the recorded old image and Docker proves it is stopped.
  validate_release_image "$image" || \
    die "recorded recovery image is unavailable or untrusted: $image"
  container="$(target_container 2>/dev/null || true)"
  if [ -n "$container" ]; then
    assert_scoped_container "$container"
    running="$(docker inspect --format '{{ .State.Running }}' "$container" 2>/dev/null || true)"
    actual="$(docker inspect --format '{{ .Image }}' "$container" 2>/dev/null || true)"
    if [ "$running" = "true" ] || [ "$actual" != "$image_id" ]; then
      return 77
    fi
  fi
  deploy_only_validator "$image"
  wait_until_quiescent_ready "$image_id" "$ready_timeout" "$check_seconds" || return 1
  container="$(target_container)"
  DRAINED_CONTAINER="$container"
  if resume_and_verify "$container" "$ready_timeout" "$check_seconds"; then
    DRAINED_CONTAINER=""
    return 0
  else
    resume_status=$?
  fi
  return "$resume_status"
}

cancel_prepared_drain() {
  local image="$1" image_id="$2" container running actual resume_status
  container="$(target_container 2>/dev/null || true)"
  if [ -n "$container" ]; then
    assert_scoped_container "$container"
    running="$(docker inspect --format '{{ .State.Running }}' "$container" 2>/dev/null || true)"
    actual="$(docker inspect --format '{{ .Image }}' "$container" 2>/dev/null || true)"
    if [ "$running" = "true" ] && [ "$actual" = "$image_id" ]; then
      # Always deliver USR2. A pre-ack worker may still report working even
      # though its in-memory drain Event is set.
      if resume_and_verify "$container" "$ready_timeout" "$check_seconds"; then
        return 0
      else
        resume_status=$?
      fi
      return "$resume_status"
    fi
  fi
  return 1
}

recover_interrupted_transaction() {
  local phase previous previous_id current current_id version revision suppress_candidate
  local resume_status
  [ -f "$TRANSACTION_FILE" ] || return 0
  phase="$(transaction_value PHASE)"
  previous="$(transaction_value PREVIOUS_IMAGE)"
  previous_id="$(transaction_value PREVIOUS_IMAGE_ID)"
  current="$(transaction_value CURRENT_IMAGE)"
  current_id="$(transaction_value CURRENT_IMAGE_ID)"
  version="$(transaction_value CURRENT_VERSION)"
  revision="$(transaction_value CURRENT_REVISION)"
  suppress_candidate="$(transaction_value SUPPRESS_CANDIDATE)"
  suppress_candidate="${suppress_candidate:-true}"
  [ -n "$phase" ] && [ -n "$previous" ] && [ -n "$previous_id" ] && \
    [ -n "$current" ] && [ -n "$current_id" ] || \
    die "update transaction journal is incomplete; refusing automatic recovery"
  log "recovering interrupted update transaction (phase $phase)"
  case "$phase" in
    prepared)
      if ! resume_or_restore_prepared_image "$previous" "$previous_id"; then
        die "prepared validator could not be safely recovered; inspect status before retrying"
      fi
      ;;
    stopped | candidate_ready)
      if ! ensure_recorded_image_resumed "$previous" "$previous_id"; then
        die "uncommitted validator may be working; refusing recreation without a quiescent proof"
      fi
      ;;
    rollback_pending | rollback_ready)
      if ! ensure_recorded_image_resumed "$previous" "$previous_id"; then
        die "rollback validator may be working; refusing recreation without a quiescent proof"
      fi
      if [ "$suppress_candidate" = "true" ]; then
        printf '%s\n' "$current" >"$FAILED_CANDIDATE_FILE"
      fi
      ;;
    committed)
      [ -n "$version" ] && [ -n "$revision" ] || \
        die "committed update journal lacks release identity"
      if resume_existing_recorded_image \
        "$current" "$current_id" "$ready_timeout" "$check_seconds"; then
        record_success "$previous" "$current" "$version" "$revision"
      else
        resume_status=$?
        if [ "$resume_status" -eq 76 ] || [ "$resume_status" -eq 77 ] || \
          [ "$RESUME_SIGNAL_DELIVERED" = "true" ]; then
          die "committed candidate may be working; refusing rollback without a new drain acknowledgement"
        fi
        log "committed candidate could not resume; restoring the previous image"
        record_transaction rollback_pending "$previous" "$previous_id" \
          "$current" "$current_id" "$version" "$revision" "$suppress_candidate"
        ensure_recorded_image_resumed "$previous" "$previous_id"
        if [ "$suppress_candidate" = "true" ]; then
          printf '%s\n' "$current" >"$FAILED_CANDIDATE_FILE"
        fi
      fi
      ;;
    *) die "unknown update transaction phase: $phase" ;;
  esac
  rm -f "$TRANSACTION_FILE"
}

perform_replacement() {
  local candidate_ref="$1" allow_downgrade="$2" drain_timeout="$3" ready_timeout="$4" check_seconds="$5"
  local container current_state current_image_id current_version candidate_image_id candidate_version
  local candidate_revision rollback_ref short_id suppress_candidate=true
  local managed_ref managed_image_id resume_status

  if [ "$allow_downgrade" = "true" ]; then
    suppress_candidate=false
  fi

  container="$(target_container)"
  [ -n "$container" ] || die "ditto-subnet is not running"
  assert_scoped_container "$container"

  current_state="$(runtime_state "$container")"
  if state_is_drained "$current_state"; then
    log "recovering a previously committed quiescent validator before checking updates"
    DRAINED_CONTAINER="$container"
    if resume_and_verify "$container" "$ready_timeout" "$check_seconds"; then
      DRAINED_CONTAINER=""
    else
      resume_status=$?
      if [ "$resume_status" -eq 76 ]; then
        DRAINED_CONTAINER=""
        die "quiescent validator may be working after USR2; readiness is unverified"
      fi
      die "quiescent validator could not be resumed"
    fi
    current_state="$(runtime_state "$container")"
  fi
  if ! state_is_ready "$current_state"; then
    log "validator has not published an operational, platform-accepted state; deferring update"
    return 0
  fi

  current_image_id="$(docker inspect --format '{{ .Image }}' "$container")"
  managed_ref="$(managed_image_ref)"
  managed_image_id="$(docker image inspect --format '{{ .Id }}' "$managed_ref" 2>/dev/null || true)"
  [ -n "$managed_image_id" ] && [ "$managed_image_id" = "$current_image_id" ] || \
    die "running validator does not match persisted managed-image state"
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
    "$candidate_ref" "$candidate_image_id" "$candidate_version" "$candidate_revision" \
    "$suppress_candidate"

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
    "$candidate_ref" "$candidate_image_id" "$candidate_version" "$candidate_revision" \
    "$suppress_candidate"

  log "deploying only ditto-subnet as $candidate_ref"
  if ! deploy_only_validator "$candidate_ref" || ! wait_until_quiescent_ready "$candidate_image_id" "$ready_timeout" "$check_seconds"; then
    log "candidate failed readiness; restoring $rollback_ref"
    if ! deploy_only_validator "$rollback_ref" || ! wait_until_quiescent_ready "$current_image_id" "$ready_timeout" "$check_seconds"; then
      die "candidate and automatic rollback both failed; inspect Compose logs immediately"
    fi
    record_transaction rollback_ready "$rollback_ref" "$current_image_id" \
      "$candidate_ref" "$candidate_image_id" "$candidate_version" "$candidate_revision" \
      "$suppress_candidate"
    container="$(target_container)"
    DRAINED_CONTAINER="$container"
    if resume_and_verify "$container" "$ready_timeout" "$check_seconds"; then
      resume_status=0
    else
      resume_status=$?
    fi
    if [ "$resume_status" -ne 0 ]; then
      if [ "$resume_status" -eq 76 ]; then
        die "previous image may be working after USR2; leaving rollback journal for verified recovery"
      fi
      die "previous image was restored quiescently but could not be resumed"
    fi
    if [ "$allow_downgrade" != "true" ]; then
      printf '%s\n' "$candidate_ref" >"$FAILED_CANDIDATE_FILE"
    fi
    OLD_CONTAINER_STOPPED=false
    TRANSACTION_COMPLETE=true
    DRAINED_CONTAINER=""
    rm -f "$TRANSACTION_FILE"
    die "candidate failed readiness and the previous image was restored"
  fi

  record_transaction candidate_ready "$rollback_ref" "$current_image_id" \
    "$candidate_ref" "$candidate_image_id" "$candidate_version" "$candidate_revision" \
    "$suppress_candidate"
  record_transaction committed "$rollback_ref" "$current_image_id" \
    "$candidate_ref" "$candidate_image_id" "$candidate_version" "$candidate_revision" \
    "$suppress_candidate"
  container="$(target_container)"
  DRAINED_CONTAINER="$container"
  if resume_and_verify "$container" "$ready_timeout" "$check_seconds"; then
    resume_status=0
  else
    resume_status=$?
  fi
  if [ "$resume_status" -ne 0 ]; then
    if [ "$resume_status" -eq 76 ]; then
      # USR2 can allow work before updater-visible state refreshes. Preserve
      # the committed journal and never recreate without another safe drain.
      OLD_CONTAINER_STOPPED=false
      TRANSACTION_COMPLETE=true
      DRAINED_CONTAINER=""
      die "candidate may be working after USR2; refusing rollback without a new drain acknowledgement"
    fi
    # Never leave an unresumable candidate recorded as committed. Persist the
    # rollback decision before cleanup recreates the previous image.
    record_transaction rollback_pending "$rollback_ref" "$current_image_id" \
      "$candidate_ref" "$candidate_image_id" "$candidate_version" \
      "$candidate_revision" "$suppress_candidate"
    DRAINED_CONTAINER=""
    die "candidate was committed quiescently but could not be resumed"
  fi
  record_success "$rollback_ref" "$candidate_ref" "$candidate_version" "$candidate_revision"
  if [ "$allow_downgrade" != "true" ]; then
    rm -f "$FAILED_CANDIDATE_FILE"
  fi
  OLD_CONTAINER_STOPPED=false
  TRANSACTION_COMPLETE=true
  DRAINED_CONTAINER=""
  rm -f "$TRANSACTION_FILE"
  log "updated validator $current_version -> $candidate_version; retained rollback image $rollback_ref"
}

cleanup() {
  local status=$? journal_phase journal_previous journal_previous_id
  local journal_current journal_current_id journal_version journal_revision
  local journal_suppress restored_container resume_status
  trap - EXIT
  # systemd grants cleanup its separately derived TimeoutStopSec. Ignore a
  # second TERM while the bounded recovery is already in progress.
  trap '' INT TERM
  set +e
  journal_phase="$(transaction_value PHASE 2>/dev/null || true)"
  if [ -n "$journal_phase" ]; then
    journal_previous="$(transaction_value PREVIOUS_IMAGE)"
    journal_previous_id="$(transaction_value PREVIOUS_IMAGE_ID)"
    journal_current="$(transaction_value CURRENT_IMAGE)"
    journal_current_id="$(transaction_value CURRENT_IMAGE_ID)"
    journal_version="$(transaction_value CURRENT_VERSION)"
    journal_revision="$(transaction_value CURRENT_REVISION)"
    journal_suppress="$(transaction_value SUPPRESS_CANDIDATE)"
    journal_suppress="${journal_suppress:-true}"
  fi
  case "$journal_phase" in
    prepared)
      if resume_or_restore_prepared_image "$journal_previous" "$journal_previous_id"; then
        rm -f "$TRANSACTION_FILE"
        OLD_CONTAINER_STOPPED=false
        TRANSACTION_COMPLETE=true
      else
        resume_status=$?
        if [ "$resume_status" -eq 76 ] || \
          [ "$RESUME_SIGNAL_DELIVERED" = "true" ]; then
          log "CRITICAL: USR2 was delivered but readiness is unverified; leaving the prepared journal without recreating"
          OLD_CONTAINER_STOPPED=false
          TRANSACTION_COMPLETE=true
        elif [ "$resume_status" -eq 77 ]; then
          log "CRITICAL: prepared target may be working; leaving the journal without recreating"
          OLD_CONTAINER_STOPPED=false
          TRANSACTION_COMPLETE=true
        else
          log "CRITICAL: prepared target could not be safely restored"
        fi
      fi
      DRAINED_CONTAINER=""
      ;;
    stopped | candidate_ready)
      if resume_existing_recorded_image "$journal_previous" \
        "$journal_previous_id" "$CLEANUP_READY_TIMEOUT" "$CLEANUP_CHECK_SECONDS"; then
        OLD_CONTAINER_STOPPED=false
        TRANSACTION_COMPLETE=true
        DRAINED_CONTAINER=""
      else
        resume_status=$?
        if [ "$resume_status" -eq 76 ] || [ "$resume_status" -eq 77 ] || \
          [ "$RESUME_SIGNAL_DELIVERED" = "true" ]; then
          log "CRITICAL: journal target may be working; refusing another recreation"
          OLD_CONTAINER_STOPPED=false
          TRANSACTION_COMPLETE=true
        else
          ROLLBACK_REF="$journal_previous"
          ROLLBACK_IMAGE_ID="$journal_previous_id"
          OLD_CONTAINER_STOPPED=true
          TRANSACTION_COMPLETE=false
        fi
      fi
      DRAINED_CONTAINER=""
      ;;
    committed)
      # These journal phases identify the image that owns the quiescent target.
      # Recover from the durable phase rather than depending on the assignment
      # order of the in-memory flags around the journal write.
      if resume_existing_recorded_image "$journal_current" \
        "$journal_current_id" "$CLEANUP_READY_TIMEOUT" "$CLEANUP_CHECK_SECONDS"; then
        resume_status=0
        log "interrupted at $journal_phase; resuming the journal-selected validator"
        OLD_CONTAINER_STOPPED=false
        TRANSACTION_COMPLETE=true
        DRAINED_CONTAINER=""
      else
        resume_status=$?
        if [ "$resume_status" -eq 76 ] || [ "$resume_status" -eq 77 ] || \
          [ "$RESUME_SIGNAL_DELIVERED" = "true" ]; then
          log "CRITICAL: committed validator may be working; refusing rollback without a new drain"
          OLD_CONTAINER_STOPPED=false
          TRANSACTION_COMPLETE=true
          DRAINED_CONTAINER=""
        elif [ -n "$journal_previous" ] && [ -n "$journal_previous_id" ] && \
          [ -n "$journal_current" ] && [ -n "$journal_current_id" ]; then
          log "CRITICAL: could not resume the $journal_phase validator"
          # A candidate that cannot leave quiescent mode is no longer a
          # committed authority. Persist rollback intent before recreating
          # anything so the next timer can never retry it as committed.
          record_transaction rollback_pending "$journal_previous" \
            "$journal_previous_id" "$journal_current" "$journal_current_id" \
            "$journal_version" "$journal_revision" "$journal_suppress"
          journal_phase=rollback_pending
          ROLLBACK_REF="$journal_previous"
          ROLLBACK_IMAGE_ID="$journal_previous_id"
          OLD_CONTAINER_STOPPED=true
          TRANSACTION_COMPLETE=false
        else
          log "CRITICAL: committed journal is incomplete; refusing blind rollback"
        fi
      fi
      # Do not spend a second readiness window on the same container below.
      # If journal recovery failed, the rollback block remains armed.
      DRAINED_CONTAINER=""
      ;;
    rollback_ready)
      log "interrupted at rollback_ready; resuming the restored validator"
      if resume_existing_recorded_image "$journal_previous" \
        "$journal_previous_id" "$CLEANUP_READY_TIMEOUT" "$CLEANUP_CHECK_SECONDS"; then
        OLD_CONTAINER_STOPPED=false
        TRANSACTION_COMPLETE=true
        DRAINED_CONTAINER=""
      else
        resume_status=$?
        log "CRITICAL: could not resume the rollback_ready validator"
        if [ "$resume_status" -eq 76 ] || [ "$resume_status" -eq 77 ] || \
          [ "$RESUME_SIGNAL_DELIVERED" = "true" ]; then
          log "CRITICAL: rollback image may be working; refusing another recreation"
          OLD_CONTAINER_STOPPED=false
          TRANSACTION_COMPLETE=true
        else
          ROLLBACK_REF="$journal_previous"
          ROLLBACK_IMAGE_ID="$journal_previous_id"
          OLD_CONTAINER_STOPPED=true
          TRANSACTION_COMPLETE=false
        fi
      fi
      DRAINED_CONTAINER=""
      ;;
    rollback_pending)
      if resume_existing_recorded_image "$journal_previous" \
        "$journal_previous_id" "$CLEANUP_READY_TIMEOUT" "$CLEANUP_CHECK_SECONDS"; then
        OLD_CONTAINER_STOPPED=false
        TRANSACTION_COMPLETE=true
      else
        resume_status=$?
        if [ "$resume_status" -eq 76 ] || [ "$resume_status" -eq 77 ] || \
          [ "$RESUME_SIGNAL_DELIVERED" = "true" ]; then
          log "CRITICAL: rollback image may be working; refusing another recreation"
          OLD_CONTAINER_STOPPED=false
          TRANSACTION_COMPLETE=true
        else
          ROLLBACK_REF="$journal_previous"
          ROLLBACK_IMAGE_ID="$journal_previous_id"
          OLD_CONTAINER_STOPPED=true
          TRANSACTION_COMPLETE=false
        fi
      fi
      DRAINED_CONTAINER=""
      ;;
  esac
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
      restored_container="$(target_container 2>/dev/null || true)"
      if [ "$journal_phase" = "rollback_pending" ]; then
        record_transaction rollback_ready "$journal_previous" \
          "$journal_previous_id" "$journal_current" "$journal_current_id" \
          "$journal_version" "$journal_revision" "$journal_suppress"
      fi
      if [ -z "$restored_container" ] || \
        ! resume_and_verify "$restored_container" "$CLEANUP_READY_TIMEOUT" "$CLEANUP_CHECK_SECONDS"; then
        log "CRITICAL: restored validator is quiescent but could not be resumed"
      else
        OLD_CONTAINER_STOPPED=false
        TRANSACTION_COMPLETE=true
        if [ "$journal_phase" = "rollback_pending" ]; then
          if [ "$journal_suppress" = "true" ]; then
            printf '%s\n' "$journal_current" >"$FAILED_CANDIDATE_FILE"
          fi
          rm -f "$TRANSACTION_FILE"
        elif [ "$journal_phase" = "stopped" ] || \
          [ "$journal_phase" = "candidate_ready" ]; then
          rm -f "$TRANSACTION_FILE"
        fi
      fi
    fi
  fi
  if [ "$LOCK_HELD" = "true" ]; then
    flock -u 9 >/dev/null 2>&1 || true
    exec 9>&-
  fi
  exit "$status"
}

handle_interrupt() {
  log "received termination request; running transaction cleanup"
  exit 143
}

acquire_lock() {
  # Keep the file present and lock its inode for this process lifetime. Kernel
  # release on crash removes stale-lock recovery and PID check/write races.
  exec 9>>"$LOCK_FILE"
  flock -n 9 || die "another validator update operation is already running"
  LOCK_HELD=true
}

show_status() {
  local enabled container image_id version revision state managed_image
  enabled="$(setting VALIDATOR_AUTO_UPDATE false)"
  printf 'enabled=%s\n' "$enabled"
  printf 'channel=%s\n' "$CANDIDATE_CHANNEL"
  managed_image="$(
    awk -F= '$1 == "DITTO_SUBNET_IMAGE" { print substr($0, index($0, "=") + 1) }' \
      "$MANAGED_IMAGE_FILE" 2>/dev/null || true
  )"
  printf 'managed_image=%s\n' "${managed_image:-unmanaged}"
  [ ! -f "$TRANSACTION_FILE" ] || \
    printf 'TRANSACTION_PHASE=%s\n' "$(transaction_value PHASE)"
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

reconcile_sidecars() {
  local managed_ref managed_id container actual_id current_state resume_status
  if is_true "$(setting VALIDATOR_AUTO_UPDATE false)"; then
    die "set VALIDATOR_AUTO_UPDATE=false and stop the timer before sidecar reconciliation"
  fi
  managed_ref="$(managed_image_ref)"
  managed_id="$(docker image inspect --format '{{ .Id }}' "$managed_ref" 2>/dev/null || true)"
  [ -n "$managed_id" ] || die "persisted managed validator image is unavailable"
  container="$(target_container)"
  [ -n "$container" ] || die "ditto-subnet is not running"
  assert_scoped_container "$container"
  actual_id="$(docker inspect --format '{{ .Image }}' "$container")"
  [ "$actual_id" = "$managed_id" ] || \
    die "running validator does not match persisted managed-image state"
  current_state="$(runtime_state "$container")"
  state_is_ready "$current_state" || \
    die "validator is not operational and freshly platform-accepted"

  if ! request_bounded_drain "$container" "$drain_timeout" "$check_seconds"; then
    return 0
  fi
  log "reconciling non-validator sidecars while ditto-subnet remains drained"
  if ! DITTO_ALLOW_MANAGED_SIDECAR_RECONCILE=true \
    DITTO_SIDECAR_READY_TIMEOUT_SECONDS="$ready_timeout" \
    "$COMPOSE" managed-reconcile; then
    # Compose may have partially recreated a dependency. Do not accept new
    # leases until the operator repairs/verifies sidecars and explicitly runs
    # `recover`; generic EXIT cleanup must not resume this validator.
    DRAINED_CONTAINER=""
    die "sidecar reconciliation failed; validator remains drained until explicit recovery"
  fi
  if resume_and_verify "$container" "$ready_timeout" "$check_seconds"; then
    DRAINED_CONTAINER=""
    log "sidecars reconciled and validator resumed"
    return 0
  else
    resume_status=$?
  fi
  if [ "$resume_status" -eq 76 ]; then
    DRAINED_CONTAINER=""
    die "validator may be working after USR2; sidecars changed but readiness is unverified"
  fi
  die "sidecars changed but validator could not be resumed"
}

mode="${1:-run}"
case "$mode" in
  adopt)
    [ "$#" -eq 2 ] || die "usage: $0 adopt <immutable-validator-digest>"
    ;;
  budget | reconcile-sidecars | recover | run | rollback | status)
    [ "$#" -le 1 ] || \
      die "usage: $0 [run|status|rollback|reconcile-sidecars|recover|budget]"
    ;;
  *) die "usage: $0 [run|status|rollback|reconcile-sidecars|recover|budget|adopt <digest>]" ;;
esac

if [ "$mode" = "budget" ]; then
  show_timeout_budget
  exit 0
fi

command -v docker >/dev/null 2>&1 || die "Docker is not installed"
[ -x "$COMPOSE" ] || die "validator Compose wrapper is not executable"

if [ "$mode" = "status" ]; then
  show_status
  exit 0
fi

command -v flock >/dev/null 2>&1 || die "flock (util-linux) is not installed"
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

if [ "$mode" = "adopt" ]; then
  adopt_running_image "$2"
  exit 0
fi

if [ "$mode" = "reconcile-sidecars" ]; then
  reconcile_sidecars
  exit 0
fi

if [ "$mode" = "recover" ]; then
  if is_true "$(setting VALIDATOR_AUTO_UPDATE false)"; then
    die "set VALIDATOR_AUTO_UPDATE=false and stop the timer before explicit recovery"
  fi
  managed_image_ref >/dev/null
  recover_interrupted_transaction
  recover_quiescent_target
  log "recovery complete"
  exit 0
fi

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

managed_image_ref >/dev/null
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
