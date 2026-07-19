#!/usr/bin/env bash
# Transactionally update the complete validator Compose stack from an immutable
# descriptor image. The legacy validator-only updater remains available for
# operators who have not completed supervised stack adoption.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STACK_COMPOSE="${DITTO_VALIDATOR_STACK_COMPOSE:-$ROOT_DIR/scripts/validator-stack-compose.sh}"
ENV_FILE="${DITTO_SUBNET_ENV_FILE:-$ROOT_DIR/.env}"
STATE_DIR="${DITTO_VALIDATOR_STACK_UPDATE_STATE_DIR:-$ROOT_DIR/.validator-stack-update}"
DESCRIPTOR_REPOSITORY="ghcr.io/ditto-assistant/ditto-subnet-stack"
CANDIDATE_CHANNEL="$DESCRIPTOR_REPOSITORY:compat-2"
MANAGED_FILE="$STATE_DIR/managed-release.env"
TRANSACTION_FILE="$STATE_DIR/transaction.env"
FAILED_CANDIDATE_FILE="$STATE_DIR/failed-candidate"
LAST_UPDATE_FILE="$STATE_DIR/last-update.env"
LOCK_FILE="$STATE_DIR/lock"
CURRENT_DIR="$STATE_DIR/current"
PREVIOUS_DIR="$STATE_DIR/previous"
STAGED_DIR="$STATE_DIR/staged"
EXPECTED_FORMAT_VERSION=1
EXPECTED_COMPATIBILITY_EPOCH=2
EXPECTED_UPDATE_PROTOCOL=1
EXPECTED_COMPOSE_SCHEMA=1
RUNTIME_STATE_PATH=/tmp/ditto-validator-update-state.json
SERVICES=(pylon sandbox-docker model-relay ollama dittobench-api ditto-subnet)
IMAGE_KEYS=(PYLON_IMAGE SANDBOX_DOCKER_IMAGE MODEL_RELAY_IMAGE OLLAMA_IMAGE DITTOBENCH_API_IMAGE VALIDATOR_IMAGE)
LOCK_HELD=false
DRAINED_CONTAINER=""
DRAINED_RELEASE_DIR=""
RESUME_SIGNAL_DELIVERED=false
CLEANUP_ACTIVE=false

log() { printf 'validator-stack-auto-update: %s\n' "$*" >&2; }
die() { log "error: $*"; exit 1; }

setting() {
  local name="$1" default="$2" value="${!1-}" line=""
  if [ -z "$value" ] && [ -f "$ENV_FILE" ]; then
    line="$(awk -v key="$name" 'index($0,key "=")==1 {v=substr($0,length(key)+2)} END {print v}' "$ENV_FILE")"
    value="$line"
  fi
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "${value:-$default}"
}

is_true() { case "$1" in 1|true|TRUE|True|yes|YES|Yes) return 0;; *) return 1;; esac; }
require_positive_integer() { [[ "$2" =~ ^[1-9][0-9]*$ ]] || die "$1 must be a positive integer"; }
is_descriptor_digest() { [[ "$1" =~ ^ghcr\.io/ditto-assistant/ditto-subnet-stack@sha256:[0-9a-f]{64}$ ]]; }
is_image_digest() { [[ "$1" =~ ^[a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$ ]]; }

validate_image_repository() {
  case "$1" in
    VALIDATOR_IMAGE) [[ "$2" =~ ^ghcr\.io/ditto-assistant/ditto-subnet-validator@sha256:[0-9a-f]{64}$ ]] ;;
    SANDBOX_DOCKER_IMAGE) [[ "$2" =~ ^ghcr\.io/ditto-assistant/ditto-subnet-sandbox-docker@sha256:[0-9a-f]{64}$ ]] ;;
    DITTOBENCH_API_IMAGE) [[ "$2" =~ ^ghcr\.io/ditto-assistant/dittobench-api-sandbox@sha256:[0-9a-f]{64}$ ]] ;;
    MODEL_RELAY_IMAGE) [[ "$2" =~ ^ghcr\.io/ditto-assistant/dittobench-api-relay@sha256:[0-9a-f]{64}$ ]] ;;
    PYLON_IMAGE) [[ "$2" =~ ^docker\.io/backenddevelopersltd/bittensor-pylon@sha256:[0-9a-f]{64}$ ]] ;;
    OLLAMA_IMAGE) [[ "$2" =~ ^docker\.io/ollama/ollama@sha256:[0-9a-f]{64}$ ]] ;;
    *) return 1 ;;
  esac
}

manifest_value() {
  local file="$1" key="$2"
  awk -F= -v key="$key" '$1==key {print substr($0,index($0,"=")+1); exit}' "$file"
}

