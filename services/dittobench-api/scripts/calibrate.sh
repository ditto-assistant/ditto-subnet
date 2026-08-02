#!/usr/bin/env bash
# calibrate.sh — measure DittoBench test-difficulty variance end to end, with no
# OpenRouter key.
#
# Builds + starts the deterministic reference harness (cmd/refharness) and the
# API, runs K direct-path evals on freshly generated datasets (rotating seed),
# and reports the mean + stddev of the scores plus a fixed-seed noise floor.
# Because the harness is deterministic, the score spread IS the dataset-difficulty
# spread. Exit status is the calibrate gate (0 = stddev within --max-stddev).
#
# Usage:
#   scripts/calibrate.sh [RUNS] [N] [EXTRA calibrate flags...]
#     RUNS  number of runs (default 100)
#     N     tool cases per run (default 196 = 14*14, a balanced stratified set)
set -euo pipefail

RUNS="${1:-100}"
N="${2:-196}"
shift || true; shift || true
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'kill "${RH:-}" "${API:-}" 2>/dev/null || true; rm -rf "$TMP"' EXIT

echo "building binaries..."
go build -o "$TMP/refharness"     "$ROOT/cmd/refharness"
go build -o "$TMP/dittobench-api" "$ROOT/cmd/dittobench-api"
go build -o "$TMP/calibrate"      "$ROOT/cmd/calibrate"

"$TMP/refharness" -port 9000 >"$TMP/refharness.log" 2>&1 &
RH=$!
# allow-private also disables the per-IP submit rate limit (trusted local mode),
# so the calibration loop isn't throttled.
DITTOBENCH_ALLOW_PRIVATE_HARNESS=1 "$TMP/dittobench-api" -port 8000 >"$TMP/api.log" 2>&1 &
API=$!

for _ in $(seq 1 50); do
  if curl -sf localhost:8000/health >/dev/null 2>&1 && curl -sf localhost:9000/health >/dev/null 2>&1; then
    break
  fi
  sleep 0.2
done

"$TMP/calibrate" --runs "$RUNS" --n "$N" --repeat-seed 999 "$@"
