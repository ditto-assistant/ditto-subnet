# Hosting the model gateway (scoring validators)

This is for operators running the scoring half of a validator
(`VALIDATOR_ENABLE_SCORING=true`). Weights-only validators do not score, host no
models, and can ignore this doc.

A scoring validator serves exactly one LLM: the locked harness model, the model
every miner's agent is scored against. v2 locks this to one open-weight model
so scores are comparable across validators and model choice is not an attack
surface. Scoring itself is judge-free and fully deterministic (dittobench-api
`docs/judge-determinism.md`), so there is no judge model, no validator-side
LLM key, and grading contributes zero noise to the k=3 median.

The locked model is **Qwen3-32B** (`HARNESS_MODEL=qwen/qwen3-32b`). The harness
reaches it from inside the eval sandbox through an OpenAI-compatible gateway
you host. Three backends work; pick one:

## Hardware requirements

The locked model is a consensus parameter, so the hardware floor is
non-negotiable on the self-hosted options: you cannot substitute a smaller
model. Option C needs no GPU at all.

| Option | Backend | GPUs |
|---|---|---|
| A | Ollama, Qwen3-32B Q4_K_M (~20 GB weights) | one 24 GB card (3090/4090/L4) |
| B | vLLM, GPTQ/AWQ 4-bit | one 24 GB card; bf16 needs ~65 GB (1x80 GB or 2x48 GB) |
| C | `model-relay` fronting Chutes (`Qwen/Qwen3-32B-TEE`) | none |

Host besides the GPUs (A/B): 32 GB+ system RAM, 60 GB+ free disk, and the same
host as the eval sandbox so gateway latency stays negligible. Cases run
sequentially, so single-request latency matters more than batch throughput.

## Pin the exact artifact (quantization is part of the consensus)

Two validators serving "the same model" at different quantizations produce
different logits and disagree at argmax ties. The consensus artifact is the
exact weights file, not just the model id:

- Option A: pull the explicit quantization tag and record the digest
  (`ollama show`); digests must match across the fleet.
- Option B: pin the Hugging Face revision (commit hash) and agree on one
  quantization fleet-wide.
- Option C: Chutes serves one pinned artifact (`root: Qwen/Qwen3-32B-FP8`) in
  attested TEEs, so every relay-backed validator gets the same serving stack by
  construction. Note FP8 on Chutes will not bit-match a local Q4_K_M validator;
  the fleet standardizes on ONE option for scored runs.
- Treat a quantization change like a model change: coordinated bump plus a
  bench-version re-score when it affects scores.

## Topology

```
                          ┌─────────────────────────── validator host ──┐
  eval sandbox (harness) ─┤ host.docker.internal:11434 ──► gateway       │
                          │   (Ollama / vLLM / model-relay ──► Chutes)   │
                          └──────────────────────────────────────────────┘
```

The sandbox reaches the gateway at `host.docker.internal:11434` (a `NO_PROXY`
bypass already allows this, and the egress firewall drops everything else).

## Option A: Ollama (simplest self-hosted)

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama serve            # or run as a systemd service

# pull the locked model with the explicit quantization tag, record the digest
ollama pull qwen3:32b-q4_K_M
ollama show qwen3:32b-q4_K_M
```

Pin `num_gpu`, `num_thread`, and `num_ctx` per host: thread count and GPU-layer
split change the reduction order. This only affects the harness's own sampling
stability now; grading is deterministic regardless.

## Option B: vLLM (higher throughput)

```bash
pip install vllm
vllm serve Qwen/Qwen3-32B \
  --revision <pinned-commit> \
  --port 11434 \
  --enforce-eager
```

## Option C: model-relay + Chutes (no GPU)

Run dittobench-api's `cmd/model-relay` as the gateway. It terminates the
sandbox's requests locally, forces the model field to the locked id, injects
your Chutes key, and forwards to Chutes' TEE-served catalog. The sandbox never
holds the key and cannot choose the model, so the lock's semantics match a
local gateway.

```bash
RELAY_API_KEY=cpk-... \
RELAY_MODEL=Qwen/Qwen3-32B-TEE \
PORT=11434 \
./model-relay
```

Chutes serves the model inside Intel TDX trust domains with per-token model
verification bound to the exact HF revision, independently attestable, at
roughly $0.10/M input and $0.42/M output tokens. A full scoring run is on the
order of 10^5 to 10^6 tokens, well under a dollar. The tradeoffs: a third-party
dependency in the scoring loop (hedge: the relay makes the backend swappable
back to Option A with no other change), and FP8 serving noise in the harness's
own outputs, absorbed by k=3 like any serving noise.

## Wiring the validator

Set on the dittobench-api scorer service (see dittobench-api
`docs/model-lock.md`):

```
DITTOBENCH_MODEL_LOCK=1
HARNESS_MODEL=qwen3:32b-q4_K_M                   # A: the Ollama tag
# HARNESS_MODEL=Qwen/Qwen3-32B-TEE               # C: the Chutes id (relay pins it anyway)
HARNESS_PROVIDER=ollama
HARNESS_GATEWAY_URL=http://host.docker.internal:11434
```

`HARNESS_MODEL` must name the model as the gateway knows it; the canonical id
`qwen/qwen3-32b` names the same locked model in docs and score reports.

Also drop `openrouter.ai` from `EGRESS_PROXY_ALLOW` so the gateway is the only
reachable LLM (the harness fails closed if it tries to route elsewhere). See
dittobench-api `docs/sandbox-egress-hardening.md`. On Option C, only the relay
process may reach the Chutes upstream; the sandbox still reaches only the
relay.

## Verifying reproducibility

Grading is deterministic, so this is now a one-command check: score the same
fixed submission transcript twice (or on two validators) and diff the per-case
scores; they must be byte-identical. Residual cross-validator composite spread
comes only from the harness's own execution against the gateway, which k=3
absorbs.

## References (dittobench-api, private)

- `docs/model-lock.md`: the harness lock design, gateway backends, and config.
- `docs/judge-determinism.md`: the judge-free grading rules.
- `docs/sandbox-egress-hardening.md`: the egress firewall the lock relies on.