validate_manifest() {
  local dir="$1" file line key value count=0 expected_count=14 image_key seen_keys='|'
  file="$dir/manifest.env"
  local allowed=' STACK_FORMAT_VERSION STACK_VERSION STACK_REVISION DITTOBENCH_REVISION COMPATIBILITY_EPOCH UPDATE_PROTOCOL COMPOSE_SCHEMA HEARTBEAT_PROTOCOL VALIDATOR_IMAGE SANDBOX_DOCKER_IMAGE DITTOBENCH_API_IMAGE MODEL_RELAY_IMAGE PYLON_IMAGE OLLAMA_IMAGE '
  [ -f "$dir/compose.yml" ] && [ ! -L "$dir/compose.yml" ] || return 1
  [ -f "$file" ] && [ ! -L "$file" ] || return 1
  [ -z "$(find "$dir" -type l -print -quit)" ] || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || continue
    [[ "$line" =~ ^([A-Z][A-Z0-9_]*)=([^[:space:]]+)$ ]] || return 1
    key="${BASH_REMATCH[1]}"; value="${BASH_REMATCH[2]}"
    [[ "$allowed" == *" $key "* ]] || return 1
    [[ "$seen_keys" != *"|$key|"* ]] || return 1
    seen_keys="${seen_keys}${key}|"; count=$((count+1))
  done <"$file"
  [ "$count" -eq "$expected_count" ] || return 1
  for key in $allowed; do [[ "$seen_keys" == *"|$key|"* ]] || return 1; done
  [ "$(manifest_value "$file" STACK_FORMAT_VERSION)" = "$EXPECTED_FORMAT_VERSION" ] || return 1
  [[ "$(manifest_value "$file" STACK_VERSION)" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
  [[ "$(manifest_value "$file" STACK_REVISION)" =~ ^[0-9a-f]{40}$ ]] || return 1
  [[ "$(manifest_value "$file" DITTOBENCH_REVISION)" =~ ^[0-9a-f]{40}$ ]] || return 1
  [ "$(manifest_value "$file" COMPATIBILITY_EPOCH)" = "$EXPECTED_COMPATIBILITY_EPOCH" ] || return 1
  [ "$(manifest_value "$file" UPDATE_PROTOCOL)" = "$EXPECTED_UPDATE_PROTOCOL" ] || return 1
  [ "$(manifest_value "$file" COMPOSE_SCHEMA)" = "$EXPECTED_COMPOSE_SCHEMA" ] || return 1
  [[ "$(manifest_value "$file" HEARTBEAT_PROTOCOL)" =~ ^[1-9][0-9]*$ ]] || return 1
  for image_key in "${IMAGE_KEYS[@]}"; do
    value="$(manifest_value "$file" "$image_key")"
    is_image_digest "$value" || return 1
    validate_image_repository "$image_key" "$value" || return 1
  done
}

verify_descriptor_signature() {
  local image="$1"
  is_descriptor_digest "$image" || return 1
  cosign verify \
    --certificate-identity-regexp '^https://github.com/ditto-assistant/ditto-subnet/.github/workflows/release.yml@refs/heads/main$' \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    "$image" >/dev/null
}

validate_compose_contract() {
  local dir="$1" actual_services actual_images expected_services expected_images key configured
  expected_services="$(printf '%s\n' "${SERVICES[@]}" | sort)"
  actual_services="$(compose "$dir" config --services 2>/dev/null | sort)" || return 1
  [ "$actual_services" = "$expected_services" ] || return 1
  expected_images="$(for key in "${IMAGE_KEYS[@]}"; do manifest_value "$dir/manifest.env" "$key"; done | sort)"
  actual_images="$(compose "$dir" config --images 2>/dev/null | sort)" || return 1
  [ "$actual_images" = "$expected_images" ] || return 1
  configured="$(compose "$dir" config 2>/dev/null)" || return 1
  ! grep -Eq '^[[:space:]]+build:' <<<"$configured"
}

descriptor_label() {
  local image="$1" label="$2" value
  value="$(docker image inspect --format "{{ index .Config.Labels \"$label\" }}" "$image" 2>/dev/null || true)"
  [ "$value" = '<no value>' ] && value=''
  printf '%s' "$value"
}

validate_descriptor() {
  local image="$1" dir="$2" manifest
  manifest="$dir/manifest.env"
  is_descriptor_digest "$image" || return 1
  validate_manifest "$dir" || return 1
  [ "$(descriptor_label "$image" io.heyditto.validator.stack-release)" = true ] || return 1
  [ "$(descriptor_label "$image" org.opencontainers.image.source)" = "https://github.com/ditto-assistant/ditto-subnet" ] || return 1
  [ "$(descriptor_label "$image" io.heyditto.validator.compatibility-epoch)" = "$EXPECTED_COMPATIBILITY_EPOCH" ] || return 1
  [ "$(descriptor_label "$image" io.heyditto.validator.update-protocol)" = "$EXPECTED_UPDATE_PROTOCOL" ] || return 1
  [ "$(descriptor_label "$image" io.heyditto.validator.compose-schema)" = "$EXPECTED_COMPOSE_SCHEMA" ] || return 1
  [ "$(descriptor_label "$image" org.opencontainers.image.version)" = "$(manifest_value "$manifest" STACK_VERSION)" ] || return 1
  [ "$(descriptor_label "$image" org.opencontainers.image.revision)" = "$(manifest_value "$manifest" STACK_REVISION)" ] || return 1
  validate_compose_contract "$dir" || return 1
}

extract_descriptor() {
  local image="$1" destination="$2" container temporary
  # Keep authentication adjacent to extraction so a future caller cannot turn
  # a merely well-formed local bundle into reported managed-release identity.
  verify_descriptor_signature "$image" || return 1
  temporary="$destination.tmp.$$"
  rm -rf -- "$temporary"
  mkdir -p "$temporary"
  container="$(docker create "$image")" || { rm -rf -- "$temporary"; return 1; }
  if ! docker cp "$container:/release/." "$temporary"; then
    docker rm -f "$container" >/dev/null 2>&1 || true
    rm -rf -- "$temporary"
    return 1
  fi
  docker rm -f "$container" >/dev/null
  # Reject every entry except the two regular release files before adding the
  # updater-owned descriptor binding. This catches links, devices, sockets,
  # FIFOs, and directories as well as ordinary unexpected files.
  if [ ! -f "$temporary/manifest.env" ] || [ -L "$temporary/manifest.env" ] || \
    [ ! -f "$temporary/compose.yml" ] || [ -L "$temporary/compose.yml" ] || \
    [ "$(find "$temporary" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')" -ne 2 ]; then
    rm -rf -- "$temporary"
    return 1
  fi
  printf '%s\n' "$image" >"$temporary/.descriptor-ref"
  chmod 0700 "$temporary"
  chmod 0600 "$temporary/manifest.env" "$temporary/compose.yml" "$temporary/.descriptor-ref"
  rm -rf -- "$destination"
  if ! mv "$temporary" "$destination"; then
    rm -rf -- "$temporary" "$destination"
    return 1
  fi
  # The Compose trust wrapper only accepts the canonical updater-owned staged
  # directory and requires its immutable descriptor binding. Move the
  # untrusted extraction there before validation; callers cannot consume it
  # unless this check succeeds, and failures remove it immediately.
  if ! validate_descriptor "$image" "$destination"; then
    rm -rf -- "$destination"
    return 1
  fi
}

resolve_channel_digest() {
  local ref="$1" digest
  docker pull "$ref" >/dev/null
  digest="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$ref" | awk -v p="$DESCRIPTOR_REPOSITORY@" 'index($0,p)==1 {print; exit}')"
  is_descriptor_digest "$digest" || die "descriptor channel did not resolve to an immutable digest"
  printf '%s' "$digest"
}

pull_release_images() {
  local dir="$1" key image
  for key in "${IMAGE_KEYS[@]}"; do
    image="$(manifest_value "$dir/manifest.env" "$key")"
    docker pull "$image" >/dev/null || return 1
    docker image inspect "$image" >/dev/null 2>&1 || return 1
  done
  validate_release_image_labels "$dir"
}

validate_image_labels() {
  local image="$1" source="$2" revision="$3" version="$4"
  [ "$(descriptor_label "$image" org.opencontainers.image.source)" = "$source" ] &&
    [ "$(descriptor_label "$image" org.opencontainers.image.revision)" = "$revision" ] &&
    [ "$(descriptor_label "$image" org.opencontainers.image.version)" = "$version" ]
}

validate_release_image_labels() {
  local dir="$1" manifest stack_revision dbench_revision version
  manifest="$dir/manifest.env"
  stack_revision="$(manifest_value "$manifest" STACK_REVISION)"
  dbench_revision="$(manifest_value "$manifest" DITTOBENCH_REVISION)"
  version="$(manifest_value "$manifest" STACK_VERSION)"
  validate_image_labels "$(manifest_value "$manifest" VALIDATOR_IMAGE)" \
    https://github.com/ditto-assistant/ditto-subnet "$stack_revision" "$version" || return 1
  [ "$(descriptor_label "$(manifest_value "$manifest" VALIDATOR_IMAGE)" io.heyditto.validator.heartbeat-protocol)" = \
    "$(manifest_value "$manifest" HEARTBEAT_PROTOCOL)" ] || return 1
  validate_image_labels "$(manifest_value "$manifest" SANDBOX_DOCKER_IMAGE)" \
    https://github.com/ditto-assistant/ditto-subnet "$stack_revision" "$version" || return 1
  validate_image_labels "$(manifest_value "$manifest" DITTOBENCH_API_IMAGE)" \
    https://github.com/ditto-assistant/dittobench-api "$dbench_revision" "$version" || return 1
  validate_image_labels "$(manifest_value "$manifest" MODEL_RELAY_IMAGE)" \
    https://github.com/ditto-assistant/dittobench-api "$dbench_revision" "$version"
}

compose() {
  local dir="$1"; shift
  "$STACK_COMPOSE" "$dir" "$@"
}

service_container() { compose "$1" ps -q "$2" 2>/dev/null || true; }
runtime_state() { docker exec "$1" sh -c "cat '$RUNTIME_STATE_PATH'" 2>/dev/null || true; }
container_heartbeat_protocol() {
  local container="$1" image value
  image="$(docker inspect --format '{{.Image}}' "$container" 2>/dev/null || true)"
  value="$(descriptor_label "$image" io.heyditto.validator.heartbeat-protocol)"
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || return 1
  printf '%s' "$value"
}
state_ready() {
  local state="$1" container="$2" heartbeat
  heartbeat="$(container_heartbeat_protocol "$container")" || return 1
  [[ "$state" == *\"compatibility_epoch\":$EXPECTED_COMPATIBILITY_EPOCH* ]] &&
    [[ "$state" == *\"heartbeat_protocol\":$heartbeat* ]] &&
    [[ "$state" == *'"platform_accepted":true'* ]] &&
    [[ "$state" == *\"update_protocol\":$EXPECTED_UPDATE_PROTOCOL* ]] &&
    { [[ "$state" == *'"state":"ready"'* ]] || [[ "$state" == *'"state":"working"'* ]]; }
}
state_drained() {
  local state="$1" container="$2" heartbeat
  heartbeat="$(container_heartbeat_protocol "$container")" || return 1
  [[ "$state" == *\"compatibility_epoch\":$EXPECTED_COMPATIBILITY_EPOCH* ]] &&
    [[ "$state" == *\"heartbeat_protocol\":$heartbeat* ]] &&
    [[ "$state" == *'"platform_accepted":true'* ]] &&
    [[ "$state" == *\"update_protocol\":$EXPECTED_UPDATE_PROTOCOL* ]] &&
    [[ "$state" == *'"state":"drained"'* ]]
}

assert_stack_matches() {
  local dir="$1" index service key ref expected actual container
  for index in "${!SERVICES[@]}"; do
    service="${SERVICES[$index]}"; key="${IMAGE_KEYS[$index]}"
    ref="$(manifest_value "$dir/manifest.env" "$key")"
    expected="$(docker image inspect --format '{{.Id}}' "$ref" 2>/dev/null || true)"
    container="$(service_container "$dir" "$service")"
    [ -n "$expected" ] && [ -n "$container" ] || return 1
    actual="$(docker inspect --format '{{.Image}}' "$container" 2>/dev/null || true)"
    [ "$actual" = "$expected" ] || return 1
  done
}

stack_services_healthy() {
  local dir="$1" service container running health
  for service in "${SERVICES[@]:0:5}"; do
    container="$(service_container "$dir" "$service")"; [ -n "$container" ] || return 1
    running="$(docker inspect --format '{{.State.Running}}' "$container" 2>/dev/null || true)"
    [ "$running" = true ] || return 1
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container" 2>/dev/null || true)"
    case "$health" in healthy|none) ;; *) return 1;; esac
  done
}

wait_stack_quiescent() {
  local dir="$1" timeout="$2" interval="$3" deadline container state running
  deadline=$((SECONDS+timeout))
  while ((SECONDS<deadline)); do
    if stack_services_healthy "$dir"; then
      container="$(service_container "$dir" ditto-subnet)"
      running="$(docker inspect --format '{{.State.Running}}' "$container" 2>/dev/null || true)"
      state="$(runtime_state "$container")"
      if [ "$running" = true ] && state_drained "$state" "$container" && assert_stack_matches "$dir"; then return 0; fi
    fi
    sleep "$interval"
  done
  return 1
}

resume_and_verify() {
  local dir="$1" timeout="$2" interval="$3" container deadline state delivered=false
  container="$(service_container "$dir" ditto-subnet)"; [ -n "$container" ] || return 1
  RESUME_SIGNAL_DELIVERED=true
  if docker kill --signal=USR2 "$container" >/dev/null 2>&1; then delivered=true; fi
  deadline=$((SECONDS+timeout))
  while ((SECONDS<deadline)); do
    state="$(runtime_state "$container")"
    if state_ready "$state" "$container" && [ "$delivered" = true ]; then RESUME_SIGNAL_DELIVERED=false; DRAINED_CONTAINER=''; return 0; fi
    sleep "$interval"
  done
  return 76
}

request_drain() {
  local dir="$1" timeout="$2" interval="$3" container deadline state
  container="$(service_container "$dir" ditto-subnet)"; [ -n "$container" ] || return 1
  docker kill --signal=USR1 "$container" >/dev/null || return 1
  DRAINED_CONTAINER="$container"; DRAINED_RELEASE_DIR="$dir"; deadline=$((SECONDS+timeout))
  while ((SECONDS<deadline)); do
    state="$(runtime_state "$container")"
    if state_drained "$state" "$container"; then return 0; fi
    [ "$(docker inspect --format '{{.State.Running}}' "$container" 2>/dev/null || true)" = true ] || return 1
    sleep "$interval"
  done
  resume_and_verify "$dir" "$ready_timeout" "$interval" || return 1
  return 75
}

new_bootstrap_token() {
  local token
  token="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
  [[ "$token" =~ ^[0-9a-f]{32}$ ]] || return 1
  printf '%s' "$token"
}

deploy_release() {
  local dir="$1" token
  token="$(new_bootstrap_token)" || return 1
  DITTO_ALLOW_MANAGED_STACK_MUTATION=true VALIDATOR_START_DRAINED=true \
    VALIDATOR_BOOTSTRAP_TOKEN="$token" compose "$dir" up -d --no-build --pull never \
      --force-recreate pylon sandbox-docker model-relay ollama dittobench-api || return 1
  DITTO_ALLOW_MANAGED_STACK_MUTATION=true VALIDATOR_START_DRAINED=true \
    VALIDATOR_BOOTSTRAP_TOKEN="$token" compose "$dir" up -d --no-deps --no-build \
      --pull never --force-recreate ditto-subnet
}

restore_release() {
  local dir="$1"
  deploy_release "$dir" || return 1
  wait_stack_quiescent "$dir" "$ready_timeout" "$check_seconds" || return 1
  resume_and_verify "$dir" "$ready_timeout" "$check_seconds"
}

record_transaction() {
  local phase="$1" previous="$2" candidate="$3" temporary="$TRANSACTION_FILE.tmp"
  umask 077
  printf 'PHASE=%s\nPREVIOUS_RELEASE=%s\nCANDIDATE_RELEASE=%s\n' "$phase" "$previous" "$candidate" >"$temporary"
  mv "$temporary" "$TRANSACTION_FILE"
}
transaction_value() { manifest_value "$TRANSACTION_FILE" "$1"; }
record_managed() {
  local digest="$1" temporary="$MANAGED_FILE.tmp"
  is_descriptor_digest "$digest" || die "refusing to persist a mutable descriptor reference"
  printf 'STACK_RELEASE=%s\n' "$digest" >"$temporary"; mv "$temporary" "$MANAGED_FILE"
}
managed_release() {
  local digest
  [ -f "$MANAGED_FILE" ] || die "managed stack mode is not adopted; run supervised adopt first"
  digest="$(manifest_value "$MANAGED_FILE" STACK_RELEASE)"
  is_descriptor_digest "$digest" || die "managed stack release state is malformed"
  printf '%s' "$digest"
}

semver_greater_same_major() {
  local candidate="$1" current="$2" cm cn cp om on op
  IFS=. read -r cm cn cp <<<"$candidate"; IFS=. read -r om on op <<<"$current"
  [ "$cm" = "$om" ] && { ((10#$cn>10#$on)) || { [ "$cn" = "$on" ] && ((10#$cp>10#$op)); }; }
}

install_staged_as_current() {
  local old="$STATE_DIR/old-current.$$"
  rm -rf -- "$old" "$PREVIOUS_DIR"
  [ ! -d "$CURRENT_DIR" ] || mv "$CURRENT_DIR" "$old"
  mv "$STAGED_DIR" "$CURRENT_DIR"
  [ ! -d "$old" ] || mv "$old" "$PREVIOUS_DIR"
}

promote_previous_after_rollback() {
  local previous_ref="$1" current_ref current_old="$STATE_DIR/failed-current.$$"
  current_ref="$(cat "$CURRENT_DIR/.descriptor-ref" 2>/dev/null || true)"
  [ "$current_ref" = "$previous_ref" ] && return 0
  [ -d "$PREVIOUS_DIR" ] || return 1
  validate_descriptor "$previous_ref" "$PREVIOUS_DIR" || return 1
  rm -rf -- "$current_old"
  mv "$CURRENT_DIR" "$current_old"
  mv "$PREVIOUS_DIR" "$CURRENT_DIR"
  mv "$current_old" "$PREVIOUS_DIR"
}

rollback_to_previous() {
  local previous_ref="$1"
  [ -d "$PREVIOUS_DIR" ] || return 1
  deploy_release "$PREVIOUS_DIR" || return 1
  wait_stack_quiescent "$PREVIOUS_DIR" "$ready_timeout" "$check_seconds" || return 1
  record_transaction rollback_ready "$previous_ref" "$(transaction_value CANDIDATE_RELEASE)"
  resume_and_verify "$PREVIOUS_DIR" "$ready_timeout" "$check_seconds" || return 1
  promote_previous_after_rollback "$previous_ref" || return 1
  record_managed "$previous_ref"
}

recover_transaction() {
  local phase previous candidate container state
  [ -f "$TRANSACTION_FILE" ] || return 0
  phase="$(transaction_value PHASE)"; previous="$(transaction_value PREVIOUS_RELEASE)"; candidate="$(transaction_value CANDIDATE_RELEASE)"
  if [ "$phase" = migration_started ]; then
    die "supervised first migration was interrupted after the unmanaged stack stopped; repair or complete it manually before adoption"
  fi
  is_descriptor_digest "$previous" && is_descriptor_digest "$candidate" || die "transaction journal is malformed"
  log "recovering interrupted full-stack transaction ($phase)"
  case "$phase" in
    prepared|drained)
      container="$(service_container "$CURRENT_DIR" ditto-subnet)"; state="$(runtime_state "$container")"
      if [ "$(docker inspect --format '{{.State.Running}}' "$container" 2>/dev/null || true)" = true ]; then
        resume_and_verify "$CURRENT_DIR" "$ready_timeout" "$check_seconds" || die "previous validator may have resumed; inspect before retrying"
      else
        restore_release "$CURRENT_DIR" || die "previous complete stack stopped and could not be restored"
      fi
      ;;
    old_stopped|candidate_started|rollback_pending|rollback_ready)
      rollback_to_previous "$previous" || die "could not recover the previous complete stack"
      printf '%s\n' "$candidate" >"$FAILED_CANDIDATE_FILE"
      ;;
    committed)
      container="$(service_container "$CURRENT_DIR" ditto-subnet)"; state="$(runtime_state "$container")"
      if state_ready "$state" "$container" && stack_services_healthy "$CURRENT_DIR" && assert_stack_matches "$CURRENT_DIR"; then
        record_managed "$candidate"
      elif wait_stack_quiescent "$CURRENT_DIR" "$ready_timeout" "$check_seconds"; then
        resume_and_verify "$CURRENT_DIR" "$ready_timeout" "$check_seconds" || die "committed stack may be working; refusing rollback"
        record_managed "$candidate"
      else
        [ "$(docker inspect --format '{{.State.Running}}' "$container" 2>/dev/null || true)" != true ] || state_drained "$state" "$container" || die "committed validator state is ambiguous; refusing an unsafe rollback"
        record_transaction rollback_pending "$previous" "$candidate"
        rollback_to_previous "$previous" || die "committed stack failed recovery and full rollback failed"
        printf '%s\n' "$candidate" >"$FAILED_CANDIDATE_FILE"
      fi
      ;;
    *) die "unknown transaction phase $phase";;
  esac
  rm -f "$TRANSACTION_FILE"
}

perform_update() {
  local candidate_ref="$1" allow_downgrade="$2" previous_ref current_version candidate_version old_container current_container
  previous_ref="$(managed_release)"
  [ -d "$CURRENT_DIR" ] && validate_descriptor "$previous_ref" "$CURRENT_DIR" || die "managed current descriptor is missing or invalid"
  assert_stack_matches "$CURRENT_DIR" || die "running stack has drifted from its managed release"
  current_container="$(service_container "$CURRENT_DIR" ditto-subnet)"
  state_ready "$(runtime_state "$current_container")" "$current_container" || { log "validator is not freshly platform-accepted; deferring"; return 0; }
  docker pull "$candidate_ref" >/dev/null
  extract_descriptor "$candidate_ref" "$STAGED_DIR" || die "candidate descriptor is invalid"
  pull_release_images "$STAGED_DIR" || die "candidate component pull failed before drain"
  current_version="$(manifest_value "$CURRENT_DIR/manifest.env" STACK_VERSION)"
  candidate_version="$(manifest_value "$STAGED_DIR/manifest.env" STACK_VERSION)"
  if [ "$candidate_ref" = "$previous_ref" ]; then log "already running stack $current_version"; rm -rf -- "$STAGED_DIR"; return 0; fi
  if [ "$allow_downgrade" != true ] && ! semver_greater_same_major "$candidate_version" "$current_version"; then die "candidate $candidate_version requires supervised major migration or is not newer"; fi
  rm -rf -- "$PREVIOUS_DIR"
  cp -R "$CURRENT_DIR" "$PREVIOUS_DIR"
  record_transaction prepared "$previous_ref" "$candidate_ref"
  request_drain "$CURRENT_DIR" "$drain_timeout" "$check_seconds" || { rm -f "$TRANSACTION_FILE"; return 0; }
  record_transaction drained "$previous_ref" "$candidate_ref"
  old_container="$(service_container "$CURRENT_DIR" ditto-subnet)"
  docker stop --time 30 "$old_container" >/dev/null || die "could not stop drained validator"
  DRAINED_CONTAINER=''; DRAINED_RELEASE_DIR=''; record_transaction old_stopped "$previous_ref" "$candidate_ref"
  if ! deploy_release "$STAGED_DIR"; then record_transaction rollback_pending "$previous_ref" "$candidate_ref"; rollback_to_previous "$previous_ref" || die "candidate deploy and full rollback both failed"; printf '%s\n' "$candidate_ref" >"$FAILED_CANDIDATE_FILE"; rm -f "$TRANSACTION_FILE"; die "candidate deploy failed; previous stack restored"; fi
  record_transaction candidate_started "$previous_ref" "$candidate_ref"
  if ! wait_stack_quiescent "$STAGED_DIR" "$ready_timeout" "$check_seconds"; then record_transaction rollback_pending "$previous_ref" "$candidate_ref"; rollback_to_previous "$previous_ref" || die "candidate readiness and full rollback both failed"; printf '%s\n' "$candidate_ref" >"$FAILED_CANDIDATE_FILE"; rm -f "$TRANSACTION_FILE"; die "candidate readiness failed; previous stack restored"; fi
  install_staged_as_current
  record_transaction committed "$previous_ref" "$candidate_ref"
  resume_and_verify "$CURRENT_DIR" "$ready_timeout" "$check_seconds" || die "candidate may be working; committed journal retained"
  record_managed "$candidate_ref"
  printf 'PREVIOUS_RELEASE=%s\nCURRENT_RELEASE=%s\nCURRENT_VERSION=%s\n' "$previous_ref" "$candidate_ref" "$candidate_version" >"$LAST_UPDATE_FILE"
  rm -f "$FAILED_CANDIDATE_FILE" "$TRANSACTION_FILE"
  log "updated complete validator stack $current_version -> $candidate_version"
}

cleanup() {
  local status="$1" phase previous candidate
  [ "$CLEANUP_ACTIVE" = false ] || exit "$status"
  CLEANUP_ACTIVE=true; trap - EXIT INT TERM; set +e
  if [ -n "$DRAINED_CONTAINER" ] && [ "$RESUME_SIGNAL_DELIVERED" = false ] && [ -d "$DRAINED_RELEASE_DIR" ]; then
    log "interrupted after drain; attempting to resume previous validator"
    if [ "$(docker inspect --format '{{.State.Running}}' "$DRAINED_CONTAINER" 2>/dev/null || true)" = true ]; then
      resume_and_verify "$DRAINED_RELEASE_DIR" "$ready_timeout" "$check_seconds" || log "CRITICAL: validator resume could not be verified"
    else
      restore_release "$DRAINED_RELEASE_DIR" || log "CRITICAL: stopped complete stack could not be restored"
    fi
  fi
  phase="$(transaction_value PHASE 2>/dev/null || true)"
  if [ -z "$DRAINED_CONTAINER" ] && [ "$RESUME_SIGNAL_DELIVERED" = false ] && \
    { [ "$phase" = old_stopped ] || [ "$phase" = candidate_started ] || [ "$phase" = rollback_pending ]; }; then
    previous="$(transaction_value PREVIOUS_RELEASE 2>/dev/null || true)"
    if is_descriptor_digest "$previous" && rollback_to_previous "$previous"; then
      candidate="$(transaction_value CANDIDATE_RELEASE 2>/dev/null || true)"
      is_descriptor_digest "$candidate" && printf '%s\n' "$candidate" >"$FAILED_CANDIDATE_FILE"
      rm -f "$TRANSACTION_FILE"
      log "interrupted replacement rolled back the complete previous stack"
    else
      log "CRITICAL: interrupted replacement could not restore the complete previous stack"
    fi
  fi
  if [ "$LOCK_HELD" = true ]; then flock -u 9 >/dev/null 2>&1 || true; exec 9>&-; fi
  exit "$status"
}

show_status() {
  printf 'enabled=%s\nchannel=%s\nmanaged_release=%s\n' "$(setting VALIDATOR_STACK_AUTO_UPDATE false)" "$CANDIDATE_CHANNEL" "$(manifest_value "$MANAGED_FILE" STACK_RELEASE 2>/dev/null || printf unmanaged)"
  if [ -f "$CURRENT_DIR/manifest.env" ] && [ ! -L "$CURRENT_DIR/manifest.env" ]; then
    while IFS= read -r line; do
      case "$line" in
        STACK_VERSION=*|STACK_REVISION=*|DITTOBENCH_REVISION=*|COMPATIBILITY_EPOCH=*|UPDATE_PROTOCOL=*|COMPOSE_SCHEMA=*|HEARTBEAT_PROTOCOL=*|VALIDATOR_IMAGE=*|SANDBOX_DOCKER_IMAGE=*|DITTOBENCH_API_IMAGE=*|MODEL_RELAY_IMAGE=*|PYLON_IMAGE=*|OLLAMA_IMAGE=*)
          printf '%s\n' "$line"
          ;;
      esac
    done <"$CURRENT_DIR/manifest.env"
  fi
  [ ! -f "$TRANSACTION_FILE" ] || printf 'transaction_phase=%s\n' "$(transaction_value PHASE)"
  [ ! -f "$LAST_UPDATE_FILE" ] || cat "$LAST_UPDATE_FILE"
  [ ! -f "$FAILED_CANDIDATE_FILE" ] || printf 'failed_candidate=%s\n' "$(cat "$FAILED_CANDIDATE_FILE")"
}

mode="${1:-run}"
case "$mode" in adopt|migrate|rollback) [ "$#" -le 2 ] || die "usage: $0 $mode [descriptor-digest]";; run|recover|status|budget) [ "$#" -eq 1 ] || die "usage: $0 $mode";; *) die "usage: $0 [run|status|recover|adopt <descriptor-digest>|migrate <descriptor-digest>|rollback]";; esac
if [ "$mode" = budget ]; then printf 'TIMEOUT_START_SECONDS=10800\nTIMEOUT_STOP_SECONDS=600\n'; exit 0; fi
command -v docker >/dev/null 2>&1 || die "Docker is not installed"
[ -x "$STACK_COMPOSE" ] || die "stack Compose wrapper is not executable"
if [ "$mode" = status ]; then show_status; exit 0; fi
command -v flock >/dev/null 2>&1 || die "flock is not installed"
command -v cosign >/dev/null 2>&1 || die "cosign is required to authenticate stack descriptors"
case "$STATE_DIR" in /*) ;; *) die "stack update state directory must be absolute";; esac
[ "$STATE_DIR" != / ] || die "refusing to use the filesystem root as updater state"
[ ! -L "$STATE_DIR" ] || die "stack update state directory must not be a symbolic link"
mkdir -p "$STATE_DIR"; chmod 0700 "$STATE_DIR"; exec 9>>"$LOCK_FILE"; flock -n 9 || die "another stack update is running"; LOCK_HELD=true
drain_timeout="$(setting VALIDATOR_AUTO_UPDATE_DRAIN_TIMEOUT_SECONDS 4800)"; ready_timeout="$(setting VALIDATOR_AUTO_UPDATE_READY_TIMEOUT_SECONDS 300)"; check_seconds="$(setting VALIDATOR_AUTO_UPDATE_CHECK_SECONDS 5)"
require_positive_integer drain_timeout "$drain_timeout"; require_positive_integer ready_timeout "$ready_timeout"; require_positive_integer check_seconds "$check_seconds"
trap 'cleanup $?' EXIT; trap 'exit 143' INT TERM
recover_transaction
if [ "$mode" = recover ]; then log "recovery complete"; exit 0; fi
if is_true "$(setting VALIDATOR_AUTO_UPDATE false)"; then
  die "disable the legacy validator-only updater before using full-stack managed mode"
fi
if [ "$mode" = adopt ]; then
  is_true "$(setting VALIDATOR_STACK_AUTO_UPDATE false)" && die "disable the stack timer before supervised adoption"
  [ "$#" -eq 2 ] || die "usage: $0 adopt <immutable-descriptor-digest>"
  is_descriptor_digest "$2" || die "adopt requires an immutable stack descriptor digest"
  [ ! -f "$MANAGED_FILE" ] || die "managed stack mode is already adopted"
  verify_descriptor_signature "$2" || die "adopted descriptor publisher identity is invalid"
  docker pull "$2" >/dev/null; extract_descriptor "$2" "$STAGED_DIR" || die "adopted descriptor is invalid"; pull_release_images "$STAGED_DIR" || die "adopted release images unavailable"
  assert_stack_matches "$STAGED_DIR" || die "running services do not exactly match the adopted release; perform the supervised Compose migration first"
  adopted_container="$(service_container "$STAGED_DIR" ditto-subnet)"
  state_ready "$(runtime_state "$adopted_container")" "$adopted_container" || die "running validator is not operational and freshly platform-accepted"
  rm -rf -- "$CURRENT_DIR"; mv "$STAGED_DIR" "$CURRENT_DIR"; record_managed "$2"; log "adopted managed complete stack $2"; exit 0
fi
if [ "$mode" = migrate ]; then
  is_true "$(setting VALIDATOR_STACK_AUTO_UPDATE false)" && die "disable the stack timer before supervised first migration"
  [ "$#" -eq 2 ] || die "usage: $0 migrate <immutable-descriptor-digest>"
  is_descriptor_digest "$2" || die "migrate requires an immutable stack descriptor digest"
  [ ! -f "$MANAGED_FILE" ] || die "stack is already managed; use run or rollback"
  verify_descriptor_signature "$2" || die "migration descriptor publisher identity is invalid"
  docker pull "$2" >/dev/null
  extract_descriptor "$2" "$STAGED_DIR" || die "migration descriptor is invalid"
  pull_release_images "$STAGED_DIR" || die "migration component verification failed before drain"
  old_container="$(service_container "$STAGED_DIR" ditto-subnet)"
  [ -n "$old_container" ] || die "unmanaged validator is not running in this Compose project"
  state_ready "$(runtime_state "$old_container")" "$old_container" || die "unmanaged validator is not operational and freshly platform-accepted"
  request_drain "$STAGED_DIR" "$drain_timeout" "$check_seconds" || die "unmanaged validator did not drain; migration cancelled"
  docker stop --time 30 "$old_container" >/dev/null || die "could not stop the drained unmanaged validator"
  DRAINED_CONTAINER=''; DRAINED_RELEASE_DIR=''
  record_transaction migration_started UNMANAGED "$2"
  if ! deploy_release "$STAGED_DIR" || ! wait_stack_quiescent "$STAGED_DIR" "$ready_timeout" "$check_seconds"; then
    die "managed candidate failed after the unmanaged stack stopped; validator remains quiescent for supervised repair"
  fi
  rm -rf -- "$CURRENT_DIR"; mv "$STAGED_DIR" "$CURRENT_DIR"
  resume_and_verify "$CURRENT_DIR" "$ready_timeout" "$check_seconds" || die "migrated validator may be working; verify it before recovery"
  record_managed "$2"; rm -f "$TRANSACTION_FILE"
  log "completed supervised first migration to $2"
  exit 0
fi
if [ "$mode" = rollback ]; then
  is_true "$(setting VALIDATOR_STACK_AUTO_UPDATE false)" && die "disable the stack timer before manual rollback"
  [ -f "$LAST_UPDATE_FILE" ] || die "no previous complete release recorded"
  rollback_ref="$(manifest_value "$LAST_UPDATE_FILE" PREVIOUS_RELEASE)"; is_descriptor_digest "$rollback_ref" || die "recorded rollback release is invalid"
  verify_descriptor_signature "$rollback_ref" || die "rollback descriptor publisher identity is invalid"
  docker pull "$rollback_ref" >/dev/null; extract_descriptor "$rollback_ref" "$STAGED_DIR" || die "rollback descriptor unavailable"; pull_release_images "$STAGED_DIR" || die "rollback images unavailable"
  perform_update "$rollback_ref" true; exit 0
fi
if ! is_true "$(setting VALIDATOR_STACK_AUTO_UPDATE false)"; then log "disabled (set VALIDATOR_STACK_AUTO_UPDATE=true to opt in)"; exit 0; fi
candidate="$(resolve_channel_digest "$CANDIDATE_CHANNEL")"
verify_descriptor_signature "$candidate" || die "candidate descriptor publisher identity is invalid"
if [ -f "$FAILED_CANDIDATE_FILE" ] && [ "$(cat "$FAILED_CANDIDATE_FILE")" = "$candidate" ]; then log "candidate suppressed after failed full-stack rollback"; exit 0; fi
perform_update "$candidate" false
