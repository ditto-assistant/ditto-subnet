#!/usr/bin/env bash
# Run the production validator stack with a locally materialized, verified
# dittobench-api build context. Compose 2.40 and 5.0 can corrupt remote Git
# context URLs while converting builds to Bake; a local context avoids that
# compatibility bug without weakening the repository's ref+checksum pin.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/docker-compose.yml"
STATE_DIR="${DITTO_VALIDATOR_UPDATE_STATE_DIR:-$ROOT_DIR/.validator-update}"
RUNTIME_STATE_PATH=/tmp/ditto-validator-update-state.json
DRAINED_VALIDATOR=""
SCORER_REPLACEMENT_STARTED=false
SCORER_REPLACED=false
CLEANUP_ACTIVE=false

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

require_absolute_path() {
  local name="$1"
  local value="$2"
  case "$value" in
    /*) ;;
    *) die "$name must be an absolute path" ;;
  esac
  case "$value" in
    *$'\n'* | *$'\r'*) die "$name must not contain a newline" ;;
  esac
}

require_positive_integer() {
  local name="$1"
  local value="$2"
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || die "$name must be a positive integer"
}

compose_container() {
  docker compose --project-directory "$ROOT_DIR" -f "$COMPOSE_FILE" \
    ps -q "$1" 2>/dev/null || true
}

validator_runtime_state() {
  docker exec "$1" sh -c "cat '$RUNTIME_STATE_PATH'" 2>/dev/null || true
}

validator_bootstrap_token() {
  docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$1" \
    2>/dev/null | awk -F= '$1=="VALIDATOR_BOOTSTRAP_TOKEN" {print substr($0,index($0,"=")+1); exit}'
}

validator_state_is() {
  local state="$1"
  local expected="$2"
  [[ "$state" == *'"platform_accepted":true'* ]] && \
    [[ "$state" == *\"state\":\"$expected\"* ]]
}

wait_for_validator_state() {
  local container="$1"
  local timeout="$2"
  shift 2
  local deadline state expected
  deadline=$((SECONDS+timeout))
  while ((SECONDS<deadline)); do
    state="$(validator_runtime_state "$container")"
    for expected in "$@"; do
      if validator_state_is "$state" "$expected"; then
        return 0
      fi
    done
    sleep 1
  done
  return 1
}

wait_for_scorer_healthy() {
  local container="$1"
  local timeout="$2"
  local deadline running health
  deadline=$((SECONDS+timeout))
  while ((SECONDS<deadline)); do
    running="$(docker inspect --format '{{.State.Running}}' "$container" 2>/dev/null || true)"
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container" 2>/dev/null || true)"
    if [ "$running" = true ] && [ "$health" = healthy ]; then
      return 0
    fi
    sleep 1
  done
  return 1
}

resume_source_validator() {
  local container="$1"
  local timeout="$2"
  docker kill --signal=USR2 "$container" >/dev/null || return 1
  wait_for_validator_state "$container" "$timeout" ready working
}

request_source_validator_drain() {
  local container="$1"
  local timeout="$2"
  printf 'draining validator before reconciling the live scoring stack\n' >&2
  docker kill --signal=USR1 "$container" >/dev/null || \
    die "could not request validator drain before scorer replacement"
  DRAINED_VALIDATOR="$container"
  if ! wait_for_validator_state "$container" "$timeout" drained; then
    resume_source_validator "$container" 30 || true
    DRAINED_VALIDATOR=""
    die "validator did not drain before scorer replacement"
  fi
}

cleanup() {
  local status="$1"
  [ "$CLEANUP_ACTIVE" = false ] || exit "$status"
  CLEANUP_ACTIVE=true
  trap - EXIT INT TERM
  set +e
  if [ -n "$DRAINED_VALIDATOR" ]; then
    if [ "$SCORER_REPLACEMENT_STARTED" = false ] || [ "$SCORER_REPLACED" = true ]; then
      resume_source_validator "$DRAINED_VALIDATOR" 30 || \
        printf 'CRITICAL: could not verify validator resume after interrupted scorer replacement\n' >&2
    else
      printf 'CRITICAL: scorer replacement was interrupted; validator remains drained for safe operator recovery\n' >&2
    fi
  fi
  exit "$status"
}

trap 'cleanup $?' EXIT
trap 'cleanup 130' INT
trap 'cleanup 143' TERM

command -v docker >/dev/null 2>&1 || die "Docker is not installed"
command -v git >/dev/null 2>&1 || die "git is not installed"

wallets_dir="${DITTO_BITTENSOR_WALLETS_DIR:-$HOME/.bittensor/wallets}"
require_absolute_path DITTO_BITTENSOR_WALLETS_DIR "$wallets_dir"
export DITTO_BITTENSOR_WALLETS_DIR="$wallets_dir"

if [ "$#" -eq 0 ]; then
  die "usage: $0 <docker compose arguments>"
fi

compose_version="$(docker compose version --short 2>/dev/null)" || \
  die "Docker Compose plugin v2 or newer is required"
compose_version="${compose_version#v}"
compose_major="${compose_version%%.*}"
case "$compose_major" in
  '' | *[!0-9]*) die "could not parse Docker Compose version: $compose_version" ;;
esac
if [ "$compose_major" -lt 2 ]; then
  die "Docker Compose plugin v2 or newer is required (found $compose_version)"
fi

docker info >/dev/null 2>&1 || die "Docker Engine is not reachable"
docker buildx version >/dev/null 2>&1 || die "Docker Buildx is not installed"

# Read-only/lifecycle commands do not consume a build context. Keep them usable
# with already-built images and during a GitHub outage; ancestry is verified
# only before a command that can materialize a scorer image.
materialize_context=false
up_command=false
for argument in "$@"; do
  case "$argument" in
    up) materialize_context=true; up_command=true ;;
    build | create | run | --build) materialize_context=true ;;
  esac
done
if [ "$materialize_context" != "true" ]; then
  exec docker compose --project-directory "$ROOT_DIR" -f "$COMPOSE_FILE" "$@"
fi

source_revision="$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null)" || \
  die "could not resolve ditto-subnet source revision"
if [[ ! "$source_revision" =~ ^[0-9a-f]{40}$ ]]; then
  die "ditto-subnet source revision must be a full Git SHA"
fi
export DITTO_SOURCE_REVISION="$source_revision"
export DITTO_SOURCE_IDENTITY="local-source:$source_revision"

context_override="${DITTOBENCH_BUILD_CONTEXT:-}"
if [ -n "$context_override" ]; then
  pinned_contexts="$context_override"
else
  pinned_contexts="$(
    grep -Eo \
      'https://github\.com/ditto-assistant/dittobench-api\.git\?ref=[^&[:space:]]+&checksum=[0-9a-f]{40}' \
      "$COMPOSE_FILE" || true
  )"
fi
if [ -z "$pinned_contexts" ] || [ "$(printf '%s\n' "$pinned_contexts" | sort -u | wc -l | tr -d ' ')" -ne 1 ]; then
  die "docker-compose.yml must contain exactly one structured Git context pin"
fi
pinned_contexts="$(printf '%s\n' "$pinned_contexts" | sort -u)"

repository="${pinned_contexts%%\?*}"
ref_and_checksum="${pinned_contexts#*\?ref=}"
ref="${ref_and_checksum%%&checksum=*}"
checksum="${pinned_contexts##*checksum=}"
[ "$repository" = "https://github.com/ditto-assistant/dittobench-api.git" ] || \
  die "dittobench-api context must use the official repository"
local_smoke=false
if [ "$ref" != "refs/heads/main" ]; then
  if [ "${DITTOBENCH_ALLOW_UNMERGED_SMOKE:-false}" != "true" ] || \
    [ "${SUBTENSOR_NETWORK:-finney}" != "local" ]; then
    die "unmerged dittobench-api refs require local network smoke opt-in"
  fi
  case "$ref" in
    refs/heads/*) local_smoke=true ;;
    *) die "local dittobench-api smoke ref must be a branch" ;;
  esac
fi
case "$checksum" in
  *[!0-9a-f]* | '') die "invalid dittobench-api checksum in docker-compose.yml" ;;
esac
if [ "${#checksum}" -ne 40 ]; then
  die "dittobench-api checksum must be a full 40-character commit"
fi

cache_home="${DITTO_SUBNET_BUILD_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/ditto-subnet}"
checkout="$cache_home/dittobench-api/$checksum"
verified_marker="$checkout.ref-verified"
verification_record="$ref $checksum"

if [ -e "$checkout" ] && [ ! -d "$checkout/.git" ]; then
  die "cache path exists but is not a Git checkout: $checkout"
fi

if [ ! -d "$checkout/.git" ]; then
  mkdir -p "$(dirname "$checkout")"
  git init --quiet "$checkout"
  git -C "$checkout" remote add origin "$repository"
fi

origin="$(git -C "$checkout" remote get-url origin)"
if [ "$origin" != "$repository" ]; then
  die "cached dittobench-api origin is $origin, expected $repository"
fi

if ! git -C "$checkout" cat-file -e "$checksum^{commit}" 2>/dev/null; then
  printf 'fetching pinned dittobench-api context %s (%s)\n' \
    "$checksum" "$ref" >&2
  git -C "$checkout" fetch --quiet --depth 1 origin "$checksum"
  resolved="$(git -C "$checkout" rev-parse FETCH_HEAD)"
  if [ "$resolved" != "$checksum" ]; then
    die "dittobench-api fetch resolved to $resolved, expected $checksum"
  fi
fi

# A checksum may intentionally lag main, but it must have landed there. Verify
# ancestry once, then cache that evidence beside the immutable SHA checkout so
# routine restarts remain independent of GitHub availability.
if [ ! -f "$verified_marker" ] || \
  [ "$(cat "$verified_marker")" != "$verification_record" ]; then
  printf 'verifying pinned dittobench-api commit is on %s\n' "$ref" >&2
  git -C "$checkout" fetch --quiet origin "$ref" || \
    die "could not fetch dittobench-api $ref for ancestry verification"
  resolved_ref="$(git -C "$checkout" rev-parse FETCH_HEAD)"
  if [ "$local_smoke" = "true" ] && [ "$resolved_ref" != "$checksum" ]; then
    die "local smoke checksum $checksum is not the current $ref commit"
  elif [ "$local_smoke" != "true" ] && \
    ! git -C "$checkout" merge-base --is-ancestor "$checksum" FETCH_HEAD; then
    die "dittobench-api checksum $checksum is not in $ref history"
  fi
  printf '%s\n' "$verification_record" > "$verified_marker"
fi

if [ -n "$(
  git -c core.fileMode=false -C "$checkout" status \
    --porcelain=v1 --untracked-files=all --ignored=matching
)" ]; then
  die "cached dittobench-api checkout has local changes: $checkout"
fi

git -C "$checkout" checkout --quiet --detach "$checksum"

# Git's immutable tree is authoritative for executable modes. Some migrations,
# archive restores, and filesystems preserve contents but drop +x, which can
# make a later Docker build fail even though the cached checkout has the right
# commit. Repair mode-only drift, then fail closed if any other drift remains.
while IFS=' ' read -r -d '' tracked_mode tracked_path; do
  case "$tracked_mode" in
    100755) chmod 0755 "$checkout/$tracked_path" ;;
    100644) chmod 0644 "$checkout/$tracked_path" ;;
  esac
done < <(
  git -C "$checkout" ls-tree -r -z \
    --format='%(objectmode) %(path)' "$checksum"
)

if [ -n "$(
  git -c core.fileMode=true -C "$checkout" status \
    --porcelain=v1 --untracked-files=all --ignored=matching
)" ]; then
  die "cached dittobench-api checkout could not be normalized: $checkout"
fi

head_checksum="$(git -C "$checkout" rev-parse HEAD)"
if [ "$head_checksum" != "$checksum" ]; then
  die "cached dittobench-api checkout is $head_checksum, expected $checksum"
fi
if [ ! -f "$checkout/Dockerfile" ]; then
  die "pinned dittobench-api checkout has no Dockerfile: $checkout"
fi

export DITTOBENCH_BUILD_CONTEXT="$checkout"
printf 'using dittobench-api %s with Docker Compose %s\n' \
  "$checksum" "$compose_version" >&2

# Compose reuses an already-built scorer image whenever one exists. A stack
# update therefore recreates dittobench-api with the NEW pinned environment
# while the OLD binary keeps running: the scorer then asserts a revision it was
# not built from, the validator's identity check passes, and the fleet silently
# serves an outdated benchmark set. Rebuild for real — and replace the running
# containers — whenever the pinned revision differs from the one last built
# here. The marker is keyed on the pin, so an unchanged pin never pays for a
# rebuild and a routine restart stays fast and independent of the network.
mkdir -p "$STATE_DIR"
built_revision_file="$STATE_DIR/dittobench-built-revision"
built_revision=""
if [ -f "$built_revision_file" ] && [ ! -L "$built_revision_file" ]; then
  built_revision="$(awk 'NF { print; exit }' "$built_revision_file")"
fi
if [ "$built_revision" != "$checksum" ]; then
  printf 'pinned dittobench-api revision changed (%s -> %s); rebuilding the scorer\n' \
    "${built_revision:-none}" "$checksum" >&2
  docker compose --project-directory "$ROOT_DIR" -f "$COMPOSE_FILE" \
    build --pull dittobench-api || \
    die "could not rebuild dittobench-api at pinned revision $checksum"
  # A freshly built image only matters once it is the image that RUNS. Recreate
  # the scorer container when it already exists, so a targeted command such as
  # `up --no-deps ditto-subnet` cannot leave the previous scorer serving. A
  # stack that is not up yet needs nothing here: its first `up` starts them
  # from the image just built.
  running="$(compose_container dittobench-api)"
  if [ -n "$running" ]; then
    validator_container="$(compose_container ditto-subnet)"
    if [ -n "$validator_container" ]; then
      drain_timeout="${DITTO_VALIDATOR_COMPOSE_DRAIN_TIMEOUT_SECONDS:-4800}"
      require_positive_integer \
        DITTO_VALIDATOR_COMPOSE_DRAIN_TIMEOUT_SECONDS "$drain_timeout"
      request_source_validator_drain "$validator_container" "$drain_timeout"
    fi
    SCORER_REPLACEMENT_STARTED=true
    docker compose --project-directory "$ROOT_DIR" -f "$COMPOSE_FILE" \
      up -d --no-deps --no-build dittobench-api || \
      die "could not restart dittobench-api on the rebuilt image"
    running="$(compose_container dittobench-api)"
    ready_timeout="${DITTO_VALIDATOR_COMPOSE_READY_TIMEOUT_SECONDS:-180}"
    require_positive_integer \
      DITTO_VALIDATOR_COMPOSE_READY_TIMEOUT_SECONDS "$ready_timeout"
    if [ -z "$running" ] || ! wait_for_scorer_healthy "$running" "$ready_timeout"; then
      die "rebuilt scorer did not become healthy; validator remains drained"
    fi
    SCORER_REPLACED=true
    if [ -n "$DRAINED_VALIDATOR" ] && [ "$up_command" != true ]; then
      resume_source_validator "$DRAINED_VALIDATOR" 30 || \
        die "rebuilt scorer is running but validator resume could not be verified"
      DRAINED_VALIDATOR=""
    fi
  fi
fi

if [ "$up_command" = true ]; then
  drain_timeout="${DITTO_VALIDATOR_COMPOSE_DRAIN_TIMEOUT_SECONDS:-4800}"
  ready_timeout="${DITTO_VALIDATOR_COMPOSE_READY_TIMEOUT_SECONDS:-180}"
  require_positive_integer \
    DITTO_VALIDATOR_COMPOSE_DRAIN_TIMEOUT_SECONDS "$drain_timeout"
  require_positive_integer \
    DITTO_VALIDATOR_COMPOSE_READY_TIMEOUT_SECONDS "$ready_timeout"
  # Bind bootstrap drain state to the fully rendered source stack. Any source,
  # pin, image, or operator configuration change gets a new token, so a
  # replacement validator cannot claim work while Compose is still reconciling
  # its scorer. A no-op `up` keeps the same token and remains a no-op.
  stack_identity="$(
    VALIDATOR_START_DRAINED=true VALIDATOR_BOOTSTRAP_TOKEN=source-template \
      docker compose --project-directory "$ROOT_DIR" -f "$COMPOSE_FILE" \
      config | git hash-object --stdin
  )" || die "could not derive the source stack deployment identity"
  [[ "$stack_identity" =~ ^[0-9a-f]{40}$ ]] || \
    die "source stack deployment identity is malformed"
  bootstrap_token="source-$stack_identity"
  for argument in "$@"; do
    case "$argument" in
      --force-recreate | --renew-anon-volumes | -V)
        bootstrap_token="$bootstrap_token-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
        break
        ;;
    esac
  done

  validator_container="$(compose_container ditto-subnet)"
  if [ -z "$DRAINED_VALIDATOR" ] && [ -n "$validator_container" ] && \
    [ "$(validator_bootstrap_token "$validator_container")" != "$bootstrap_token" ]; then
    request_source_validator_drain "$validator_container" "$drain_timeout"
  fi

  SCORER_REPLACEMENT_STARTED=true
  SCORER_REPLACED=false
  if ! VALIDATOR_START_DRAINED=true VALIDATOR_BOOTSTRAP_TOKEN="$bootstrap_token" \
    docker compose --project-directory "$ROOT_DIR" -f "$COMPOSE_FILE" "$@"; then
    die "source stack reconciliation failed; validator remains drained"
  fi
  running="$(compose_container dittobench-api)"
  if [ -n "$running" ] && ! wait_for_scorer_healthy "$running" "$ready_timeout"; then
    die "reconciled scorer did not become healthy; validator remains drained"
  fi
  SCORER_REPLACED=true
  validator_container="$(compose_container ditto-subnet)"
  if [ -n "$DRAINED_VALIDATOR" ]; then
    [ -n "$validator_container" ] || \
      die "source stack has no running validator after reconciliation"
    DRAINED_VALIDATOR="$validator_container"
    wait_for_validator_state "$validator_container" "$ready_timeout" drained || \
      die "replacement validator did not remain drained during reconciliation"
    resume_source_validator "$validator_container" "$ready_timeout" || \
      die "source stack is healthy but validator resume could not be verified"
    DRAINED_VALIDATOR=""
  elif [ -n "$validator_container" ]; then
    state="$(validator_runtime_state "$validator_container")"
    if validator_state_is "$state" drained; then
      DRAINED_VALIDATOR="$validator_container"
      resume_source_validator "$validator_container" "$ready_timeout" || \
        die "new source validator resume could not be verified"
      DRAINED_VALIDATOR=""
    elif ! validator_state_is "$state" ready && ! validator_state_is "$state" working; then
      die "source validator did not publish a safe post-reconciliation state"
    fi
  fi
  if [ "$built_revision" != "$checksum" ]; then
    printf '%s\n' "$checksum" > "$built_revision_file"
  fi
  exit 0
fi

if [ "$built_revision" != "$checksum" ]; then
  printf '%s\n' "$checksum" > "$built_revision_file"
fi
exec docker compose --project-directory "$ROOT_DIR" -f "$COMPOSE_FILE" "$@"
