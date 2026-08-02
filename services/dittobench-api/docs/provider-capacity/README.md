# Shared-model capacity study

Snapshot date: 2026-07-17 through 2026-07-18 UTC. This study is stacked on
the generalized provider tools in PR #29. It made no production, chain,
assignment, deployment, release, rescreen, or merge change.

## Decision

Use `Qwen/Qwen3.6-27B-TEE` as the primary high-inference candidate. For a
fixed three-model generalist score, start a shadow calibration with:

1. `Qwen/Qwen3.6-27B-TEE` -- capacity and throughput leader;
2. `zai-org/GLM-5.1-TEE` -- architecture and fleet-diversity component;
3. `Qwen/Qwen3-32B-TEE` -- continuity anchor.

Do not include `google/gemma-4-31B-turbo-TEE` while its Chutes fleet remains
saturated. Its nominal fleet is large, but five snapshots showed only 2.8%
conservative headroom and a 69.4% one-hour completion ratio. The like-for-like
load ramp then completed only 11/32 requests, with 17 rate limits and four
timeouts.

GLM-5.1 is not a throughput leader. It remains in the proposed mix because it
had 21 active instances, five-instance scale allowance, roughly 61% headroom,
near-perfect public completion, and substantially different MoE architecture.
Its inclusion must be gated by a shadow score floor and an end-to-end runtime
budget. Further GLM full-score work was intentionally stopped at the user's
request.

Machine-readable aggregates are in
[`results/2026-07-18-shared-model-study.json`](results/2026-07-18-shared-model-study.json).

## Exact overlap

The Chutes catalog was captured at `2026-07-17T21:47:08Z`; OpenRouter models
at `2026-07-17T21:47:09Z`. Slugs and serving derivatives were compared through
their catalog roots and Hugging Face identifiers, not by display name alone.

| Family | Chutes id -> root | OpenRouter slug | Weight relationship |
| --- | --- | --- | --- |
| Qwen3.6-27B | `Qwen/Qwen3.6-27B-TEE` -> `Qwen/Qwen3.6-27B-FP8` | `qwen/qwen3.6-27b` (`qwen/qwen3.6-27b-20260422`) | Same family/version; FP8 serving derivative |
| Gemma-4-31B-IT | `google/gemma-4-31B-turbo-TEE` -> `nvidia/Gemma-4-31B-IT-NVFP4` | `google/gemma-4-31b-it` (`google/gemma-4-31b-it-20260402`) | Same family/size/version, but NVIDIA NVFP4 derivative rather than the same artifact |
| GLM-5.1 | `zai-org/GLM-5.1-TEE` -> `zai-org/GLM-5.1-FP8` | `z-ai/glm-5.1` (`z-ai/glm-5.1-20260406`) | Same family/version; FP8 serving derivative |

No smaller Gemma-family model existed in the captured Chutes catalog. OpenRouter
listed other Gemma sizes, but they were not shared Chutes models. No model named
"Gemma 4" was assumed to exist; `Gemma-4-31B-IT` above is the exact cataloged
family.

Captured OpenRouter provider tags:

- Qwen3.6: `morph`, `chutes/fp8`, `siliconflow/fp8`, `io-net/fp8`,
  `phala`, `deepinfra/fp8`, `venice/fp8`, `alibaba`, `wandb/fp8`.
- Gemma: `open-inference/bf16`, `wandb/bf16`, `venice/bf16`,
  `chutes/fp4`, `deepinfra/turbo`, `deepinfra/fp8`, `siliconflow/fp8`,
  `novita/bf16`, `friendli`, `parasail/fp8`, `phala`, `modelrun/fp4`,
  `together` (duplicated endpoint metadata), `sambanova`, and
  `cerebras/fp16`.
- GLM-5.1 had 18 endpoints when refreshed at `2026-07-17T23:08:17Z`.
  Provider-only routing cannot distinguish two endpoint tags owned by one
  provider; those measurements are provider-level, not exact-endpoint claims.

## Architecture and size

Primary model documentation establishes that:

