#!/usr/bin/env bash
set -euo pipefail

compose_file="${1:?usage: test-validator-stack-release-runtime.sh COMPOSE_FILE}"
project="ditto-release-smoke-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}"

cleanup() {
  VALIDATOR_STACK_DESCRIPTOR_REF=release-smoke \
    docker compose --project-name "$project" --file "$compose_file" \
      down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

export VALIDATOR_STACK_DESCRIPTOR_REF=release-smoke
# Compose interpolates required variables for the whole generated model even
# when this smoke boots only the sandbox and Ollama dependencies. Production
# hosts replace this obvious fixture with their public validator hotkey.
export VALIDATOR_HOTKEY=release-smoke

# The release bundle uses pull_policy=never so production never substitutes a
# floating tag. Pull the exact digest-bound images explicitly, then boot the
# actual generated runtime services rather than a source-Compose fixture.
for service in sandbox-docker ollama; do
  image="$(docker compose --project-name "$project" --file "$compose_file" \
    config --format json | jq -er --arg service "$service" \
      '.services[$service].image')"
  docker pull --platform linux/amd64 "$image" >/dev/null
  docker image inspect "$image" >/dev/null
done

docker compose --project-name "$project" --file "$compose_file" \
  up --detach --wait sandbox-docker ollama

docker compose --project-name "$project" --file "$compose_file" \
  exec --no-TTY sandbox-docker docker info >/dev/null
docker compose --project-name "$project" --file "$compose_file" \
  exec --no-TTY ollama sh -ceu \
    'test -d /root/.ollama; touch /root/.ollama/.release-smoke; rm /root/.ollama/.release-smoke'
