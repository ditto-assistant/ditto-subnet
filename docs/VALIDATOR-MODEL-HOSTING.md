# Hosting the model gateway (scoring validators)

This is for operators running the scoring half of a validator
(`VALIDATOR_ENABLE_SCORING=true`). Weights-only validators do not score, host no
models, and can ignore this doc.

A scoring validator runs two LLM consumers, both of which should point at a local
OpenAI-compatible gateway you host (Ollama or vLLM) rather than a hosted API:

1. The harness model, the model the miner's agent is scored against. v2 locks this
   to one open-weight model so scores are comparable across validators and model
   choice is not an attack surface. The harness runs inside the eval sandbox and
   reaches the gateway from there.
2. The judge model, the LLM that grades a slice of cases (tool response quality,
   memory correctness). Self-hosting it is what makes judging reproducible: two
   validators grading the same submission must agree, or the k=3 median is noise.
   See dittobench-api `docs/judge-determinism.md`.

Both can be served by one gateway. Ollama can hold several models at once. vLLM is
one model per instance, so use one instance per model or share a single model for
both roles.

## Why local, not a hosted API

Comparability: the k=3 median only means something if all three validators run the
same model. A hosted model can be silently version-bumped or routed to different
hardware, so two validators disagree. A pinned local model removes that.

Reproducibility: the judge is sent `temperature 0 + top_p 1 + a fixed seed`. A
hosted, multi-tenant, batched model does not reliably honor a seed, because batch
composition and kernel routing flip the argmax at token boundaries. A local stack
you pin does.

No key in the sandbox: under the lock no OpenRouter key is forwarded into the
sandbox at all, which closes the key-exfiltration and BYOK-spend concerns.

## Hardware requirements

The locked model is a consensus parameter, so the hardware floor is
non-negotiable: you cannot substitute a smaller model to fit a smaller GPU.
Sizing below is for Qwen2.5-72B-Instruct, the v2 locked model, serving both
the harness and the judge from one gateway.

| Setup | GPUs | Fits |
|---|---|---|
| Ollama, Q4_K_M quant (the consensus default) | 1x 80 GB (H100/A100) | comfortable, full GPU offload plus KV cache |
| Ollama, Q4_K_M quant | 2x 48 GB (L40S / RTX 6000 Ada / A6000) | works, layers split across cards |
| Ollama, Q4_K_M quant | 3x 24 GB (4090-class) | works but slower; more splits, more PCIe traffic |
| vLLM, GPTQ/AWQ 4-bit | 1x 80 GB or 2x 48 GB | works with `--enforce-eager` |
| vLLM, bf16 | 2x 80 GB minimum (`--tensor-parallel-size 2`) | full-precision option |

Rules of thumb behind the table: the Q4_K_M GGUF artifact is about 47 GB of
weights, bf16 is about 145 GB, and you need headroom on top for the KV cache
(a few GB at the 8k-16k context this workload uses) and CUDA overhead. Do not
plan on partial CPU offload: it works, but 72B token rates drop to single
digits and a full scoring run stops finishing inside the ticket deadline.

Host besides the GPUs: 64 GB+ system RAM, 100 GB+ free disk for model
artifacts, and the same host (or same LAN) as the eval sandbox so gateway
latency stays negligible. Throughput-wise a full profile run is on the order
of 10^5 to 10^6 tokens through the gateway (harness turns plus the judged
slice), which at 72B speeds means a few hours of GPU time per submission.
Cases run sequentially, so single-request latency, not batch throughput, is
what matters; this is why one pinned host at low concurrency is both the
cheapest and the most reproducible option.

## Pin the exact artifact (quantization is part of the consensus)

Two validators serving "the same model" at different quantizations produce
different logits and will disagree at argmax ties, which is exactly the k=3
noise this setup exists to remove. The consensus artifact is therefore not
just the model id but the exact weights file:

- The fleet default is the Ollama Q4_K_M artifact, pulled with the explicit
  tag `qwen2.5:72b-instruct-q4_K_M` (the short `qwen2.5:72b-instruct` tag
  resolves to it today, but explicit is what pins it). After pulling, record
  the digest (`ollama show`) and compare it with the other validators; digests
  must match.
- On vLLM, pin the Hugging Face revision (commit hash) in the serve command
  and agree on one quantization across the fleet. A bf16 vLLM validator will
  not reproduce an Ollama Q4_K_M validator even with every other knob pinned.
- Treat a quantization change like a model change: it follows the same
  coordinated bump as `HARNESS_MODEL` (and a bench-version re-score when it
  affects scores).

## Topology

```
                          ┌─────────────────────────── validator host ──┐
  eval sandbox (harness) ─┤ host.docker.internal:11434  ──► gateway      │
  dittobench-api (judge) ─┤ localhost:11434 (same host)  ──► (Ollama /   │
                          │                                    vLLM)     │
                          └──────────────────────────────────────────────┘
```

