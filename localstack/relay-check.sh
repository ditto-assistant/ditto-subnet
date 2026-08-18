#!/usr/bin/env bash
# relay-check.sh — bounded proof that the localstack relay reaches REAL OpenRouter
# with the locked model. Starts the relay in real (non-stub) mode, sends ONE tiny
# chat completion for the locked model, and prints the completion snippet plus the
# /health token accounting. This is the single, cost-bounded real-inference check;
# it does NOT run a full scored bench (a scored v12 run additionally needs a
# broker inference session — see README "The v12 gate gap").
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib.sh"

export MODEL="${MODEL:-openai/gpt-oss-20b}"
build_binaries

KEY="$(fetch_openrouter_key)"
log "starting relay -> OpenRouter (real mode; key sourced, not printed)"
env PORT="$RELAY_PORT" OPENROUTER_API_KEY="$KEY" \
    ${OPENROUTER_BASE_URL:+OPENROUTER_BASE_URL="$OPENROUTER_BASE_URL"} \
    "$BIN_DIR/localstack-relay" >"$RUN_DIR/relay-check.log" 2>&1 &
RELAY_PID=$!
trap 'kill "$RELAY_PID" 2>/dev/null || true' EXIT
wait_health "http://localhost:${RELAY_PORT}/health" "relay"

log "sending ONE completion for the locked model ($MODEL)..."
REQ="$(python3 -c 'import json,os;print(json.dumps({"model":os.environ["MODEL"],"messages":[{"role":"user","content":"Reply with exactly: OK"}],"max_tokens":16,"temperature":0}))')"
RESP="$(curl -s -X POST "http://localhost:${RELAY_PORT}/v1/chat/completions" \
  -H 'Content-Type: application/json' -d "$REQ")"

echo "$RESP" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print("non-JSON upstream response:"); print(sys.stdin.read()); sys.exit(1)
ch = (d.get("choices") or [{}])[0]
msg = (ch.get("message") or {}).get("content")
print("upstream model :", d.get("model"))
print("finish_reason  :", ch.get("finish_reason"))
print("content        :", repr(msg))
print("usage          :", json.dumps(d.get("usage")))
if d.get("error"):
    print("error          :", json.dumps(d["error"]))
'
echo "-- relay /health accounting --"
curl -s "http://localhost:${RELAY_PORT}/health" | python3 -c '
import sys, json
h = json.load(sys.stdin)
for k in ("requests","successes","infrastructure_failures","usage_available","prompt_tokens","completion_tokens","provider","model","profile_revision"):
    print(f"  {k:<22} {h.get(k)}")
'
