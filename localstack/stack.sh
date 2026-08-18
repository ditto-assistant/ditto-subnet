#!/usr/bin/env bash
# stack.sh up|down — start/stop the localstack deps (model relay + dittobench-api).
#
#   up   builds binaries, starts the relay on :$RELAY_PORT and dittobench-api on
#        :$API_PORT (private-harness mode), and waits for both /health endpoints.
#   down kills whatever `up` started (via pidfiles) and leaves logs in .run/.
#
# Env:
#   STUB=1   run the relay in the $0 stub mode (no OpenRouter calls). Used by the
#            free refharness smoke.
#   Any RELAY_STUB=1 has the same effect.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

start_relay() {
  local stub="${STUB:-${RELAY_STUB:-0}}"
  local -a env=(PORT="$RELAY_PORT")
  if [[ "$stub" == "1" ]]; then
    env+=(RELAY_STUB=1)
    log "starting relay in STUB mode (no OpenRouter, \$0)"
  else
    local key; key="$(fetch_openrouter_key)"
    env+=(OPENROUTER_API_KEY="$key")
    [[ -n "${OPENROUTER_BASE_URL:-}" ]] && env+=(OPENROUTER_BASE_URL="$OPENROUTER_BASE_URL")
    log "starting relay -> OpenRouter (key sourced, not printed)"
  fi
  env "${env[@]}" "$BIN_DIR/localstack-relay" >"$RUN_DIR/relay.log" 2>&1 &
  echo $! >"$RUN_DIR/relay.pid"
  wait_health "http://localhost:${RELAY_PORT}/health" "relay"
}

start_api() {
  # v9+ runs (practice or scored) persist the per-run harness projection to a
  # private 0700 dir; the pipeline fails without it.
  local priv="$RUN_DIR/private"
  mkdir -p "$priv"; chmod 700 "$priv"
  DITTOBENCH_ALLOW_PRIVATE_HARNESS=1 \
  DITTOBENCH_PRIVATE_ARTIFACT_DIR="$priv" \
  HARNESS_GATEWAY_URL="$HARNESS_GATEWAY_URL" \
  HARNESS_EMBED_URL="$HARNESS_EMBED_URL" \
    "$BIN_DIR/dittobench-api" -port "$API_PORT" >"$RUN_DIR/api.log" 2>&1 &
  echo $! >"$RUN_DIR/api.pid"
  wait_health "http://localhost:${API_PORT}/health" "dittobench-api"
}

stop_one() {
  local name="$1" pf="$RUN_DIR/$1.pid"
  [[ -f "$pf" ]] || return 0
  local pid; pid="$(cat "$pf")"
  if kill "$pid" 2>/dev/null; then log "stopped $name (pid $pid)"; fi
  rm -f "$pf"
}

case "${1:-up}" in
  up)
    build_binaries
    start_relay
    start_api
    log "stack up: api=http://localhost:${API_PORT}  relay=http://localhost:${RELAY_PORT}"
    ;;
  down)
    stop_one api
    stop_one relay
    stop_one refharness
    log "stack down"
    ;;
  *) die "usage: stack.sh up|down" ;;
esac
