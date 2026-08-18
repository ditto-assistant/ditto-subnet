#!/usr/bin/env bash
# up.sh — bring up the Phase-1 REAL-STACK bench localstack that completes a scored
# bench_version=12 run with the causal model-dependence gate + counterfactual
# FIRING, using a real Platform grant and real OpenRouter inference.
#
# Topology (chain + screening mocked; everything else real):
#   host:  postgres(:5442, docker)  model-relay(:8082)  tls-terminator(:8443 -> :8082)
#   container ds-broker (Linux): dittobench-api broker(:8010 published, :11436 internal)
#                                + a harness on :9000 (co-located so the broker's
#                                loopback source-binding + the harness->broker calls
#                                both see 127.0.0.1). SSL_CERT_FILE=/ca.pem lets Go
#                                (Linux) trust the dev terminator cert.
#
# The broker requires an https platform proxy URL with a signed Ed25519 proof; the
# terminator supplies TLS and the container trusts its CA via SSL_CERT_FILE — which
# Go honors on Linux (it does NOT on macOS, hence the container).
#
# Env:
#   HARNESS          model | refharness      (default model)
#   CONSTANT_ANSWER  set => the model harness ignores the model output and returns
#                    this fixed string (drives model_dependence fail-closed).
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
P1="$ROOT/localstack/.run/phase1"; mkdir -p "$P1/bin"
source "$ROOT/localstack/lib.sh" >/dev/null 2>&1
HOTKEY="$(cd "$ROOT/apps/platform" && uv run python ../../localstack/phase1/exchange_sign.py addr 2>/dev/null | tail -1)"
HARNESS="${HARNESS:-model}"
HARNESS_BIN="/bin/modelharness.linux"; [ "$HARNESS" = refharness ] && HARNESS_BIN="/bin/refharness.linux"

log(){ printf '\033[36m[phase1]\033[0m %s\n' "$*" >&2; }

