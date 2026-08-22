#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repository_root=$(cd -- "$script_dir/.." && pwd)
starter_root="$repository_root/miners/dittobench-coding-starter-kit"
datagen_root="$repository_root/research/dittobench-coding-datagen"
practice_port="${DITTO_CODING_PRACTICE_PORT:-18080}"
practice_log=$(mktemp /tmp/dittobench-coding-practice.XXXXXX.log)
practice_pid=""

cleanup() {
  if [[ -n "$practice_pid" ]]; then
    kill "$practice_pid" 2>/dev/null || true
    wait "$practice_pid" 2>/dev/null || true
  fi
  find "$practice_log" -delete 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ ! "$practice_port" =~ ^[0-9]+$ ]] \
  || (( practice_port < 1024 || practice_port > 65535 )); then
  echo "DITTO_CODING_PRACTICE_PORT must be between 1024 and 65535" >&2
  exit 2
fi

(
  cd "$datagen_root"
  uv sync --locked --group dev --python 3.12
)

(
  cd "$starter_root"
  cargo build --locked --bin dittobench-coding-miner
)

(
  cd "$starter_root"
  exec target/debug/dittobench-coding-miner \
    --port "$practice_port" \
    --model-mode scripted \
    --allow-practice-model \
    --script fixtures/mock/ledger-001.json
) >"$practice_log" 2>&1 &
practice_pid=$!

ready=false
for _ in $(seq 1 30); do
  if curl --fail --silent \
    "http://127.0.0.1:${practice_port}/coding/health" >/dev/null; then
    ready=true
    break
  fi
  if ! kill -0 "$practice_pid" 2>/dev/null; then
    break
  fi
  sleep 1
done

if [[ "$ready" != true ]]; then
  echo "scripted coding harness did not become ready" >&2
  tail -n 100 "$practice_log" >&2 || true
  exit 1
fi

(
  cd "$datagen_root"
  uv run dittobench-coding-datagen evaluate-practice \
    --pack practice/v1 \
    --task PRACTICE-LEDGER-001 \
    --harness-url "http://127.0.0.1:${practice_port}"
)
