# DittoBench v5 relay-token waste penalty

> **Historical contract only.** Benchmark v5/v6 execution is retired. Do not
> run or refresh its starter-kit calibration campaign. The frozen manifest and
> evidence remain solely to reproduce historical scores; v7+ uses trusted usage
> with a platform-owned relative efficiency bonus.

DittoBench v5 measures model tokens at the validator-owned relay. A miner's
`prompt_tokens` and `output_tokens` fields are never trusted or scored. Every
frozen-model call crosses the relay outside miner control, where the upstream
provider's prompt/completion/total usage and round trips are validated and
accumulated for the isolated run. Post-pinning prompt bytes and provider
latency remain reported audit telemetry.

Do not move this counter into the starter kit. Miner code is editable, so an
in-process or "sealed" counter can be bypassed or fed fabricated numbers. The
relay is the authoritative boundary already enforcing the model lock.

The v2-v4 score contracts are unchanged. V5 is not advertised by
`/v1/capabilities` until the embedded manifest contains reviewed budgets and an
explicit phase-B scoring approval.

## Waste-only transform

The budget is the frozen-Qwen starter kit's nearest-rank p90 total provider
tokens for the same run size, provider, relay-profile revision, model, dataset
contract, and starter-kit revision. Prompt and completion tokens both count.

```text
if observed_total_tokens <= p90_budget_total_tokens:
    multiplier = 1.0
else:
    excess_ratio = observed_total_tokens / p90_budget_total_tokens - 1
    multiplier = max(0.90, 1 - 0.10 * excess_ratio / (1 + excess_ratio))

adjusted = round_6(raw_composite * multiplier)
```

This is deliberately not an optimization reward. Any usage at or below the
generous p90 budget is identical. Above-budget waste receives a monotonic,
saturating penalty that approaches a `0.90` floor. Useful multi-query fan-out
and wide candidate pools therefore have budget headroom; whole-table context
dumps and pathological loops are the intended target.

For raw quality `0.9` and a `1,000`-token p90 budget:

| Observed total | Multiplier | Adjusted |
| ---: | ---: | ---: |
| 500 | 1.0 | 0.9 |
| 1,000 | 1.0 | 0.9 |
| 1,250 | 0.98 | 0.882 |
| 2,000 | 0.95 | 0.855 |
| 4,000 | 0.925 | 0.8325 |
| 100,000 | 0.901 | 0.8109 |

The report preserves raw quality, prompt/completion/total tokens, baseline id
and counts, p90 percentile, excess ratio, maximum penalty, multiplier floor,
applied/neutral reason, adjusted score, and raw/adjusted standard error. A score
can never increase through token use. Missing, zero, inconsistent, or
attribution-unsafe telemetry and missing budgets fail neutral at `1.0`.

Latency is not scored: absolute latency mixes provider and hardware capacity
with harness behavior. Provider-boundary and per-case latency stay available
for audit and later within-validator/reference-normalized analysis.

## Historical calibration record

[`internal/efficiency/baselines_v5.json`](../internal/efficiency/baselines_v5.json)
pins:

- bench version, formula version, and v5 dataset known vector;
- exact stock starter-kit commit;
- 20 public seed/dataset hashes for every run size;
- one p90 budget per run size, provider, immutable relay profile, and exact
  upstream model id;
- an explicit `scoring_enabled` phase gate.

Twenty samples made nearest-rank p90 the 18th ordered run without interpolation.
The removed `cmd/tokenbaseline` command produced this frozen record. It is not a
supported operational workflow and must not be used to prepare a new benchmark.

The reviewed phase-B manifest contains 20 audited observations in each group
and `scoring_enabled` is true:

| Provider | Run size | Prompt | Completion | Total p90 budget |
| --- | --- | ---: | ---: | ---: |
| Chutes | small | 102,641 | 1,004 | 103,645 |
| Chutes | medium | 613,406 | 6,483 | 619,889 |
| Chutes | full | 1,476,423 | 15,370 | 1,491,793 |
| OpenRouter | small | 89,133 | 1,309 | 90,442 |
| OpenRouter | medium | 508,706 | 6,447 | 515,153 |
| OpenRouter | full | 1,254,314 | 16,059 | 1,270,373 |

The complete sanitized per-seed observations and distributions are committed
in [`calibration/token-efficiency-v5/summary.json`](../calibration/token-efficiency-v5/summary.json).

## Phase-B activation and screener feedback loop

Efficiency changes miner incentives: once wasting fewer relay tokens protects
score, defeating model-invocation screening becomes more valuable. A cheap shim
could try to mimic output variance or latency while avoiding real model work.
That makes the screener a higher-value attack surface than it is today.

Do not enable the penalty merely because budgets exist. Phase B requires the
parser-breaking accuracy work and hardened screener to be live under this new
adversarial pressure, plus reviewed p90 budgets. Activation is an explicit
manifest change in a separate review.

Observed-trajectory waste signals such as redundant retrieval and pathological
sub-query fan-out are complementary, but they are outside this implementation.
This change is strictly the trusted relay-token counting waste penalty.
