# Hosting the model gateway (scoring validators)

This is for operators running the **scoring** half of a validator
(`VALIDATOR_ENABLE_SCORING=true`). Weights-only validators do not score, host no
models, and can ignore this doc.

A scoring validator runs two LLM consumers, both of which should point at a local
OpenAI-compatible gateway you host (Ollama or vLLM) rather than a hosted API:

1. **The harness model** — the model the miner's agent is scored against. v2 locks
   this to one open-weight model so scores are comparable across validators and
   model choice is not an attack surface. The harness runs inside the eval sandbox
   and reaches the gateway from there.
2. **The judge model** — the LLM that grades a slice of cases (tool response
   quality, memory correctness). Self-hosting it is what makes judging
   **reproducible**: two validators grading the same submission must agree, or the
   k=3 median is noise. See dittobench-api `docs/judge-determinism.md`.

Both can be served by **one** gateway (Ollama can hold several models at once;
vLLM is one model per instance, so use one instance per model or share a single
model for both roles).

## Why local, not a hosted API

- **Comparability.** The k=3 median only means something if all three validators
  run the same model. A hosted model can be silently version-bumped or routed to
  different hardware, so two validators disagree. A pinned local model removes
  that.
- **Reproducibility.** The judge is sent `temperature 0 + top_p 1 + a fixed seed`.
  A hosted, multi-tenant, batched model does not reliably honor a seed (batch
  composition and kernel routing flip the argmax at token boundaries). A local
  stack you pin does.
- **No key in the sandbox.** Under the lock no OpenRouter key is forwarded into
  the sandbox at all, closing the key-exfiltration and BYOK-spend concerns.

## Topology

```
                          ┌─────────────────────────── validator host ──┐
  eval sandbox (harness) ─┤ host.docker.internal:11434  ──► gateway      │
  dittobench-api (judge) ─┤ localhost:11434 (same host)  ──► (Ollama /   │
                          │                                    vLLM)     │
                          └──────────────────────────────────────────────┘
```

- The **sandbox** reaches the gateway at `host.docker.internal:11434` (a `NO_PROXY`
  bypass already allows this; the egress firewall drops everything else).
- The **dittobench-api process** (the judge) reaches the same gateway at whatever
  address is local to where that process runs — `localhost:11434` if it is on the
  host, `host.docker.internal:11434` if it is itself containerized. Adjust the URL
  to your deployment.

## Option A: Ollama (simplest; recommended for a single pinned host)

Ollama is the easiest to make bit-reproducible on one host at low concurrency,
which suits the judge.

```bash
# install + serve (listens on :11434)
curl -fsSL https://ollama.com/install.sh | sh
ollama serve            # or run as a systemd service

# pull the locked models (harness + judge; can be the same model)
ollama pull qwen2.5:72b-instruct
```

Determinism knobs to pin (in the modelfile or per-request `options`):

- `seed` and `temperature 0` — the API sends these; keep the server defaults from
  overriding them.
- `num_gpu`, `num_thread`, `num_ctx` — pin these. Thread count and GPU-layer split
  change the reduction order and can flip the argmax. Fixing them per host is what
  makes a run bit-stable.

Ollama exposes an OpenAI-compatible endpoint at `/v1/chat/completions`, which is
what the judge client expects.

## Option B: vLLM (higher throughput)

```bash
pip install vllm
vllm serve Qwen/Qwen2.5-72B-Instruct \
  --port 11434 \
  --enforce-eager          # disable CUDA graphs for reproducible numerics
# also: fix --tensor-parallel-size, and cap --max-num-seqs (or serialize the
# judge) so batch composition stays stable — continuous batching under variable
# load otherwise perturbs the numerics and flips the argmax at ties.
```

vLLM honors a top-level `seed` and serves an OpenAI-compatible API. Reproducibility
requires eager mode + fixed parallelism + stable batching, as above.

## Wiring the validator

### Harness model lock (the model the miner is scored against)

Set on the dittobench-api scorer service (see dittobench-api `docs/model-lock.md`):

```
DITTOBENCH_MODEL_LOCK=1
HARNESS_MODEL=qwen/qwen2.5-72b-instruct        # the locked id (bump for v3)
HARNESS_PROVIDER=ollama                          # provider string the crate uses for a local gateway
HARNESS_GATEWAY_URL=http://host.docker.internal:11434
```

And drop `openrouter.ai` from `EGRESS_PROXY_ALLOW` so the gateway is the only
reachable LLM (the harness fails closed if it tries to route elsewhere). See
dittobench-api `docs/sandbox-egress-hardening.md`.

### Judge model (reproducible grading)

Also on the dittobench-api scorer service (see dittobench-api
`docs/judge-determinism.md`):

```
LLM_BASE_URL=http://localhost:11434/v1/chat/completions   # adjust host per topology above
SCORER_MODEL=qwen2.5:72b-instruct                          # must name what the gateway serves
OPENROUTER_API_KEY=local                                    # any non-empty token; local gateways ignore it
```

The judge sends the same OpenAI Chat Completions shape to any base URL, so Ollama
and vLLM both work without a code change. `temperature 0 + top_p 1 + seed` are
sent automatically.

## Verifying reproducibility

Grade the same fixed submission twice and diff the per-case scores; they should be
identical. If they are not, the serving stack is not honoring the seed or the
batching is not pinned — recheck the Option A/B knobs above. The `SCORER_MODEL_B`
audit-slice hook can log verdict agreement across runs for a continuous check.

## References (dittobench-api, private)

- `docs/model-lock.md` — the harness lock design and config surface.
- `docs/judge-determinism.md` — what the judge sends, why temperature 0 is not
  enough, and the vLLM/Ollama determinism recipe.
- `docs/sandbox-egress-hardening.md` — the egress firewall the lock relies on.
