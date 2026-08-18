#!/usr/bin/env bash
# bench.sh — drive ONE bench run end to end against the localstack and print the
# score report.
#
# Usage:
#   localstack/bench.sh                       # scored v12 refharness (needs stack up)
#   AGENT_URL=http://localhost:7000 \
#   BENCH=12 RUN_SIZE=small SEED=42 SCORED=1 localstack/bench.sh
#
# Env:
#   AGENT_URL   harness base URL. Empty or "refharness" => build+start the bundled
#               deterministic reference harness (no model, no key).
#   BENCH       bench_version (default 12)
#   RUN_SIZE    small | medium | full (default small)
#   SEED        dataset seed (default 42)
#   SCORED      1 => pin dataset_sha256 (SCORED scope, activates the v9-base
#               evidence + gates). 0 => practice scope (no gates). Default 1.
#   STUB        1 => relay stub mode (free). Forwarded to stack.sh if it starts.
#
# The stack (relay + api) is auto-started if it is not already healthy; it is
# left running so you can iterate. Tear it down with localstack/stack.sh down.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib.sh"

AGENT_URL="${AGENT_URL:-refharness}"
BENCH="${BENCH:-12}"
RUN_SIZE="${RUN_SIZE:-small}"
SEED="${SEED:-42}"
SCORED="${SCORED:-1}"

# 1. Ensure the deps are up.
if ! curl -sf "http://localhost:${API_PORT}/health" >/dev/null 2>&1; then
  log "stack not up; starting it"
  "$HERE/stack.sh" up
else
  log "reusing running stack on :${API_PORT}"
fi

# 2. Resolve the harness. refharness is bundled and needs no key.
if [[ "$AGENT_URL" == "refharness" || -z "$AGENT_URL" ]]; then
  if ! curl -sf "http://localhost:${REFHARNESS_PORT}/health" >/dev/null 2>&1; then
    [[ -x "$BIN_DIR/refharness" ]] || build_binaries
    "$BIN_DIR/refharness" -port "$REFHARNESS_PORT" >"$RUN_DIR/refharness.log" 2>&1 &
    echo $! >"$RUN_DIR/refharness.pid"
    wait_health "http://localhost:${REFHARNESS_PORT}/health" "refharness"
  fi
  AGENT_URL="http://localhost:${REFHARNESS_PORT}"
  warn "using refharness: it makes NO model calls, so a SCORED v9+ run cannot"
  warn "settle the model_use / model_dependence evidence (expected to fail closed)."
fi
log "harness = $AGENT_URL   bench=$BENCH run_size=$RUN_SIZE seed=$SEED scored=$SCORED"

# 3. Build the submit body. For SCORED runs, compute the dataset_sha256 the
#    scorer will re-derive and reject on mismatch.
SHA=""
if [[ "$SCORED" == "1" ]]; then
  log "computing expected_dataset_sha256 for (seed=$SEED, bench=$BENCH, $RUN_SIZE)..."
  SHA="$(dataset_sha "$SEED" "$BENCH" "$RUN_SIZE" | tail -1)"
  log "dataset_sha256 = $SHA"
fi

BODY="$(AGENT_URL="$AGENT_URL" BENCH="$BENCH" RUN_SIZE="$RUN_SIZE" SEED="$SEED" SHA="$SHA" python3 - <<'PY'
import json, os
body = {
    "harness_url": os.environ["AGENT_URL"],
    "bench_version": int(os.environ["BENCH"]),
    "run_size": os.environ["RUN_SIZE"],
    "seed": int(os.environ["SEED"]),
}
if os.environ.get("SHA"):
    body["dataset_sha256"] = os.environ["SHA"]
print(json.dumps(body))
PY
)"

log "POST /v1/submit  $BODY"
RID="$(curl -s -X POST "http://localhost:${API_PORT}/v1/submit" \
  -H 'Content-Type: application/json' -d "$BODY" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("run_id") or ("ERROR: "+json.dumps(d)))')"
if [[ "$RID" == ERROR:* ]]; then die "submit rejected: ${RID#ERROR: }"; fi
log "run_id = $RID"

# 4. Poll to a terminal state.
for _ in $(seq 1 1800); do
  JOB="$(curl -s "http://localhost:${API_PORT}/v1/runs/$RID")"
  ST="$(printf '%s' "$JOB" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')"
  case "$ST" in
    done|failed) break ;;
    *) sleep 1 ;;
  esac
done

OUT="$RUN_DIR/report-$RID.json"
printf '%s' "$JOB" >"$OUT"
log "full run JSON saved to $OUT"

# 5. Human-readable report. Read the saved file (stdin is the heredoc program).
python3 - "$OUT" <<'PY'
import sys, json
job = json.load(open(sys.argv[1]))
print("\n================ localstack bench report ================")
print(f"status        {job.get('status')}")
print(f"bench_version {job.get('bench_version')}")
print(f"seed          {job.get('seed')}")
if job.get("status") == "failed":
    print(f"error         {job.get('error')}")
    fail = job.get("failure") or {}
    if fail:
        print(f"failure       {json.dumps(fail)}")
    print("========================================================")
    sys.exit(0)
r = job.get("report") or {}
def g(k, fmt="%s"):
    v = r.get(k)
    return (fmt % v) if v is not None else "-"
print(f"composite     {g('composite','%.6f')}")
print(f"raw_composite {g('raw_composite','%.6f')}")
print(f"tool_mean     {g('tool_mean','%.6f')}")
print(f"memory_mean   {g('memory_mean','%.6f')}   (LongMemEval / memory axis)")
print(f"median_ms     {g('median_ms')}")
det = r.get("details") or {}
te = det.get("token_efficiency") or {}
if te:
    print("-- token_efficiency --")
    print(f"  raw_composite      {te.get('raw_composite')}")
    print(f"  adjusted_composite {te.get('adjusted_composite')}")
    print(f"  formula_version    {te.get('formula_version')}")
v9 = det.get("v9_base") or {}
if v9:
    print("-- v9_base --")
    print(f"  effective_composite_micros {v9.get('effective_composite_micros')}")
    print(f"  applied_gate_factor_bps    {v9.get('applied_gate_factor_bps')}")
    sg = v9.get("score_gates") or {}
    for name in ("model_use", "authoritative_tool", "model_dependence"):
        gate = sg.get(name)
        if not gate:
            continue
        extra = ""
        if name == "model_dependence":
            extra = f" eligible={gate.get('eligible_cases')} dependent={gate.get('dependent_cases')} dependence_bps={gate.get('dependence_bps')} slice_complete={gate.get('slice_attribution_complete')}"
        print(f"  {name:<18} result={gate.get('result')} factor_bps={gate.get('factor_bps')}{extra}")
else:
    print("-- v9_base: absent (practice scope, or bench_version < 9) --")
print("========================================================")
PY
