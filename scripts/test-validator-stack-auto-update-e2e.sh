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

cleanup() {
  docker compose --project-name "$PROJECT_NAME" --env-file "$ENV_FILE" \
    -f "$COMPOSE_FILE" -f "$FIXTURE_FILE" down --volumes --remove-orphans \
    >/dev/null 2>&1 || true
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

# CI deliberately uses a four-vCPU daemon. The candidate must be creatable on
# that documented minimum, while the production default remains uncapped.
daemon_cpus="$(docker info --format '{{.NCPU}}')"
if [ -n "${CI:-}" ] && [ "$daemon_cpus" -ne 4 ]; then
  echo "expected the CI Docker daemon to expose exactly 4 CPUs; found $daemon_cpus" >&2
  exit 1
fi

compose=(docker compose --project-name "$PROJECT_NAME" --env-file "$ENV_FILE" \
  -f "$COMPOSE_FILE" -f "$FIXTURE_FILE")
"${compose[@]}" pull sandbox-docker
"${compose[@]}" create --no-build sandbox-docker
container_id="$("${compose[@]}" ps --all -q sandbox-docker)"
test -n "$container_id"
test "$(docker inspect --format '{{.HostConfig.NanoCpus}}' "$container_id")" = "0"
"${compose[@]}" start sandbox-docker
test "$(docker inspect --format '{{.State.Running}}' "$container_id")" = "true"

echo "validator stack updater release gate passed"
