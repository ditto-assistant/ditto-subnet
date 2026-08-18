# Bench localstack

Run a **real, scored DittoBench run end to end on one machine** and print a
composite score, so bench versions (currently **v12**) can be validated against
real agent harnesses. The only external dependency is **OpenRouter** (the locked
model `openai/gpt-oss-20b`). Everything else the production stack needs — chain /
`set_weights`, screening, the Platform lease — is skipped: a `POST /v1/submit`
with `dataset_sha256` set is a self-contained SCORED run (`runScope` in
`services/dittobench-api/cmd/dittobench-api/main.go`).

## TL;DR

```bash
# Free plumbing smoke — scored v12 vs refharness, relay stubbed, $0, no key.
make localstack-smoke

# Prove real inference works (locked model via OpenRouter, ~1 cheap call).
make localstack-relay-check

# Bring the stack up and drive your own harness through it.
make localstack-up                                  # STUB=1 make localstack-up for $0 relay
AGENT_URL=http://localhost:7070 BENCH=12 RUN_SIZE=small SEED=42 make localstack-bench
make localstack-down
```

## What the stack is

| Component | Port | What it is |
|---|---|---|
| `dittobench-api` | 8000 | the validator scorer, `DITTOBENCH_ALLOW_PRIVATE_HARNESS=1` |
| `localstack-relay` | 11434 | dev model-relay: pins the locked model, forwards to OpenRouter, serves `/health` accounting |
| your harness / `refharness` | 9000 | the agent under test |

`cmd/localstack-relay` (added under `services/dittobench-api/cmd/`) is the
sessionless-path gateway the scorer reads at `HARNESS_GATEWAY_URL`. The certified
`compat/model-relay` is **code-frozen to the pre-v7 model** (`qwen/qwen3-32b`) and
its own profile revision, which a bench v7+/v9+ run **rejects**
(`relay_preflight.go:requireTokenAccounting` demands `Model == llm.V7HarnessModel`
and route profile `== llm.V9AggregateProfileRevision`). The localstack relay
reports exactly that identity (both constants imported from `internal/llm`, so
they cannot drift), pins every upstream request to `openai/gpt-oss-20b`, and
supports a `RELAY_STUB=1` `$0` mode for the free smoke.

## How the dataset SHA is computed

A SCORED submit must carry the `dataset_sha256` the scorer will **re-derive and
reject on mismatch** (`main.go:verifyDatasetHash`). The driver computes it with:

```
cd research/dittobench-datagen && go run ./cmd/generate -bench-version 12 -seed 42 -run-size small -sha
```

This is authoritative because the run path (`submitRunSize` → `runSizeJob`) and
`cmd/generate` both call the **same** `gen.ProfileForVersion(run_size, bench)` and
`gen.GenerateDataset`/`BuildArtifactForVersion` — byte-identical artifact, same
`SHA256Hex()`. Verified empirically: the scored smoke's `dataset_sha256`
(`0030f7ac…` for seed 42 / v12 / small) is **accepted** by the scorer.

## How inference routes to OpenRouter

Direct-harness sessionless path (`inference_session_id == ""`):

```
harness --(OpenAI /v1/chat/completions)--> localstack-relay :11434 --(Bearer OPENROUTER_API_KEY)--> OpenRouter
                                                  |
dittobench-api reads /health here at run start & end for token accounting
```

The scorer injects nothing into a direct harness, so **you** point the harness at
the relay: set its `OPENROUTER_BASE_URL=http://localhost:11434/v1` (or
`DITTOBENCH_INFERENCE_BASE_URL`/`OPENAI_BASE_URL` — the relay serves both
`/v1/chat/completions` and `/chat/completions`). `make localstack-relay-check`
proves this hop live: locked model `openai/gpt-oss-20b`, real tokens,
`cost ≈ $3.9e-06`.

The OpenRouter key is **env-first**: a set `OPENROUTER_API_KEY` wins; otherwise it
is pulled from GCP Secret Manager (`gcloud secrets versions access latest
--secret=LOCAL_OPENROUTER_API_KEY --project=ditto-app-dev`). It is only ever
assigned to a variable / passed as child-process env, never logged.

## The v12 gate gap (important — read before trusting a scored composite)

**A pure direct-harness (sessionless) scored v9+ run cannot COMPLETE**, and the
v12 counterfactual + `model_dependence` gate **cannot fire on this path**. Two
independent guards, both requiring a broker inference session:

