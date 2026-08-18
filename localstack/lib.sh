#!/usr/bin/env bash
# lib.sh — shared config + helpers for the ditto-subnet bench localstack.
# Sourced by stack.sh, bench.sh and smoke.sh. Never run directly.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_DIR="$ROOT/services/dittobench-api"
DATAGEN_DIR="$ROOT/research/dittobench-datagen"
RUN_DIR="$ROOT/localstack/.run"
BIN_DIR="$RUN_DIR/bin"
mkdir -p "$RUN_DIR" "$BIN_DIR"

# Ports (overridable from the environment).
API_PORT="${API_PORT:-8000}"
RELAY_PORT="${RELAY_PORT:-11434}"
REFHARNESS_PORT="${REFHARNESS_PORT:-9000}"

# The sessionless direct-harness scored path reads the model relay at
# HARNESS_GATEWAY_URL. On the host (no Docker) it must be localhost, not the
# in-container host.docker.internal default.
export HARNESS_GATEWAY_URL="${HARNESS_GATEWAY_URL:-http://localhost:${RELAY_PORT}}"
export HARNESS_EMBED_URL="${HARNESS_EMBED_URL:-http://localhost:${RELAY_PORT}}"

log()  { printf '\033[36m[localstack]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[33m[localstack]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[localstack]\033[0m %s\n' "$*" >&2; exit 1; }

# fetch_openrouter_key prints the OpenRouter API key to stdout WITHOUT logging
# it. Env-first: a set OPENROUTER_API_KEY wins so no secret round-trip happens in
# CI or when a developer exports their own key. Otherwise it is pulled from GCP
# Secret Manager (project ditto-app-dev, secret LOCAL_OPENROUTER_API_KEY). The
# value is only ever assigned to a variable/redirected, never echoed.
fetch_openrouter_key() {
  if [[ -n "${OPENROUTER_API_KEY:-}" ]]; then
    printf '%s' "$OPENROUTER_API_KEY"
    return 0
  fi
  local project="${OPENROUTER_SECRET_PROJECT:-ditto-app-dev}"
  local secret="${OPENROUTER_SECRET_NAME:-LOCAL_OPENROUTER_API_KEY}"
  command -v gcloud >/dev/null 2>&1 || die "gcloud not found and OPENROUTER_API_KEY unset"
  gcloud secrets versions access latest --secret="$secret" --project="$project" 2>/dev/null \
    || die "could not read secret $secret from project $project (check ADC / gcloud auth)"
}

build_binaries() {
  log "building binaries (dittobench-api, localstack-relay, refharness)..."
  ( cd "$API_DIR" && go build -o "$BIN_DIR/dittobench-api" ./cmd/dittobench-api )
  ( cd "$API_DIR" && go build -o "$BIN_DIR/localstack-relay" ./cmd/localstack-relay )
  ( cd "$API_DIR" && go build -o "$BIN_DIR/refharness" ./cmd/refharness )
}

wait_health() {
  local url="$1" name="$2"
  for _ in $(seq 1 100); do
    if curl -sf "$url" >/dev/null 2>&1; then return 0; fi
    sleep 0.2
  done
  die "$name never became healthy at $url"
}

# dataset_sha computes the expected_dataset_sha256 the scorer will re-derive.
# The scorer regenerates the dataset in-process and FAILS the run on any
# mismatch (main.go:verifyDatasetHash), so this MUST reproduce those exact bytes.
# cmd/generate calls the very same gen.ProfileForVersion + gen.GenerateDataset the
# run path uses (submitRunSize -> runSizeJob), so its printed sha is authoritative.
dataset_sha() {
  local seed="$1" bench="$2" run_size="$3"
  ( cd "$DATAGEN_DIR" && go run ./cmd/generate \
      -bench-version "$bench" -seed "$seed" -run-size "$run_size" -sha )
}
