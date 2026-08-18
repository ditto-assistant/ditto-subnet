#!/usr/bin/env bash
# smoke.sh — the FREE end-to-end plumbing smoke.
#
# Runs the scored bench_version=12 pipeline against the deterministic refharness
# with the relay in STUB mode, so NO OpenRouter calls are made ($0). It exercises
# dataset generation, the dataset_sha256 pin, the relay preflight/accounting, the
# run loop and the scoring/gate wiring end to end. refharness makes no model
# calls, so the scored v9+ evidence cannot settle — the run is EXPECTED to fail
# closed at the model-use/attribution boundary. That failure is the proof the
# scored v12 path is wired correctly.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export STUB=1
AGENT_URL=refharness BENCH="${BENCH:-12}" RUN_SIZE="${RUN_SIZE:-small}" \
  SEED="${SEED:-42}" SCORED="${SCORED:-1}" exec "$HERE/bench.sh"