1. **Per-case model attribution is broker-only.**
   `runCaseWithModelAttribution` (`v9_base.go:23`) short-circuits to plain
   telemetry when `inference_session_id == ""`, so `ModelAttributionComplete` is
   never set. `applyV9BaseEvidence` then fails with *"v9 case attribution
   unavailable: trusted relay windows did not settle"*. (With a *zero-inference*
   harness like refharness you hit the earlier model-use guard instead —
   `model_inference_required` — which the free smoke demonstrates verbatim.)

2. **The v12 counterfactual is gated on a session.**
   `runV12Counterfactual := scope==Scored && bench>=12 && inference_session_id != "" && broker != nil`
   (`main.go:2075`). `beginAblationCase` returns *"inference session unavailable"*
   without one. So `model_dependence` never gets its dependent/independent slice
   evidence and would read `insufficient_evidence` even if scoring were reached.

**What a completing scored v12 run needs (not built here):** a v12-bound,
activated, source-active **broker inference session** — `prepare` → `activate`
(valid UUID grant/agent, valid slot, deadlines, provider/profile/model identity)
→ `claimRun` → `installSourceCapability`/`bindSource`, then per-case snapshots and
the counterfactual's live embeddings. The blocker for doing this locally: the
broker forwards chat to `DITTOBENCH_PLATFORM_INFERENCE_PROXY_URL` — **HTTPS only**,
exact path `/api/v1/inference/chat/completions` — with an **Ed25519-signed grant
proof**, using the broker's **default-trust** HTTP client. A local shim would need
a system-trusted TLS cert (self-signed is rejected) plus a re-implementation of
the Platform grant/proof contract and an embeddings upstream on
`:11434/api/embed`. That is a separate sub-project; the v12 gate itself is already
covered by the modeled unit tests (`v12_dependence_gate_test.go`,
`v12_counterfactual_test.go`).

### So what IS validated here

- **Dataset generation + the `dataset_sha256` pin for v12** (accepted by the scorer).
- **Relay preflight + token accounting** for the v12 identity (`accounting_version 2`,
  locked model, `V9AggregateProfileRevision`).
- **The full generate → seed → run → score pipeline for v12** — see the PRACTICE
  run below, which completes with a real composite, `tool_mean`, `memory_mean`
  (LongMemEval axis) and `token_efficiency`.
- **Real OpenRouter inference** through the relay with the locked model.
- **The scored-v12 fail-closed wiring**, demonstrated empirically by the smoke.

```bash
# Practice v12 (no dataset_sha256): completes, prints a real composite.
SCORED=0 AGENT_URL=refharness BENCH=12 RUN_SIZE=small SEED=42 ./localstack/bench.sh
# => status done, composite 0.021484, tool_mean 0.166667, memory_mean 0.062500,
#    token_efficiency v7-quality-only-v1
```

To get a completing SCORED composite for the efficiency + LongMemEval axes today,
run a bench version below v9 (no v9-base evidence) with a model-using harness, or
implement the broker session above for v9+.

## Adding a real agent harness

The bundled `refharness` makes **no** model calls (it is deterministic), so it can
only exercise the plumbing, not real inference. To validate a real agent:

1. Build the harness (e.g. the Python `lets_5.0` or the Rust `red-dragon` starter
   in `miners/dittobench-starter-kit/`) and start it on some port, e.g. `:7070`.
2. Point its inference at the relay: `OPENROUTER_BASE_URL=http://localhost:11434/v1`
   (the relay pins the model, so the harness's model id is overridden anyway).
3. `make localstack-up` (real relay), then
   `AGENT_URL=http://localhost:7070 BENCH=12 RUN_SIZE=small SEED=42 make localstack-bench`.
4. For **v12 scored** specifically, see "The v12 gate gap" — a broker session is
   required for the run to complete and for `model_dependence` to fire. Practice
   scope (`SCORED=0`) completes today and gives you `tool_mean` + `memory_mean`.

## Files

- `localstack/lib.sh` — config, env-first secret fetch, build, health, SHA helpers.
- `localstack/stack.sh up|down` — start/stop relay + dittobench-api (pidfiles in `.run/`).
- `localstack/bench.sh` — one run: compute SHA, submit, poll, print the report.
- `localstack/smoke.sh` — free scored-v12 plumbing smoke (`STUB=1`, refharness).
- `localstack/relay-check.sh` — bounded real-OpenRouter inference check.
- `services/dittobench-api/cmd/localstack-relay/` — the v12-identity OpenRouter relay.
- Logs and the last run JSON land in `localstack/.run/` (gitignored).
