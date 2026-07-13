# Hosting the model gateway (scoring validators)

For operators running the scoring half of a validator
(`VALIDATOR_ENABLE_SCORING=true`). Weights-only validators host no model and can
skip this.

A scoring validator serves exactly one LLM: the locked harness model that every
miner's agent is scored against. v2 locks it to one open-weight model so scores
are comparable across validators and model choice is not an attack surface.
Grading is judge-free and deterministic (dittobench-api
`docs/judge-determinism.md`), so there is no judge model and no validator-side
LLM key; grading adds zero noise to the k=3 median.

The locked model is **Qwen3-32B**. The harness reaches it from inside the eval
sandbox over an OpenAI-compatible gateway you host. The fleet standard is
**model-relay fronting Chutes** (`Qwen/Qwen3-32B-TEE`, FP8, hardware-attested):
zero GPUs and one pinned serving stack for every validator by construction.
Options A/B are self-hosted GPU fallbacks; they do not bit-match FP8, so do not
mix them with relay-backed validators in the same k=3 set.

## Hardware requirements

One host runs everything: the validator worker, its co-located dittobench-api
scorer (which `docker build`s and runs each miner crate in a sandbox), and the
gateway. The fleet-standard floor (Option C, no GPU):

| Resource | Floor |
|---|---|
| CPU | 4 vCPU |
| RAM | 16 GB |
| Disk | 80 GB+ free |

Disk is dominated by the Docker build cache and per-submission images, not the
model. The chat model is served remotely in Chutes' TEE, so no GPU is needed;
the small embedding model runs locally on CPU.

Self-hosted gateways (Options A/B) add a **24 GB GPU** (3090/4090/L4) on the same
host for the 4-bit weights (~20 GB). vLLM at bf16 instead needs ~65 GB VRAM
(1x80 GB or 2x48 GB). The locked model is a consensus parameter, so you cannot
drop to a smaller model to fit a smaller card.

Cases run sequentially, so single-request latency matters more than batch
throughput. Keep the gateway on the sandbox host so gateway latency is
negligible.

## Pin the exact artifact (quantization is part of the consensus)

Two validators serving "the same model" at different quantizations produce
different logits and disagree at argmax ties, so the consensus artifact is the
exact weights file, not just the model id:

- Option A (Ollama): pull the explicit quantization tag and record the digest
  (`ollama show`); digests must match across the fleet.
- Option B (vLLM): pin the Hugging Face revision (commit hash) and agree on one
  quantization fleet-wide.
- Option C (relay): Chutes serves one pinned artifact (`Qwen/Qwen3-32B-FP8`) in
  attested TEEs, so every relay-backed validator gets the same stack by
  construction. A/B do not bit-match it and run practice or fallback only.

Treat a quantization change like a model change: a coordinated bump plus a
bench-version re-score when it moves scores.

## Topology

```
                          ┌────────────────────────── validator host ──┐
  eval sandbox (harness) ─┤ host.docker.internal:11434 ──► gateway      │
                          │   (Ollama / vLLM / model-relay ──► Chutes)  │
                          └─────────────────────────────────────────────┘
```

The sandbox reaches the gateway at `host.docker.internal:11434` (a `NO_PROXY`
bypass allows this; the egress firewall drops everything else).

## Option A: Ollama (simplest self-hosted)

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama serve                       # or a systemd service
ollama pull qwen3:32b-q4_K_M       # explicit quantization tag
ollama show qwen3:32b-q4_K_M       # record the digest; must match fleet-wide
```

Pin `num_gpu`, `num_thread`, and `num_ctx` per host: thread count and the
GPU-layer split change the reduction order. This only affects the harness's own
sampling now; grading is deterministic regardless.

## Option B: vLLM (higher throughput)

```bash
pip install vllm
vllm serve Qwen/Qwen3-32B --revision <pinned-commit> --port 11434 --enforce-eager
```

## Option C: model-relay + Chutes (no GPU, fleet standard)

Run dittobench-api's `cmd/model-relay` as the gateway. It terminates the
sandbox's requests locally, forces the model id to the locked one, injects your
Chutes key, and forwards to Chutes' TEE catalog. The sandbox never holds the key
and cannot pick the model, so the lock behaves exactly like a local gateway. The
relay serves chat; a local Ollama serves embeddings (see the wiring below).

```bash
RELAY_API_KEY=cpk-... \
RELAY_MODEL=Qwen/Qwen3-32B-TEE \
PORT=11435 \
./model-relay
```

Chutes serves the model in Intel TDX trust domains with per-token verification
bound to the exact HF revision, at roughly $0.10/M input and $0.42/M output
tokens; a full scoring run is 10^5 to 10^6 tokens, well under a dollar.
Tradeoffs: a third-party dependency in the scoring loop (the relay keeps the
backend swappable to Option A with no other change) and FP8 serving noise in the
harness's outputs, absorbed by k=3.

## Wiring the validator

Set on the dittobench-api scorer service (see dittobench-api `docs/model-lock.md`):

```
DITTOBENCH_MODEL_LOCK=1

# A/B (local gateway):
HARNESS_MODEL=qwen3:32b-q4_K_M                 # the id as the gateway knows it
HARNESS_PROVIDER=ollama
HARNESS_GATEWAY_URL=http://host.docker.internal:11434

# C (relay + Chutes): chat to the relay (11435), embeddings on local Ollama (11434)
# HARNESS_MODEL=Qwen/Qwen3-32B-TEE
# HARNESS_PROVIDER=chutes
# HARNESS_GATEWAY_URL=http://host.docker.internal:11435
# HARNESS_EMBED_URL=http://host.docker.internal:11434
```

`HARNESS_MODEL` must name the model as the gateway knows it; the canonical id
`qwen/qwen3-32b` names the same locked model in docs and score reports.

Drop `openrouter.ai` from `EGRESS_PROXY_ALLOW` so the gateway is the only
reachable LLM (the harness fails closed otherwise). See the Sandbox egress
section in dittobench-api `docs/model-lock.md`. On Option C only the relay may
reach Chutes; the sandbox still reaches only the relay.

## Verifying reproducibility

Grading is deterministic, so this is a one-command check: score the same fixed
transcript twice (or on two validators) and diff the per-case scores; they must
be byte-identical. Residual cross-validator composite spread comes only from the
harness's own execution against the gateway, which k=3 absorbs.

## References (dittobench-api)

- `docs/model-lock.md`: the harness lock design, gateway backends, config, and
  the sandbox egress firewall.
- `docs/judge-determinism.md`: the judge-free grading rules.
