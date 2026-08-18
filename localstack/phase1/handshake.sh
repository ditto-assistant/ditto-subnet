#!/usr/bin/env bash
# handshake.sh — run the full broker inference-session handshake + a scored v12
# submit against the Phase-1 real stack (postgres + model-relay + TLS terminator
# on the host; dittobench-api broker + harness in a Linux container).
#
#   prepare (/v1/inference/session) -> exchange (/api/v1/inference/exchange, sr25519)
#   -> activate -> scored /v1/submit with inference_session_id + identity quad -> poll.
#
# Env: AGENT_URL (default http://127.0.0.1:9000, in-container loopback harness),
#      SEED (42), RUN_SIZE (small), BENCH (12).
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
P1="$ROOT/localstack/.run/phase1"
API="http://localhost:8010"
RELAY="https://localhost:8443"
CTOKEN="localdev"
GRANT_ID="00000000-0000-0000-0000-0000000000b2"
AGENT_ID="00000000-0000-0000-0000-0000000000a1"
SLOT_ID="slot-0"
AGENT_URL="${AGENT_URL:-http://127.0.0.1:9000}"
SEED="${SEED:-42}"; RUN_SIZE="${RUN_SIZE:-small}"; BENCH="${BENCH:-12}"

j() { python3 -c "import sys,json;print(json.load(sys.stdin)$1)"; }

echo "[0] re-seed grant (pending, fresh deadline) so the handshake is repeatable"
PGPASSWORD=ditto psql -h localhost -p 5442 -U ditto -d ditto -q -f "$P1/seed.sql" >/dev/null 2>&1 || true

echo "[1] prepare session"
PREP="$(curl -s -X POST "$API/v1/inference/session" -H "Authorization: Bearer $CTOKEN" -H 'Content-Type: application/json' -d '{}')"
echo "  $PREP"
SID="$(printf '%s' "$PREP" | j "['session_id']")"
ASECRET="$(printf '%s' "$PREP" | j "['activation_secret']")"
BPK="$(printf '%s' "$PREP" | j "['broker_public_key']")"

echo "[2] exchange (sr25519) -> grant"
REQ="$(cd "$ROOT/apps/platform" && uv run python ../../localstack/phase1/exchange_sign.py exchange "$GRANT_ID" "$BPK" 2>/dev/null | tail -1)"
HOTKEY="$(printf '%s' "$REQ" | j "['validator_hotkey']")"
EXCH="$(curl -sk -X POST "$RELAY/api/v1/inference/exchange" -H "X-Validator-Hotkey: $HOTKEY" -H 'Content-Type: application/json' -d "$REQ")"
echo "  $EXCH"
BEARER="$(printf '%s' "$EXCH" | j "['bearer']")"
PROXY_URL="$(printf '%s' "$EXCH" | j "['proxy_url']")"
EXPIRES="$(printf '%s' "$EXCH" | j "['expires_at']")"
GEN="$(printf '%s' "$EXCH" | j "['generation']")"
PROVIDER="$(printf '%s' "$EXCH" | j "['provider']")"
PROFILE="$(printf '%s' "$EXCH" | j "['profile_revision']")"
MODEL="$(printf '%s' "$EXCH" | j "['model']")"

echo "[3] activate session"
ACT="$(GRANT_ID="$GRANT_ID" AGENT_ID="$AGENT_ID" SLOT_ID="$SLOT_ID" ASECRET="$ASECRET" BEARER="$BEARER" \
  PROXY_URL="$PROXY_URL" EXPIRES="$EXPIRES" GEN="$GEN" PROVIDER="$PROVIDER" PROFILE="$PROFILE" MODEL="$MODEL" \
  python3 - <<'PY'
import json,os
print(json.dumps({
  "activation_secret": os.environ["ASECRET"],
  "grant_id": os.environ["GRANT_ID"],
  "agent_id": os.environ["AGENT_ID"],
  "slot_id": os.environ["SLOT_ID"],
  "ticket_deadline": os.environ["EXPIRES"],
  "bearer": os.environ["BEARER"],
  "proxy_url": os.environ["PROXY_URL"],
  "generation": int(os.environ["GEN"]),
  "expires_at": os.environ["EXPIRES"],
  "provider": os.environ["PROVIDER"],
  "profile_revision": os.environ["PROFILE"],
  "model": os.environ["MODEL"],
  "request_budget": 8192, "token_budget": 25000000,
  "embedding_request_budget": 100000, "embedding_token_budget": 1000000000,
  "max_output_tokens": 8192,
}))
PY
)"
ARESP="$(curl -s -X POST "$API/v1/inference/session/$SID/activate" -H "Authorization: Bearer $CTOKEN" -H 'Content-Type: application/json' -d "$ACT")"
echo "  activate -> $ARESP"