- [Qwen3.6-27B](https://huggingface.co/Qwen/Qwen3.6-27B) is a 27B dense,
  64-layer hybrid model using gated DeltaNet and gated attention. Dense means
  its language parameters are active per token; the card does not publish a
  separate active-parameter count.
- [Qwen3-32B](https://huggingface.co/Qwen/Qwen3-32B) is a standard dense GQA
  model with 32.8B total and 31.2B non-embedding parameters. Qwen3.6 is 5.8B
  total parameters smaller, or about 17.7% relative to 32.8B.
- [NVIDIA Gemma-4-31B-IT-NVFP4](https://huggingface.co/nvidia/Gemma-4-31B-IT-NVFP4)
  is a 30.7B dense hybrid local/global-attention model served in NVFP4 on
  Chutes.
- [GLM-5.1](https://github.com/zai-org/GLM-5) is a 744B-total, 40B-active MoE
  model. FP8 is its serving quantization, not its active-parameter count.

Parameter count alone does not determine throughput. Quantization, accelerator
type, batching, queueing, provider concurrency, and public fleet utilization
all materially affected the measurements below.

## Capacity snapshots

The offline score uses five snapshots from `21:48:21Z` through `22:50:15Z`.
For each snapshot it takes `max(current, 5m, 1h)` utilization, then the median
across snapshots. It also includes active instances, scale allowance,
log-scaled completed requests/hour, and completion ratio.

| Rank | Model | Score | Active | +scale | Headroom | Completed/h | Completion |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | Qwen3.6-27B | 79.61 | 27 | 0 | 77.5% | 13,059 | 99.88% |
| 2 | GLM-5.1 | 76.99 | 21 | 5 | 61.2% | 7,347 | 99.96% |
| 3 | Gemma-4-31B | 59.17 | 25 | 5 | 2.8% | 20,850 | 69.36% |
| 4 | Qwen3-32B | 53.32 | 13 | 1 | 23.9% | 203,726 | 99.58% |

The score selects candidates; it is not an SLA. It deliberately penalizes
Gemma's rate shedding even though Gemma has the largest nominal fleet.

Immediately before the bounded GLM study (`23:08:17Z`), GLM had 21/21 active
instances, +5 scalable allowance, 31.0% current / 34.4% 5m / 32.9% 1h
utilization, and 7,968/7,968 completed requests/hour. Immediately after
(`23:18:18Z`), it still had 21/21 +5, 33.9% / 29.8% / 32.7%, and
8,078/8,078 completed requests/hour. Neither snapshot had rate limits.

## Contract and routing screen

Every OpenRouter request set `HTTP-Referer: https://heyditto.ai`,
`X-OpenRouter-Title: Ditto`, and `X-Title: Ditto`. Every provider was pinned
through `provider.only` and `provider.order` with `allow_fallbacks=false`.

- Qwen3.6: Alibaba, Chutes, DeepInfra, Phala, and WandB each passed 21/21
  repeated scenarios, including 18/18 tool scenarios. SiliconFlow passed its
  initial 7/7 screen. Io Net and Venice passed only 4/7; Morph passed 0/7.
- Gemma: Cerebras, Chutes, Novita, Parasail, and SambaNova stayed clean across
  the selected repetitions. OpenInference changed from 7/7 to 0/14 and then
  returned 404s under load, proving live routing drift. DeepInfra's two endpoint
  rows produced 41/42 total successes but cannot be individually selected with
  provider-only routing.
- GLM-5.1: Baidu, BaseTen, DeepInfra, Friendli, Nebius, Novita, Parasail,
  Phala, StreamLake, and Venice passed the initial 7/7 screen. Chutes passed
  6/7 because one forced-tool response leaked reasoning. DigitalOcean and
  AtlasCloud leaked reasoning; Wafer and Fireworks returned 429s; Z.AI rejected
  forced tool choice.
- Mistral Nemo was eliminated cheaply: it emitted a textual pseudo-call instead
  of a native tool call despite its attractive price.

## Controlled load

The final comparable fixture was a forced native tool call, streamed, with
eight fixed requests at each of c=1,2,4,8, no retries, and a 60-second request
timeout.

### Direct Chutes, c=8

| Model | Completed | Req/s | Output tok/s | p95 | p95 TTFT | Full-ramp failures | Full-ramp cost |
| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| Qwen3.6 | 8/8 | 3.87 | 154.74 | 1.78s | 1.24s | none (32/32) | $0.00571 |
| GLM-5.1 | 8/8 | 0.46 | 12.03 | 8.14s | 5.08s | none (32/32) | $0.00857 estimated |
| Gemma | 3/8 | 0.24 | 6.58 | 4.16s | 2.18s | 17x 429, 4x timeout | $0.00025 completed usage |

Three earlier sustained Qwen3.6 c=8 runs completed 72/72 and ranged from
3.13-4.42 req/s (97.0-137.0 output tok/s), showing meaningful but bounded
throughput variance without errors.

### Strongest clean OpenRouter route, c=8

| Model | Provider | Completed | Req/s | p95 | p95 TTFT | Full-ramp cost |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Qwen3.6 | WandB | 8/8 | 3.93 | 0.98s | 0.60s | $0.00805 |
| GLM-5.1 | BaseTen | 8/8 | 7.03 | 1.13s | 0.85s | $0.00636 |
| Gemma | Cerebras | 8/8 | 1.79 | 4.48s | 3.92s | $0.00434 |

Gemma Cerebras peaked at 8.01 req/s at c=4, then collapsed at c=8. A single
peak therefore overstates sustained capacity.

All ten GLM routes that passed the cheap screen were ramped. BaseTen and Nebius
were clean c=8 leaders (7.03 and 6.85 req/s). Baidu and StreamLake were clean
near 3.4 req/s. Friendli had one timeout; DeepInfra had eight total 429s;
Parasail 21; Venice 24 and no completions above c=1. Across those routes,
266/320 requests completed, with 53 rate limits and one timeout, at $0.06689.

## Full DittoBench repetition

Only Qwen3.6 cleared the capacity-first screen strongly enough for repeated full
benchmark spend. The exact current top eligible artifact was downloaded and
hash-verified through the audited Backroom flow in PR #29, then reused here in
a strict local validator. The untrusted container had no real credential, no
host mount, read-only root, dropped capabilities, no-new-privileges, bounded
memory/CPU/PIDs, and an internal network. A host-only relay held the Chutes key.

Pinned provenance:

- agent `a2b69f5c-e866-45b0-a8cf-849231d9d218` (`uNiCorN-v2` v1);
- artifact SHA-256 `89a8663b7fe9bb31d005412a7f1c682b12ab0740cbdd6b21f963740c25cdd67a`;
- seed `2474496736613908576`;
- dataset SHA-256 `72fef4aad1eee98a176bb31cc0c19ce84453e78a844c6ba906f702b1ffc9b0bf`;
- bench version 2, full 114-case run.

| Run | Composite | Tool | Memory | Median | Wall | Upstream errors | Est. cost |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 0.880185 | 0.89000 | 0.87037 | 4.57s | 10.5m | 0 | $0.1752 |
| 2 | 0.892593 | 0.87778 | 0.90741 | 6.43s | 24.0m | 3 | $0.2135 |
| 3 | 0.860741 | 0.85111 | 0.87037 | 6.06s | 14.7m | 1 | $0.2251 |

Composite mean is 0.877840, sample standard deviation 0.016055, coefficient of
variation 1.83%, and range 0.031852. Wall time CV is much larger at 42.1%.
This model was lower-scoring than the recorded Qwen3-32B baseline, but stable
enough to be useful under the user's capacity-first objective.

Two cells are excluded, not silently retried: an overnight host-relay outage
produced 199 upstream errors and a meaningless 0.120417 composite; a later
Docker-resume address change completed 60 tool cases but failed secondary
memory seeding before a score. Their raw local reports are retained outside the
repository.

## Three-model aggregation

Do not use a raw minimum as the sole score: it makes the noisiest serving model
control the validator. Do not capacity-weight model scores either: live capacity
changes and would make identical harness behavior score differently over time.

Recommended policy:

1. Select the fixed model set using capacity evidence, then freeze it for a
   benchmark version.
2. Run at least three interleaved repetitions per model using the same dataset,
   seed, thinking controls, and model-specific route. Randomize order with a
   prepublished Latin-square schedule.
3. Calibrate a low/high anchor per model from a frozen reference-harness panel.
   Normalize each model as `clamp((score-low)/(high-low), 0, 1)`.
4. Eligibility gate: every model's lower confidence bound must clear its
   predeclared compatibility floor.
5. Primary rank: equal-weight mean of normalized model scores. Report the
   normalized minimum as a generality diagnostic and tie-breaker, not the sole
   composite.
6. Publish per-model distribution, infrastructure failures, retry rules, cost,
   and wall time. Rerun only failures matching predeclared infrastructure
   criteria; never selectively rerun low valid scores.

This preserves equal model influence while using capacity as the model-selection
objective. Qwen3.6 can be adopted as the primary candidate now, but the
three-model composite should remain shadow-only until GLM and Qwen3-32B anchor
panels establish normalization ranges and runtime budgets.

## Cost and uncertainty

This study attributes approximately $0.8841 in observed or catalog-estimated
inference spend, excluding the parent Qwen3-32B study. Direct Chutes streams did
not return cost, so catalog prices were applied to recorded prompt/completion
usage. Remaining uncertainty includes time-of-day demand, account quotas,
hidden provider batching, hardware mix, OpenRouter endpoint-tag ambiguity,
only three valid full Qwen3.6 repetitions on one audited artifact, no full GLM
score distribution, and no production assignment trial.
