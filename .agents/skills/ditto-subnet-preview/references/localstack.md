# Local DittoBench simulator

Run a real scored (or practice) DittoBench job on one machine and print
composite, `tool_mean`, and `memory_mean`. Command details live in
`localstack/README.md` and `localstack/phase1/README.md`. This page is the
agent workflow: which stack, how to point a miner image at it, and which
numbers are trustworthy.

External dependency is OpenRouter for the locked model `openai/gpt-oss-20b`.
Chain `set_weights`, screening, and the Platform lease are skipped. A
`POST /v1/submit` with `dataset_sha256` is a self-contained SCORED run.

## Pick the stack

| Stack | Command | What completes | Use for |
|---|---|---|---|
| Free smoke | `make localstack-smoke` | Scored-v12 plumbing vs `refharness`, relay stubbed, `$0` | Did the scorer + dataset pin boot? |
| Relay proof | `make localstack-relay-check` | One cheap locked-model call | Is OpenRouter reachable? |
| Sessionless | `make localstack-up` then `make localstack-bench` | Practice v12 (`SCORED=0`): composite, tool, memory. Scored v9+ **fails** without a broker session | Score a host-reachable harness on the v12 *dataset* |
| Phase-1 | `./localstack/phase1/up.sh` then `handshake.sh` | Scored v12 with `model_use` + `model_dependence` firing | Gate calibration on a co-located harness |
| Overlay faults | preview `up` + `FAULT_PROXY_URL` | Same as sessionless, plus 429/drop | Failure-path tests |

`refharness` makes **no** model calls. It is plumbing only.

`uv run ditto practice` is the miner's own kit rehearsal (`$mine`). It is not
this stack and does not score a downloaded third-party image.

## Sessionless (host scorer)

```bash
make localstack-up                                  # STUB=1 for a $0 stub relay
SCORED=0 AGENT_URL=http://127.0.0.1:7070 BENCH=12 RUN_SIZE=small SEED=42 make localstack-bench
make localstack-down
```

| Component | Port | Role |
|---|---|---|
| `dittobench-api` | 8000 | Scorer, `DITTOBENCH_ALLOW_PRIVATE_HARNESS=1` |
| `localstack-relay` | 11434 | Pins `openai/gpt-oss-20b`, OpenRouter upstream, `/health` accounting |
| harness | caller-chosen | `GET /health`, `POST /seed`, `POST /run` |

Sessionless `SCORED=1` v9+ **cannot complete** (no broker session). Use
`SCORED=0` for tool/memory/composite on the v12 dataset, or phase-1
(`make localstack-phase1` then `make localstack-phase1-handshake`) for scored
v12 gates. Answer-stuffing stays on penalize.

`dataset_sha256` for `SCORED=1` is:

```bash
cd research/dittobench-datagen && go run ./cmd/generate -bench-version 12 -seed 42 -run-size small -sha
```

That is the same `gen.ProfileForVersion` + `GenerateDataset` the run path uses.
Mismatch fails closed.

OpenRouter key is env-first (`OPENROUTER_API_KEY`); otherwise
`gcloud secrets versions access latest --secret=LOCAL_OPENROUTER_API_KEY --project=ditto-app-dev`.
Never log it. Never pass it into an untrusted harness; the relay holds it.

### v12 gate gap (sessionless)

A direct-harness (`inference_session_id == ""`) scored v9+ run **cannot
COMPLETE**:

1. Per-case model attribution is broker-only → `ModelAttributionComplete` never
   sets → v9-base evidence fails closed.
2. The v12 counterfactual is gated on a session
   (`scope==Scored && bench>=12 && inference_session_id != "" && broker != nil`).
   `model_dependence` never fires.

**So for sessionless v12:** use `SCORED=0`. The dataset, observed-tool scorer,
and LongMemEval memory axis still run. Report `composite`, `tool_mean`,
`memory_mean`. Do **not** claim `model_dependence` passed. Do **not** compare
those composites to on-chain v11 scores as if gates and `run_size=full` matched.

For scored v12 gates, use phase-1.

## Phase-1 (scored v12, gates firing)

Linux broker container (Go honors `SSL_CERT_FILE` on Linux, not macOS) plus
host Postgres `:5442`, model-relay `:8082`, TLS terminator `:8443`. Harness
must share the container so loopback source-binding sees `127.0.0.1`.

```bash
./localstack/phase1/up.sh                 # HARNESS=model | HARNESS=refharness
./localstack/phase1/handshake.sh          # prepare → exchange(sr25519) → activate → scored submit
./localstack/phase1/down.sh               # FULL=1 also stops postgres
```

`handshake.sh` re-seeds the grant and DELETEs the session, so it is repeatable
per agent without restarting the broker. `HARNESS=model` + `CONSTANT_ANSWER`
is the model-independent fail-closed check.