# 1. dev cert (SAN host.docker.internal) — reuse if present.
if [ ! -f "$P1/cert.pem" ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -keyout "$P1/key.pem" -out "$P1/cert.pem" -days 3 \
    -subj "/CN=host.docker.internal" \
    -addext "subjectAltName=DNS:host.docker.internal,DNS:localhost,IP:127.0.0.1" 2>/dev/null
fi

# 2. build binaries: host tools + linux (CGO for the broker) in a golang container.
log "building host tools (model-relay, tls terminator)"
( cd "$ROOT/services/model-relay" && go build -o "$P1/bin/model-relay" ./cmd/model-relay )
( cd "$ROOT/services/dittobench-api" && go build -o "$P1/bin/tlsterm" ./cmd/localstack-tlsterm )
docker volume create ds_gocache >/dev/null 2>&1 || true; docker volume create ds_gomod >/dev/null 2>&1 || true
log "building linux binaries (dittobench-api[cgo], refharness, modelharness)"
docker run --rm -v "$ROOT":/src -w /src/services/dittobench-api \
  -v ds_gocache:/root/.cache/go-build -v ds_gomod:/go/pkg/mod -e GOOS=linux \
  golang:1.26.6-alpine sh -c "apk add --no-cache build-base >/dev/null 2>&1 && \
    CGO_ENABLED=1 go build -ldflags=\"-s -w -extldflags '-static'\" -o /src/localstack/.run/phase1/bin/dittobench-api.linux ./cmd/dittobench-api && \
    CGO_ENABLED=0 go build -o /src/localstack/.run/phase1/bin/refharness.linux ./cmd/refharness && \
    CGO_ENABLED=0 go build -o /src/localstack/.run/phase1/bin/modelharness.linux ./cmd/localstack-modelharness"

# 3. postgres + migrations.
log "postgres :5442 + alembic migrate"
( cd "$ROOT/apps/platform" && [ -f .env ] || cp .env.example .env
  grep -q '^POSTGRES_PORT=5442' .env || printf '\nPOSTGRES_PORT=5442\n' >> .env
  POSTGRES_PORT=5442 docker compose --profile local up -d --wait postgres >/dev/null 2>&1
  set -a && . ./.env && set +a && uv run alembic upgrade head >/dev/null 2>&1 )

# 4. model-relay (:8082) + tls terminator (:8443 -> 127.0.0.1:8082).
log "model-relay :8082 (-> OpenRouter) + tls terminator :8443"
KEY="$(fetch_openrouter_key)"
[ -f "$P1/tlsterm.pid" ] && kill "$(cat "$P1/tlsterm.pid")" 2>/dev/null || true
[ -f "$P1/model-relay.pid" ] && kill "$(cat "$P1/model-relay.pid")" 2>/dev/null || true
TLSTERM_CERT="$P1/cert.pem" TLSTERM_KEY="$P1/key.pem" TLSTERM_UPSTREAM="http://127.0.0.1:8082" PORT=8443 \
  "$P1/bin/tlsterm" >"$P1/tlsterm.log" 2>&1 & echo $! >"$P1/tlsterm.pid"
env POSTGRES_HOST=localhost POSTGRES_PORT=5442 POSTGRES_USER=ditto POSTGRES_PASSWORD=ditto POSTGRES_DB=ditto \
    API_PORT=8082 DITTO_ROLE=relay DITTO_INFERENCE_PROXY_ENABLED=true OPENROUTER_API_KEY="$KEY" \
    DITTO_INFERENCE_PUBLIC_BASE_URL=https://host.docker.internal:8443 \
    DITTO_DEV_ALLOW_UNPERMITTED_VALIDATOR=1 SUBTENSOR_NETWORK=local NETUID=118 \
    PYLON_OPEN_ACCESS_TOKEN=dummy DITTO_UPLOAD_PAYMENT_ADDRESS="$HOTKEY" \
    "$P1/bin/model-relay" >"$P1/model-relay.log" 2>&1 & echo $! >"$P1/model-relay.pid"
sleep 3

# 5. seed agents -> validator_tickets(issued) -> inference_grants(pending), v12.
log "seed grant path for hotkey $HOTKEY"
cat > "$P1/seed.sql" <<SQL
\\set hotkey '${HOTKEY}'
BEGIN;
DELETE FROM inference_grants WHERE grant_id='00000000-0000-0000-0000-0000000000b2';
DELETE FROM validator_tickets WHERE agent_id='00000000-0000-0000-0000-0000000000a1';
DELETE FROM agents WHERE agent_id='00000000-0000-0000-0000-0000000000a1';
INSERT INTO agents (agent_id, miner_hotkey, name, sha256, status)
VALUES ('00000000-0000-0000-0000-0000000000a1', :'hotkey', 'dittobench-local-seed',
        'deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef', 'evaluating');
INSERT INTO validator_tickets (agent_id, bench_version, validator_hotkey, slot_id, status, deadline, attempt_count)
VALUES ('00000000-0000-0000-0000-0000000000a1', 12, :'hotkey', 'slot-0', 'issued', now() + interval '1 hour', 1);
INSERT INTO inference_grants (grant_id, agent_id, bench_version, validator_hotkey, slot_id, ticket_deadline, status,
  broker_public_key, generation, allowed_models, request_budget, token_budget, route_provider, route_profile,
  route_quantization, embedding_model, embedding_profile, embedding_provider, embedding_dimensions,
  embedding_request_budget, embedding_token_budget, usage_accounting_version, expires_at)
VALUES ('00000000-0000-0000-0000-0000000000b2','00000000-0000-0000-0000-0000000000a1',12,:'hotkey','slot-0',
  now()+interval '1 hour','pending',NULL,0,'["openai/gpt-oss-20b"]',8192,25000000,'openrouter',
  'openrouter-route-6a097486af3c178d-v1',NULL,'perplexity/pplx-embed-v1-0.6b',
  'dittobench-v7-openrouter-pplx-embed-v1-0.6b-768-v1','Perplexity',768,100000,1000000000,2, now()+interval '1 hour');
COMMIT;
SQL
PGPASSWORD=ditto psql -h localhost -p 5442 -U ditto -d ditto -q -f "$P1/seed.sql" >/dev/null

# 6. broker container (fresh => no stale loopback-bound sessions) + harness.
log "container ds-broker (broker :8010 + $HARNESS harness :9000)"
cat > "$P1/entrypoint.sh" <<'EOF'
#!/bin/sh
set -e
mkdir -p /private && chmod 700 /private
"${HARNESS_BIN:-/bin/refharness.linux}" -port 9000 >/tmp/harness.log 2>&1 &
exec /bin/dittobench-api.linux -port 8010
EOF
chmod +x "$P1/entrypoint.sh"
docker rm -f ds-broker >/dev/null 2>&1 || true
docker run -d --name ds-broker --add-host=host.docker.internal:host-gateway -p 8010:8010 \
  -v "$P1/bin/dittobench-api.linux:/bin/dittobench-api.linux:ro" \
  -v "$P1/bin/refharness.linux:/bin/refharness.linux:ro" \
  -v "$P1/bin/modelharness.linux:/bin/modelharness.linux:ro" \
  -v "$P1/entrypoint.sh:/entrypoint.sh:ro" -v "$P1/cert.pem:/ca.pem:ro" \
  -e SSL_CERT_FILE=/ca.pem -e DITTOBENCH_ALLOW_PRIVATE_HARNESS=1 \
  -e DITTOBENCH_BROKER_CONTROL_TOKEN=localdev -e DITTOBENCH_PRIVATE_ARTIFACT_DIR=/private \
  -e DITTOBENCH_PLATFORM_INFERENCE_PROXY_URL=https://host.docker.internal:8443/api/v1/inference/chat/completions \
  -e HARNESS_BIN="$HARNESS_BIN" -e OPENROUTER_BASE_URL=http://127.0.0.1:11436/v1/inference \
  -e DITTOBENCH_MODEL=openai/gpt-oss-20b ${CONSTANT_ANSWER:+-e CONSTANT_ANSWER="$CONSTANT_ANSWER"} \
  alpine:latest /entrypoint.sh >/dev/null 2>&1
for _ in $(seq 1 50); do curl -sf http://localhost:8010/health >/dev/null 2>&1 && break; sleep 0.3; done
log "UP. run:  ./localstack/phase1/handshake.sh   (fresh broker => run ONE handshake per 'up')"
