#!/usr/bin/env bash
# single-run.sh — one DittoBench run on a freshly generated dataset, end to end,
# with no OpenRouter key required.
#
# It builds + starts the deterministic reference harness (cmd/refharness) and the
# API (cmd/dittobench-api), submits one direct-path run (fresh templated dataset,
# deterministic tool scoring), prints the full ScoreReport, and tears both down.
#
# Usage:
#   scripts/single-run.sh [N] [SEED]
#     N     number of tool cases (default 20)
#     SEED  pin the dataset seed for reproducibility (default: fresh/rotating)
#
# This exercises the tool-calling half only (the direct path uses no LLM judge or
# memory). The full SN118 pipeline (memory + judge) is the run_size path and needs
# a BYOK OpenRouter key + a real harness — see the README.
set -euo pipefail

N="${1:-20}"
SEED="${2:-0}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'kill "${RH:-}" "${API:-}" 2>/dev/null || true; rm -rf "$TMP"' EXIT

echo "building binaries..."
go build -o "$TMP/refharness" "$ROOT/cmd/refharness"
go build -o "$TMP/dittobench-api" "$ROOT/cmd/dittobench-api"

"$TMP/refharness" -port 9000 >"$TMP/refharness.log" 2>&1 &
RH=$!
DITTOBENCH_ALLOW_PRIVATE_HARNESS=1 "$TMP/dittobench-api" -port 8000 >"$TMP/api.log" 2>&1 &
API=$!

echo "waiting for health..."
for _ in $(seq 1 50); do
  if curl -sf localhost:8000/health >/dev/null 2>&1 && curl -sf localhost:9000/health >/dev/null 2>&1; then
    break
  fi
  sleep 0.2
done

body="{\"harness_url\":\"http://localhost:9000\",\"n\":${N},\"seed\":${SEED}}"
echo "submitting: $body"
rid="$(curl -s -X POST localhost:8000/v1/submit -H 'Content-Type: application/json' -d "$body" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["run_id"])')"

echo "=== ScoreReport (run $rid) ==="
curl -s "localhost:8000/v1/runs/$rid" | python3 -c '
import sys, json
r = json.load(sys.stdin)["report"]
print("seed       %d" % r["seed"])
print("n          %d" % r["n"])
print("composite  %.4f" % r["composite"])
print("tool_mean  %.4f" % r["tool_mean"])
print("median_ms  %d" % r["median_ms"])
'
