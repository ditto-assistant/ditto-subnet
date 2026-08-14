#!/usr/bin/env bash
# Locally reproducible release/update gate. This combines the production
# updater's transactional process tests with a real Docker daemon creation of
# the candidate Compose service on the documented minimum host shape.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/docker-compose.yml"
FIXTURE_FILE="$ROOT_DIR/ditto/tests/fixtures/validator-stack-runtime.yml"
PROJECT_NAME="ditto-updater-e2e-${GITHUB_RUN_ID:-$$}"
ENV_FILE="$(mktemp)"
BOOTSTRAP_IMAGE="ditto-updater-bootstrap-e2e:${GITHUB_RUN_ID:-local}"
BOOTSTRAP_VOLUMES=()

cleanup() {
  docker compose --project-name "$PROJECT_NAME" --env-file "$ENV_FILE" \
    -f "$COMPOSE_FILE" -f "$FIXTURE_FILE" down --volumes --remove-orphans \
    >/dev/null 2>&1 || true
  docker image rm "$BOOTSTRAP_IMAGE" >/dev/null 2>&1 || true
  for volume in "${BOOTSTRAP_VOLUMES[@]}"; do
    docker volume rm "$volume" >/dev/null 2>&1 || true
  done
  rm -f "$ENV_FILE"
}
trap cleanup EXIT

command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
docker info >/dev/null
docker compose version >/dev/null
cp "$ROOT_DIR/.env.example" "$ENV_FILE"

# Exercise the updater process itself, including the cooperative busy ->
# drained handoff and complete-stack replacement. External registry/signature
# boundaries remain deterministic fakes in this process-level suite.
uv run pytest \
  ditto/tests/test_validator_stack_auto_update.py \
  -k 'success_replaces_and_commits_the_complete_stack or waits_for_busy_validator_before_replacing_stack'

# Boot the exact scorer bootstrap in Docker with the same read-only root,
# capabilities, uid transition, and writable updater mount rendered into a
# signed managed descriptor. Docker-managed volumes let this test assign the
# Linux ownership used by production even when the host is macOS; the release
# contract test independently verifies the bind source. Only the incident
# target may change, while a managed peer must start without changing its
# updater bytes. A successful version command also proves the helper dropped far
# enough that its post-drop access check considers the retained mount read-only.
docker build \
  --file "$ROOT_DIR/services/dittobench-api/Dockerfile" \
  --target sandbox \
  --build-arg DITTOBENCH_SOFTWARE_VERSION=bootstrap-e2e \
  --build-arg DITTOBENCH_SOURCE_SHA=0000000000000000000000000000000000000000 \
  --tag "$BOOTSTRAP_IMAGE" \
  "$ROOT_DIR"

target_hotkey=5Cg3DiRfrgzB1XzN7VuqQNchTgZ8PzPbphMKmVvHobWSL118
peer_hotkey=5CFtzzb4vym9eysfeF9cxxp6D7gksuUVTKYNq1mchnrMs118
for validator_hotkey in "$target_hotkey" "$peer_hotkey"; do
  volume="ditto-updater-bootstrap-${GITHUB_RUN_ID:-local}-${validator_hotkey:0:8}"
  BOOTSTRAP_VOLUMES+=("$volume")
  docker volume create "$volume" >/dev/null
  docker run --rm --user 0:0 --entrypoint /bin/sh \
    --mount "type=volume,source=$volume,target=/host-scripts" \
    "$BOOTSTRAP_IMAGE" -ceu \
    'printf "legacy updater\n" >/host-scripts/validator-stack-auto-update.sh
     chown 1000:1000 /host-scripts /host-scripts/validator-stack-auto-update.sh
     chmod 0755 /host-scripts /host-scripts/validator-stack-auto-update.sh'
  docker run --rm \
    --read-only \
    --tmpfs /tmp \
    --user 0:0 \
    --security-opt no-new-privileges:true \
    --cap-drop ALL \
    --cap-add CHOWN \
    --cap-add DAC_OVERRIDE \
    --cap-add SETGID \
    --cap-add SETUID \
    --mount "type=volume,source=$volume,target=/opt/ditto/host-validator-scripts" \
    --env DITTOBENCH_BOOTSTRAP_VALIDATOR_STACK_UPDATER=true \
    --env "DITTOBENCH_BOOTSTRAP_VALIDATOR_STACK_UPDATER_TARGET_HOTKEY=$target_hotkey" \
    --env "DITTOBENCH_BOOTSTRAP_VALIDATOR_HOTKEY=$validator_hotkey" \
    "$BOOTSTRAP_IMAGE" version -json >/dev/null
done
docker run --rm --entrypoint /bin/sh \
  --mount "type=volume,source=${BOOTSTRAP_VOLUMES[0]},target=/host-scripts" \
  "$BOOTSTRAP_IMAGE" -ceu \
  'cmp /opt/ditto/validator-stack-auto-update.sh /host-scripts/validator-stack-auto-update.sh'
docker run --rm --entrypoint /bin/sh \
  --mount "type=volume,source=${BOOTSTRAP_VOLUMES[1]},target=/host-scripts" \
  "$BOOTSTRAP_IMAGE" -ceu \
  'grep -Fx "legacy updater" /host-scripts/validator-stack-auto-update.sh'

# The live managed fleet includes a two-vCPU validator. The candidate must be
# creatable with that limit, while the production default remains uncapped.
daemon_cpus="$(docker info --format '{{.NCPU}}')"
if [ -n "${CI:-}" ] && [ "$daemon_cpus" -lt 2 ]; then
  echo "expected the CI Docker daemon to expose at least 2 CPUs; found $daemon_cpus" >&2
  exit 1
fi

compose=(docker compose --project-name "$PROJECT_NAME" --env-file "$ENV_FILE" \
  -f "$COMPOSE_FILE" -f "$FIXTURE_FILE")
"${compose[@]}" pull sandbox-docker
"${compose[@]}" create --no-build sandbox-docker
container_id="$("${compose[@]}" ps --all -q sandbox-docker)"
test -n "$container_id"
test "$(docker inspect --format '{{.HostConfig.NanoCpus}}' "$container_id")" = "0"
docker update --cpus 2 "$container_id" >/dev/null
test "$(docker inspect --format '{{.HostConfig.NanoCpus}}' "$container_id")" = "2000000000"
"${compose[@]}" start sandbox-docker
test "$(docker inspect --format '{{.State.Running}}' "$container_id")" = "true"

echo "validator stack updater release gate passed"