A downloaded miner image is **not** the bundled `modelharness`. To score one
under phase-1, co-locate that binary in `ds-broker` (same loopback) and point
`AGENT_URL` at `http://127.0.0.1:9000`. Host-published miner ports are not
`127.0.0.1` from inside the broker.

## Score a downloaded miner image (sessionless)

Untrusted: digest-verify, no host Docker socket, no real provider key in the
container.

```bash
python3 .agents/skills/backroom-review/scripts/prepare_artifact.py \
  --url '<signed-url>' --sha256 '<artifact-sha256>' --output-dir /tmp/agent-x

docker build -t sn118-agent-x /tmp/agent-x/source
# Starter-kit miners embed via Ollama embeddinggemma. localstack-relay occupies
# :11434 and does not serve /api/embed — run Ollama on another host port.
OLLAMA_HOST=127.0.0.1:11435 ollama serve
OLLAMA_HOST=127.0.0.1:11435 ollama pull embeddinggemma

docker run -d --name sn118-agent-x -p 7070:8080 \
  --add-host=host.docker.internal:host-gateway \
  -e DITTOBENCH_PROVIDER=platform \
  -e DITTOBENCH_INFERENCE_BASE_URL=http://host.docker.internal:11434/v1 \
  -e DITTOBENCH_MODEL=openai/gpt-oss-20b \
  -e OPENROUTER_BASE_URL=http://host.docker.internal:11434/v1 \
  -e OPENAI_BASE_URL=http://host.docker.internal:11434/v1 \
  -e OLLAMA_BASE_URL=http://host.docker.internal:11435 \
  -e OPENAI_API_KEY=localstack \
  -e OPENROUTER_API_KEY=localstack \
  sn118-agent-x

curl -sf http://127.0.0.1:7070/health
# Advertise the tool observer at a host the *container* can reach. Without this,
# practice caps every observable tool case (capped_tool_cases == n_tool).
DITTOBENCH_TOOL_HOST=host.docker.internal make localstack-up
SCORED=0 AGENT_URL=http://127.0.0.1:7070 BENCH=12 RUN_SIZE=small SEED=42 make localstack-bench
```

Starter-kit images listen on `:8080`. Confirm `EXPOSE` / `CMD` before mapping
ports. Dummy `OPENAI_API_KEY=localstack` satisfies harnesses that require *a*
key; the relay still holds the real OpenRouter secret. If the image ignores
those env vars and dials Chutes/OpenRouter directly, stop and inspect — that
run is not a locked-model localstack score.

Starter-kit miners default `DITTOBENCH_PROVIDER=openrouter` and will 401
against api.openrouter.ai unless you set `DITTOBENCH_PROVIDER=platform`.
Require `capped_tool_cases == 0` and `tool_provenance.endpoint_attempts > 0`
before quoting `tool_mean`; a Docker harness without `DITTOBENCH_TOOL_HOST`
caps every tool case.

One harness per `AGENT_URL` at a time. Tear down the container before the next
image. Reports land in `localstack/.run/report-*.json` (gitignored).

## Run sizes

Same envelopes as `$mine`. On-chain is `full`. `small` is a directional
v12-dataset probe (minutes). `medium` catches seeding/isolation misses.
`full` is hours and is required before claiming a local score will transfer.

| `--run-size` | Envelope |
|---|---|
| `small` | 6 tool + 6 memory, 1 wave, no isolation |
| `medium` | 48 tool + 64 memory, 4 waves, isolation |
| `full` | 100 tool + 225 memory, 5 waves, isolation |

Pin `--seed` only to compare harness revisions. Quote `bench_version`,
`run_size`, `SCORED`, and `seed` with every table.

## Read the report

Require:

- `status` is `done` (not `failed`)
- `bench_version` is the one you asked for
- `tool_mean` and `memory_mean` separately; composite is `0.5/0.5` then gates
- For `SCORED=1` / phase-1: `v9_base.score_gates` (`model_use`,
  `authoritative_tool`, `model_dependence`) and `token_efficiency`

A 1.0 on `small` does not predict `full`. A practice composite is not an
on-chain composite.

## Files

- `localstack/lib.sh`, `stack.sh`, `bench.sh`, `smoke.sh`, `relay-check.sh`
- `localstack/phase1/up.sh`, `handshake.sh`, `down.sh`
- `services/dittobench-api/cmd/localstack-relay/`
- `services/dittobench-api/cmd/localstack-tlsterm`, `cmd/localstack-modelharness`
- Makefile targets: `localstack-up`, `localstack-down`, `localstack-bench`,
  `localstack-phase1`, `localstack-phase1-handshake`, `localstack-smoke`,
  `localstack-relay-check`