The sandbox reaches the gateway at `host.docker.internal:11434` (a `NO_PROXY`
bypass already allows this, and the egress firewall drops everything else).

The dittobench-api process (the judge) reaches the same gateway at whatever address
is local to where that process runs: `localhost:11434` if it is on the host,
`host.docker.internal:11434` if it is itself containerized. Adjust the URL to your
deployment.

## Option A: Ollama (simplest, recommended for a single pinned host)

Ollama is the easiest to make bit-reproducible on one host at low concurrency,
which suits the judge.

```bash
# install + serve (listens on :11434)
curl -fsSL https://ollama.com/install.sh | sh
ollama serve            # or run as a systemd service

# pull the locked model (one artifact serves both harness and judge).
# Use the explicit quantization tag so the artifact is pinned, not whatever
# the short tag currently resolves to:
ollama pull qwen2.5:72b-instruct-q4_K_M
ollama show qwen2.5:72b-instruct-q4_K_M    # record the digest; must match the fleet
```

Determinism knobs to pin, in the modelfile or per-request `options`:

- `seed` and `temperature 0`. The API sends these, so keep the server defaults from
  overriding them.
- `num_gpu`, `num_thread`, `num_ctx`. Pin these. Thread count and GPU-layer split
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
# judge) so batch composition stays stable. Continuous batching under variable
# load otherwise perturbs the numerics and flips the argmax at ties.
```

vLLM honors a top-level `seed` and serves an OpenAI-compatible API. Reproducibility
requires eager mode, fixed parallelism, and stable batching, as above.

## Wiring the validator

### Harness model lock (the model the miner is scored against)

Set on the dittobench-api scorer service (see dittobench-api `docs/model-lock.md`):

```
DITTOBENCH_MODEL_LOCK=1
HARNESS_MODEL=qwen2.5:72b-instruct-q4_K_M      # must name what the gateway serves (bump for v3)
HARNESS_PROVIDER=ollama                          # provider string the crate uses for a local gateway
HARNESS_GATEWAY_URL=http://host.docker.internal:11434
```

`HARNESS_MODEL`, like `SCORER_MODEL`, must name the model as the gateway knows
it: the Ollama tag for an Ollama gateway, the served model name for vLLM. The
canonical id `qwen/qwen2.5-72b-instruct` names the same locked model in docs
and score reports.

Also drop `openrouter.ai` from `EGRESS_PROXY_ALLOW` so the gateway is the only
reachable LLM (the harness fails closed if it tries to route elsewhere). See
dittobench-api `docs/sandbox-egress-hardening.md`.

### Judge model (reproducible grading)

Also on the dittobench-api scorer service (see dittobench-api
`docs/judge-determinism.md`):

```
LLM_BASE_URL=http://localhost:11434/v1/chat/completions   # adjust host per topology above
SCORER_MODEL=qwen2.5:72b-instruct-q4_K_M                   # must name what the gateway serves
OPENROUTER_API_KEY=local                                    # any non-empty token; local gateways ignore it
```

The judge sends the same OpenAI Chat Completions shape to any base URL, so Ollama
and vLLM both work without a code change. `temperature 0 + top_p 1 + seed` are sent
automatically.

Notes on the judge defaults:

- `SCORER_MODEL` defaults to the locked harness model, so with the lock on the
  whole scoring stack is one frozen open-weight model. You still set it
  explicitly here because the gateway's model name (the Ollama tag above)
  differs from the canonical id (`qwen/qwen2.5-72b-instruct`).
- With `LLM_BASE_URL` set, judge calls automatically add
  `response_format: {"type":"json_object"}`, constraining the verdict to a JSON
  object. Ollama and vLLM's OpenAI-compatible endpoints both support this. If
  your gateway rejects the field, set `LLM_RESPONSE_FORMAT=off`.
- `SCORER_MODEL_B` (optional) turns on the audit slice: about 1 in 5 judged
  cases is also graded by the second model, disagreements are counted, and the
  run logs `judge audit slice: X/Y disagreement(s)`. The counts also ride the
  score report as `details.judge_audited` / `details.judge_disagreed`.

## Verifying reproducibility

Grade the same fixed submission twice and diff the per-case scores. They should be
identical. If they are not, the serving stack is not honoring the seed or the
batching is not pinned, so recheck the Option A and B knobs above.

For a continuous check, set `SCORER_MODEL_B`: every run then reports its
audit-slice disagreement count in the logs and in the score report
(`details.judge_audited` / `details.judge_disagreed`). On a correctly pinned
self-hosted stack the disagreement rate reflects genuine rubric ambiguity, not
serving noise, and should sit near zero; a persistently high rate on one
validator relative to the fleet means that host's gateway is not pinned.

## References (dittobench-api, private)

- `docs/model-lock.md`: the harness lock design and config surface.
- `docs/judge-determinism.md`: what the judge sends, why temperature 0 is not
  enough, and the vLLM and Ollama determinism recipe.
- `docs/sandbox-egress-hardening.md`: the egress firewall the lock relies on.