echo "[4] compute dataset_sha256"
SHA="$(cd "$ROOT/research/dittobench-datagen" && go run ./cmd/generate -bench-version "$BENCH" -seed "$SEED" -run-size "$RUN_SIZE" -sha 2>/dev/null | tail -1)"
echo "  sha=$SHA"

echo "[5] scored submit"
BODY="$(SID="$SID" GRANT_ID="$GRANT_ID" AGENT_ID="$AGENT_ID" SLOT_ID="$SLOT_ID" EXPIRES="$EXPIRES" \
  AGENT_URL="$AGENT_URL" BENCH="$BENCH" RUN_SIZE="$RUN_SIZE" SEED="$SEED" SHA="$SHA" python3 - <<'PY'
import json,os
print(json.dumps({
  "harness_url": os.environ["AGENT_URL"],
  "bench_version": int(os.environ["BENCH"]),
  "run_size": os.environ["RUN_SIZE"],
  "seed": int(os.environ["SEED"]),
  "dataset_sha256": os.environ["SHA"],
  "tarball_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "inference_session_id": os.environ["SID"],
  "inference_grant_id": os.environ["GRANT_ID"],
  "inference_agent_id": os.environ["AGENT_ID"],
  "inference_slot_id": os.environ["SLOT_ID"],
  "inference_ticket_deadline": os.environ["EXPIRES"],
}))
PY
)"
SUB="$(curl -s -X POST "$API/v1/submit" -H 'Content-Type: application/json' -d "$BODY")"
echo "  submit -> $SUB"
RID="$(printf '%s' "$SUB" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("run_id") or ("ERR:"+json.dumps(d)))')"
if [ "${RID#ERR:}" != "$RID" ]; then echo "SUBMIT REJECTED"; exit 1; fi
echo "  run_id=$RID"

echo "[6] poll"
for _ in $(seq 1 2400); do
  JOB="$(curl -s "$API/v1/runs/$RID")"
  ST="$(printf '%s' "$JOB" | j "['status']")"
  printf '\r  status=%s      ' "$ST"
  case "$ST" in done|failed) echo; break;; *) sleep 1;; esac
done
OUT="$P1/report-$RID.json"; printf '%s' "$JOB" >"$OUT"
echo "  saved $OUT"
# Release the source-bound session so the next handshake binds 127.0.0.1 cleanly
# (multiple sessions on one loopback IP make the source lease ambiguous).
curl -s -X DELETE "$API/v1/inference/session/$SID" -H "Authorization: Bearer $CTOKEN" >/dev/null 2>&1 || true
python3 - "$OUT" <<'PY'
import sys,json
j=json.load(open(sys.argv[1]))
print("\n===== phase1 run report =====")
print("status       ", j.get("status"))
print("bench_version", j.get("bench_version"), " seed", j.get("seed"))
if j.get("status")=="failed":
    print("error        ", j.get("error"))
    print("failure      ", json.dumps(j.get("failure")))
    sys.exit(0)
r=j["report"]; d=r.get("details") or {}
print("composite    ", r.get("composite"))
print("tool_mean    ", r.get("tool_mean"), " memory_mean", r.get("memory_mean"))
te=d.get("token_efficiency") or {}
print("token_eff    ", json.dumps({k:te.get(k) for k in ("raw_composite","adjusted_composite","formula_version")}))
v9=d.get("v9_base") or {}
sg=v9.get("score_gates") or {}
for n in ("model_use","authoritative_tool","model_dependence"):
    g=sg.get(n)
    if g: print(f"gate {n:18} result={g.get('result')} factor_bps={g.get('factor_bps')} "
                + (f"administered={g.get('administered_cases')} eligible={g.get('eligible_cases')} dependent={g.get('dependent_cases')} slice_complete={g.get('slice_attribution_complete')}" if n=='model_dependence' else ''))
print("effective_composite_micros", v9.get("effective_composite_micros"), " applied_gate_factor_bps", v9.get("applied_gate_factor_bps"))
PY
