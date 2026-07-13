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

The locked model is **Qwen3-32B**, served through **Chutes** as
`Qwen/Qwen3-32B-TEE` (FP8, hardware-attested). Every validator uses the same
Chutes-served artifact, so no GPU is needed and scores bit-match across the fleet
by construction. You run dittobench-api's `cmd/model-relay` as a thin local
gateway: the eval sandbox reaches the relay, and the relay forwards to Chutes.

## Hardware requirements

One host runs everything: the validator worker, its co-located dittobench-api
scorer (which `docker build`s and runs each miner crate in a sandbox), the relay,
and a small local embeddings model. No GPU.

| Resource | Floor |
|---|---|
| CPU | 4 vCPU |
| RAM | 16 GB |
| Disk | 80 GB+ free |

Disk is dominated by the Docker build cache and per-submission images. Cases run
sequentially, so single-request latency matters more than batch throughput.

## Run the relay (chat)

`cmd/model-relay` terminates the sandbox's requests locally, forces the model id
to the locked one, injects your Chutes key, and forwards to Chutes' TEE catalog.
The sandbox never holds the key and cannot pick the model, so the lock behaves
exactly like a local gateway.

```bash
RELAY_API_KEY=cpk-... \
RELAY_MODEL=Qwen/Qwen3-32B-TEE \
PORT=11435 \
./model-relay
```

Chutes serves the model in Intel TDX trust domains with per-token verification
bound to the exact HF revision, at roughly $0.10/M input and $0.42/M output
tokens; a full scoring run is 10^5 to 10^6 tokens, well under a dollar. It serves
one pinned artifact (`Qwen/Qwen3-32B-FP8`), so quantization is handled for you and
every validator is byte-identical. The tradeoff is a third-party dependency in the
scoring loop, whose FP8 serving noise k=3 absorbs.

Keep the Chutes key in a secret manager and inject it as `RELAY_API_KEY`; it is
never exposed to the sandbox.

## Embeddings

The relay serves chat only. The harness's embeddings run on a small local model
served by Ollama on CPU, reached at `host.docker.internal:11434` while the chat
relay sits on `11435`:

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama serve                       # or a systemd service
```

Pull the embeddings model your dittobench-api run config names (see
`docs/model-lock.md`); it is small and needs no GPU.

## Topology

```
                          ┌───────────────────────── validator host ──┐
                          │  :11435  model-relay ──► Chutes TEE (chat)  │
  eval sandbox (harness) ─┤                                            │
                          │  :11434  Ollama (embeddings, CPU)           │
                          └─────────────────────────────────────────────┘
```

The sandbox reaches both endpoints at `host.docker.internal` through a `NO_PROXY`
bypass; the egress firewall drops everything else, and only the relay process may
reach Chutes upstream.

## Wiring the validator

Set on the dittobench-api scorer service (see dittobench-api `docs/model-lock.md`):

```
DITTOBENCH_MODEL_LOCK=1
HARNESS_MODEL=Qwen/Qwen3-32B-TEE
HARNESS_PROVIDER=chutes
HARNESS_GATEWAY_URL=http://host.docker.internal:11435    # chat: the relay
HARNESS_EMBED_URL=http://host.docker.internal:11434      # embeddings: local Ollama
```

Also drop `openrouter.ai` from `EGRESS_PROXY_ALLOW` so the relay is the only path
to an LLM (the harness fails closed otherwise). See the Sandbox egress section in
dittobench-api `docs/model-lock.md`. The canonical id `qwen/qwen3-32b` names the
same locked model in docs and score reports.

## Verifying reproducibility

Grading is deterministic, so this is a one-command check: score the same fixed
transcript twice (or on two validators) and diff the per-case scores; they must
be byte-identical. Residual cross-validator composite spread comes only from the
harness's own execution against the gateway, which k=3 absorbs.

## Self-hosting the model (non-standard)

Serving the locked model on your own GPU instead of Chutes is supported by the
engine but is not the fleet standard: a self-hosted gateway does not bit-match
Chutes' FP8, so it cannot share a k=3 set with relay-backed validators, and you
take on pinning the exact quantization fleet-wide. Treat it as local practice
only. See dittobench-api `docs/model-lock.md`.

## References (dittobench-api)

- `docs/model-lock.md`: the harness lock design, gateway backends, config, and
  the sandbox egress firewall.
- `docs/judge-determinism.md`: the judge-free grading rules.
