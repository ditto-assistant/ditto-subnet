#!/usr/bin/env bash
set -euo pipefail

project="ditto-embedding-gateway-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-$$}"
probe_image="curlimages/curl:8.14.1@sha256:9a1ed35addb45476afa911696297f8e115993df459278ed036182dd2cd22b67b"

export PYLON_TOKEN=local-validation
export VALIDATOR_WALLET_NAME=local-validation
export VALIDATOR_WALLET_HOTKEY=local-validation
export VALIDATOR_HOTKEY=local-validation
export VALIDATOR_PLATFORM_API_URL=http://127.0.0.1:9
export VALIDATOR_BENCHMARK_CAPACITY=1

compose=(docker compose --project-name "$project")

cleanup() {
  "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Boot the production source-Compose topology: the scorer shares the privileged
# DinD service's network namespace, while the probe itself runs inside that
# nested daemon on the same restricted bridges used by miner harnesses.
"${compose[@]}" up --detach --build --wait --wait-timeout 300 \
  sandbox-docker dittobench-api

"${compose[@]}" exec --no-TTY sandbox-docker sh -ceu '
  netstat -lnt | grep -Eq "0\.0\.0\.0:11436[[:space:]]"
'

"${compose[@]}" exec --no-TTY sandbox-docker docker pull "$probe_image" >/dev/null

probe_gateway() {
  local network="$1"
  "${compose[@]}" exec --no-TTY sandbox-docker \
    docker run --rm \
      --network "$network" \
      --add-host host.docker.internal:host-gateway \
      "$probe_image" \
      --fail --silent --show-error --max-time 5 \
      http://host.docker.internal:11436/health >/dev/null
}

probe_gateway ditto-sandbox

"${compose[@]}" exec --no-TTY sandbox-docker \
  docker network create \
    --driver bridge \
    --opt com.docker.network.bridge.name=dtjgatewaysmk \
    --opt com.docker.network.bridge.enable_icc=false \
    ditto-job-gateway-smoke >/dev/null
trap '"${compose[@]}" exec --no-TTY sandbox-docker docker network rm ditto-job-gateway-smoke >/dev/null 2>&1 || true; cleanup' EXIT

probe_gateway ditto-job-gateway-smoke

# Reaching the compatibility broker must not accidentally widen the sandbox to
# the scorer control API. Port 8000 remains denied by the same INPUT policy.
if "${compose[@]}" exec --no-TTY sandbox-docker \
  docker run --rm \
    --network ditto-job-gateway-smoke \
    --add-host host.docker.internal:host-gateway \
    "$probe_image" \
    --fail --silent --show-error --max-time 3 \
    http://host.docker.internal:8000/health >/dev/null 2>&1; then
  printf 'sandbox unexpectedly reached scorer control API on port 8000\n' >&2
  exit 1
fi

printf 'validator embedding gateway smoke passed on shared and per-run networks\n'
