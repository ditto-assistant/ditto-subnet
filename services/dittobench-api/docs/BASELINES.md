# DittoBench baselines

Reference-harness scores under the locked model, for tracking benchmark
difficulty across `bench_version` and as the target a miner submission must beat.
Regenerated over 24 distinct seeds at `run_size=full`; the offline calibrator is
`cmd/benchcal`.

## Run 1: bench_version 2, v0.7.0

- Date: 2026-07-12
- Config: v0.7.0 scorer + datagen (bounded canary, multi-family metamorphic), `bench_version` 2
- Harness: stock reference (dittobench starter kit baseline, unmodified)
- Model: Qwen3-32B. Backend: OpenRouter `qwen/qwen3-32b`. Same weights as the scored
  Chutes `Qwen/Qwen3-32B-TEE`; the TEE is the exact scored backend and may differ slightly.
- Method: 24 distinct seeds (`run_size=full`), so the spread is the real
  run-to-run variance a leaderboard score carries (dataset difficulty plus model noise).
  SE is the standard error of the mean (sd / sqrt(N)). 0 errored runs.

### Headline

| metric | mean | SE | 95% CI | sd | min / max |
| --- | --- | --- | --- | --- | --- |
| composite | 0.492 | 0.013 | [0.467, 0.517] | 0.063 | 0.385 / 0.619 |
| tool_mean | 0.793 | 0.007 | [0.779, 0.806] | 0.034 | 0.726 / 0.862 |
| memory_mean | 0.419 | 0.019 | [0.382, 0.456] | 0.093 | 0.185 / 0.574 |

### Gates and latency

- Canary pass rate: 0.333
- Metamorphic consistency: 0.347 +/- 0.065 SE (graded over the run's invariance families)
- Mean within-run `composite_stderr`: 0.0408
- Median latency: 14.3 s per case (reported, not scored)

### Memory categories (weakest first)

The memory half is where the composite lives and where a submission differentiates.
The weakest categories (injection-resistance, computed-answer, temporal-reasoning,
knowledge-update) are the reference harness's real gaps and the highest-value levers.
The memory/tool split below is approximate (a few memory-tool routing categories
sit either side).

| category | mean | SE | n |
| --- | --- | --- | --- |
| injection-resistance | 0.069 | 0.035 | 24 |
| computed-answer | 0.083 | 0.058 | 24 |
| recipe_create | 0.175 | 0.073 | 24 |
| knowledge-update | 0.181 | 0.053 | 24 |
| preference-application | 0.181 | 0.045 | 24 |
| temporal-reasoning | 0.219 | 0.031 | 24 |
| memory_save_not_search | 0.271 | 0.052 | 24 |
| memory_update | 0.292 | 0.095 | 24 |
| assistant-recall | 0.319 | 0.062 | 24 |
| canary | 0.333 | 0.098 | 24 |
| memory-write-read | 0.333 | 0.060 | 24 |
| multi-session | 0.373 | 0.040 | 24 |
| point-in-time | 0.417 | 0.064 | 24 |
| abstention | 0.433 | 0.049 | 24 |
| single-session-recall | 0.554 | 0.036 | 24 |
| isolation | 0.625 | 0.052 | 24 |
| preference | 0.625 | 0.101 | 24 |
| memory-write | 0.681 | 0.062 | 24 |
| contradiction | 0.722 | 0.052 | 24 |
| aggregation-count | 0.875 | 0.069 | 24 |
| memory_subject | 0.875 | 0.054 | 24 |
| memory_lookup | 0.917 | 0.039 | 24 |
| memory_delete | 1.000 | 0.000 | 24 |
| memory_fetch | 1.000 | 0.000 | 24 |
| recipe_apply | 1.000 | 0.000 | 24 |

### Tool categories (weakest first)

A 0.000 here is a real reference-harness gap, not a broken case: the model never
selects the calendar-create tool though `calendar_create_event` is offered every
run (`calendar_search` scores 0.85, so the tool path works; the model does not
invoke create).

| category | mean | SE | n |
| --- | --- | --- | --- |
| calendar_create | 0.000 | 0.000 | 24 |
| agent_run_not_read | 0.146 | 0.047 | 24 |
| image_edit_not_create | 0.500 | 0.067 | 24 |
| multi_job_status | 0.528 | 0.102 | 24 |
| email_send | 0.533 | 0.086 | 24 |
| job_chain_result_usage | 0.553 | 0.102 | 24 |
| agent_workflow | 0.572 | 0.019 | 24 |
| capability_discovery | 0.583 | 0.103 | 24 |
| set_tool_prefs | 0.633 | 0.023 | 24 |
| multi_web_read | 0.694 | 0.053 | 24 |
| workflow_not_job | 0.717 | 0.029 | 24 |
| agent_job | 0.735 | 0.066 | 24 |
| feedback | 0.800 | 0.042 | 24 |
| multi_web_result_usage | 0.828 | 0.053 | 24 |
| calendar_search | 0.850 | 0.040 | 24 |
| arg_hallucination | 0.875 | 0.069 | 24 |
| automation_not_job | 0.875 | 0.051 | 24 |
| automation_list | 0.903 | 0.058 | 24 |
| set_effort | 0.917 | 0.058 | 24 |
| set_font | 0.917 | 0.034 | 24 |
| route_web_not_memory | 0.917 | 0.026 | 24 |
| agent_read_not_run | 0.938 | 0.035 | 24 |
| parallel_web_image | 0.938 | 0.042 | 24 |
| multi_image_edit | 0.944 | 0.043 | 24 |
| artifacts_create | 0.958 | 0.029 | 24 |
| multi_subject_scope | 0.958 | 0.042 | 24 |
| web_search | 0.971 | 0.014 | 24 |
| image_create | 1.000 | 0.000 | 24 |
| link_read | 1.000 | 0.000 | 24 |
| no_tool | 1.000 | 0.000 | 24 |
| route_memory_not_web | 1.000 | 0.000 | 24 |
| set_accent | 1.000 | 0.000 | 24 |
| set_model | 1.000 | 0.000 | 24 |
| settings | 1.000 | 0.000 | 24 |
| web_recovery_result_usage | 1.000 | 0.000 | 24 |
| web_result_usage | 1.000 | 0.000 | 24 |

## Run 2: bench_version 3, datagen v3 (pre-tag)

- Date: 2026-07-17
- Config: v3 scorer + datagen (anti-gaming hardening: dump guard, needle
  gating + decoys, grammar collision, trajectory bait, canary multi-decoy,
  reachability preflight, transcript publication), `bench_version` 3
- Harness: stock reference (dittobench starter kit baseline, unmodified, with
  the v3 preflight)
- Model: Qwen3-32B. Backend: OpenRouter `qwen/qwen3-32b` (same weights as the
  scored Chutes `Qwen/Qwen3-32B-TEE`)
- Method: ONE full run (seed 20260717, 114 cases, 13 min). A single seed, so
  no between-seed SE here; the within-run `composite_stderr` is reported, and
  the hermetic between-seed dataset spread comes from `cmd/benchcal`
  (30 seeds: tool sd 0.030, composite sd 0.015, noise floor 0). Regenerate
  this section over 24 seeds like Run 1 once the release settles.

### Headline

| metric | value |
| --- | --- |
| composite | 0.461 |
| tool_mean | 0.713 |
| memory_mean | 0.284 |
| within-run composite_stderr | 0.0372 |

Versus Run 1 (v2): composite 0.492 -> 0.461, tool 0.793 -> 0.713, memory
0.419 -> 0.284. The drop is the hardening working: v3 removed the surfaces
the reference harness scored free points on. The transcript digest for this
run verified byte-exact against `GET /v1/runs/{id}/transcript`.

### Gates and latency

- Observed / capped tool cases: 39 / 10 (scored path would zero the capped 10)
- Injection attempts flagged: 5
- Metamorphic consistency: 0.5
- Tool efficiency factor: 1.0
- Median latency: 17.2 s per case (reported, not scored)

### Memory categories (weakest first; n = cases in this single run)

The v3 hard categories (abstention, canary, injection-resistance,
computed-answer, knowledge-update, point-in-time) all sit at 0 for the stock
kit: that is the intended miner headroom, and why the public baseline no
longer saturates the benchmark.

| category | mean | n |
| --- | --- | --- |
| abstention | 0.000 | 5 |
| aggregation-count | 0.000 | 1 |
| assistant-recall | 0.000 | 3 |
| canary | 0.000 | 1 |
| computed-answer | 0.000 | 1 |
| injection-resistance | 0.000 | 5 |
| knowledge-update | 0.000 | 2 |
| point-in-time | 0.000 | 2 |
| preference | 0.000 | 1 |
| preference-application | 0.000 | 3 |
| multi-session | 0.090 | 4 |
| temporal-reasoning | 0.250 | 4 |
| memory-write | 0.333 | 3 |
| memory-write-read | 0.333 | 3 |
| isolation | 0.500 | 4 |
| single-session-recall | 0.700 | 10 |
| contradiction | 1.000 | 3 |

### Tool categories (weakest first; n = cases in this single run)

The result-usage family drops sharply from v2 (e.g. `web_result_usage`
1.000 -> 0.400): v3's needle gating and decoy zeroing require incorporating
the value the bearing tool actually served, which the stock kit only
sometimes does. `calendar_create` remains the known v2 reference gap.

| category | mean | n |
| --- | --- | --- |
| calendar_create | 0.000 | 1 |
| capability_discovery | 0.000 | 1 |
| job_chain_result_usage | 0.000 | 1 |
| memory_update | 0.000 | 1 |
| multi_job_status | 0.000 | 1 |
| multi_subject_scope | 0.000 | 1 |
| recipe_create | 0.000 | 1 |
| multi_web_result_usage | 0.267 | 1 |
| web_recovery_result_usage | 0.400 | 1 |
| web_result_usage | 0.400 | 1 |
| agent_run_not_read | 0.500 | 2 |
| artifacts_create | 0.500 | 2 |
| image_edit_not_create | 0.500 | 2 |
| memory_save_not_search | 0.500 | 1 |
| web_search | 0.500 | 2 |
| agent_workflow | 0.600 | 1 |
| automation_not_job | 0.600 | 1 |
| calendar_search | 0.600 | 1 |
| email_send | 0.600 | 1 |
| set_tool_prefs | 0.600 | 1 |
| workflow_not_job | 0.600 | 2 |
| agent_job | 1.000 | 2 |
| agent_read_not_run | 1.000 | 2 |
| arg_hallucination | 1.000 | 1 |
| automation_list | 1.000 | 1 |
| feedback | 1.000 | 1 |
| image_create | 1.000 | 2 |
| link_read | 1.000 | 2 |
| memory_delete | 1.000 | 1 |
| memory_fetch | 1.000 | 1 |
| memory_lookup | 1.000 | 2 |
| memory_subject | 1.000 | 2 |
| multi_image_edit | 1.000 | 1 |
| multi_web_read | 1.000 | 1 |
| no_tool | 1.000 | 2 |
| parallel_web_image | 1.000 | 1 |
| recipe_apply | 1.000 | 1 |
| route_memory_not_web | 1.000 | 2 |
| route_web_not_memory | 1.000 | 2 |
| set_accent | 1.000 | 1 |
| set_effort | 1.000 | 1 |
| set_font | 1.000 | 1 |
| set_model | 1.000 | 1 |
| settings | 1.000 | 2 |

## Run 3: bench_version 3 + transform audit — AUDIT CALIBRATION

- Date: 2026-07-18
- Config: v3 scorer + datagen at the `harden/anti-gaming` branch head, i.e.
  including the reproduce-under-transform audit (Part A) and the B1-B4
  task-side rollup. `bench_version` 3.
- Harness: stock reference (dittobench starter kit baseline, unmodified)
- Model: Qwen3-32B. Backend: OpenRouter `qwen/qwen3-32b` (same weights as the
  scored Chutes `Qwen/Qwen3-32B-TEE`)
- Method: 25 distinct seeds at `run_size=full`, one FRESH harness process per
  run. The fresh process matters: `/seed` is an idempotent upsert with no
  clear, so a reused harness stacks several personas' haystacks into one store
  and both depresses retrieval and contaminates the pairs being measured.

### Headline

| metric | mean | sd | SE | min / max |
| --- | --- | --- | --- | --- |
| composite | 0.445 | 0.021 | 0.004 | 0.401 / 0.490 |
| tool_mean | 0.790 | 0.035 | 0.007 | 0.716 / 0.871 |
| memory_mean | 0.235 | 0.044 | 0.009 | 0.136 / 0.325 |
| metamorphic_consistency | 0.770 | 0.186 | 0.037 | 0.500 / 1.000 |

Versus Run 2 (single seed, pre-audit): composite 0.461 -> 0.445, memory
0.284 -> 0.235. The B1 candidate salt and the audit cases account for the
memory drop; tool is unchanged, as expected since neither touches the tool
suite.

### Transform-audit calibration (the reason for this run)

`AUDIT_MIN_ROBUSTNESS` was a provisional 0.70 with no measurement behind it.
This run measured what an honest model actually scores, alongside two
purpose-built local solvers driven through the same scored pipeline. The
brittle solver is the attacker the audit exists to catch: it is the SAME
keyword solver as the robust one, gated on recognising the exact question
surface, so answering ability is held constant and surface-keying is the only
variable.

| harness | transform_robustness | conditional agreement | informative pairs |
| --- | --- | --- | --- |
| honest LLM (qwen3-32b) | 0.910 (sd 0.148, min 0.60) | 0.389 | 18 / 95 |
| **brittle (surface-gated solver)** | **0.863** (min 0.50) | **0.000** | 6 / 37 |
| robust (same solver, ungated) | 0.924 | 0.625 | 8 / 37 |
| strong (tf-idf solver) | 0.972 | 0.857 | 7 / 37 |

**The metric does not currently separate a cheater from an honest miner.** The
brittle harness (0.863) sits inside the honest model's own spread (0.910, sd
0.148, min 0.60). At the 0.70 floor, 4 of 25 honest runs (16%) would be
quarantined while only 2 of 12 brittle runs would be caught.

The cause is structural, not a badly chosen threshold:

- **81% of audit pairs came back both-wrong** (77 of 95 on the honest model).
  Agreement counts a both-wrong pair as CONSISTENT, which is deliberate
  (accuracy already penalizes it), but it means that on a benchmark this hard
  the metric is dominated by pairs carrying no brittleness information at all.
  Only 19% of pairs were informative.
- **13 of 25 runs produced fewer than `AUDIT_MIN_PAIRS`=4 pairs**, so over half
  would not be judged even if the floor were sound. Audit pairs averaged 3.8
  per run (min 1, max 7).

A CONDITIONAL statistic — agreement among pairs where at least one half was
correct — does order the harnesses correctly (brittle 0.000, honest 0.389,
robust 0.625, strong 0.857) and is the obvious candidate replacement. It is not
yet actionable either: the honest model sits at 0.389, not far above the
brittle 0.000, and the whole comparison rests on 6-18 informative pairs.

Consequence: the audit ships OBSERVATIONAL. `DITTO_TRANSFORM_AUDIT_ENFORCE`
defaults to false, so the metric is computed, `EVENT_AUDIT` is recorded, and
the value is published, but no agent's status changes. That is what will
accumulate the real-world distribution a future threshold can be set from.

Caveat on population: this is the STOCK reference harness at memory_mean 0.235,
while the audit only ever judges champion/tail agents, which are more accurate
and would yield more informative pairs. Attempts to build a champion-competence
local solver for this run plateaued at memory_mean 0.242, so the informative-pair
rate at champion competence is unmeasured. It is a reasonable expectation that
the metric improves there, and not evidence that it does.

## Run 4: directional audit metric — validation

- Date: 2026-07-18
- Config: as Run 3, plus the directional metric (audit pair COUNTS + exact
  binomial verdict) and the audit rate raised 1500 -> 2500 bps.
- Method: 24 seeds at `run_size=full` on the honest model, and 14 seeds each for
  three non-LLM local solvers through the same scored pipeline. The brittle
  solver is the SAME solver as the robust one, gated on recognising the exact
  question surface, so answering ability is constant and surface-keying is the
  only variable.

### The metric now separates

Verdict is a one-sided exact binomial test on discordant pairs (null p=0.5),
alpha 0.01. Pooled over all runs:

| harness | pairs | n10 base-only | n01 transform-only | p | verdict | memory_mean |
| --- | --- | --- | --- | --- | --- | --- |
| honest LLM | 149 | 11 | 11 | 0.584 | clear | 0.233 |
| **brittle** | 92 | **10** | **0** | **0.00098** | **FLAGGED** | 0.139 |
| robust solver | 92 | 3 | 2 | 0.500 | clear | 0.216 |
| strong solver | 92 | 4 | 2 | 0.344 | clear | 0.241 |

Two things this establishes that Run 3's agreement metric could not:

- **The honest model is exactly symmetric (11 vs 11).** That is the null
  holding, and it also shows the transforms carry no intrinsic difficulty bias:
  if the transformed half were simply harder, an honest harness would show
  n10 > n01 systematically and the test would flag it. It does not.
- **The brittle harness is fully directional (10 vs 0)**, with answering
  ability held identical to the robust solver, which is clear at 3 vs 2.

Audit pairs rose from 3.8 to 6.2-6.6 per run with the rate change.

### Detection needs ACCUMULATION, not one finalization

Simulating verdicts on groups of 3 runs (a single k=3 finalization):

| harness | flagged groups |
| --- | --- |
| honest LLM | 0 / 8 |
| brittle | 0 / 4 |
| robust / strong | 0 / 4 each |

The brittle harness is **not caught at k=3**. Three runs yield only ~2-3
discordant pairs, below the six the test needs to reach alpha at all (with
n01=0 the test needs n10 >= 7, since 6/6 gives p=0.0156). It is caught only
once ~10 discordant pairs have accumulated, which at this harness's rate takes
roughly 10-14 runs.

That is why the verdict is computed from pooled COUNTS over every score an
agent has: a brittle agent is caught as evidence accumulates across its
lifetime, not at first finalization. It also means the audit is a slow
instrument by construction, and cannot be the primary defense against a
short-lived submission.

The rate is competence-dependent and this is the weakest harness case: this
brittle solver answers only 13.9% of memory cases, so it produces ~0.7
base-only pairs per run. A brittle harness competitive enough to be champion
would know the base answer far more often and accumulate the same evidence much
faster. That remains unmeasured (see the champion-population caveat in Run 3).

### False-positive posture

Alpha is 0.01 per verdict by construction, and the honest model's pooled result
sits at p=0.584 with a perfectly even split, which is about as far from the
rejection region as the data can be. No honest group flagged in 8 simulated
verdicts. Enforcement nevertheless stays OFF (`DITTO_TRANSFORM_AUDIT_ENFORCE`)
until the same comparison is run against champion-competence harnesses.

## Run 5: bench_version 4 — REFERENCE BASELINE

- Date: 2026-07-19
- Config: v4 scorer + datagen `v0.9.0`, `bench_version` 4. v4 is the
  false-positive release: same suite as v3, with several ways of being CORRECT
  no longer losing points (canary no longer audit-duplicated, delete
  instructions graded as acknowledgements, memory-routing no longer charged by
  the efficiency term, bounded composite factors floored).
- Harness: stock reference (dittobench starter kit baseline, unmodified, with
  the v3+ reachability preflight). Built in-sandbox from the kit tarball.
- Model: Qwen3-32B through the model-lock relay (`RELAY_PROVIDER=chutes`), the
  same locked weights a scored run uses.
- Scorer: source build, `source_revision`
  `b72a63d6d98678b35add5adf336d745eb4238027`, `supported_bench_versions`
  `[2, 3, 4]`.
- Method: ONE full run, seed `20260719`, 119 cases, ~11 min wall clock. Like
  Run 2 this is a single seed, so there is no between-seed SE here; the
  within-run `composite_stderr` is reported and the hermetic between-seed
  spread comes from `cmd/benchcal`. `dataset_sha256`
  `05fe5b2c070b2bc9da9f50242228dad29dbca8caf6dd867b10dee6679113a6b8`.
- Scope: PRACTICE (`/v1/submit`), same as Run 2. 15 of 60 tool cases were not
  observed through the endpoint and are capped rather than zeroed; the scored
  path would zero them, so a scored composite for this harness would be LOWER.
  Do not compare this number against an on-chain scored run.

### Headline

| metric | value |
| --- | --- |
| composite | 0.429 |
| tool_mean | 0.768 |
| memory_mean | 0.237 |
| within-run composite_stderr | 0.0321 |

Versus Run 2 (v3, single seed): composite 0.461 -> 0.429, tool 0.713 -> 0.768,
memory 0.284 -> 0.237.

The tool half RISING is the expected direction: v4's corrections are
concentrated there (memory-routing no longer charged as overshoot, read-only
retrieval no longer zeroing abstention cases, the efficiency and over-call
factors no longer stacking past a floor).

The memory half falling is NOT evidence that v4 made memory harder. Both runs
are single seeds; the drop of 0.047 is about 1.5x the within-run
`composite_stderr` of 0.032, so it is inside the range seed variance alone
produces. v4 also changed the memory case mix (the canary is no longer
audit-duplicated, delete instructions carry AnswerAcknowledge), so the two
denominators are not the same set of questions. Treat the composite as the
comparable figure and re-measure over 24 seeds, as Run 1 did, before drawing a
conclusion about the memory half.

### Gates and latency

- Observed / capped tool cases: 45 / 15 (scored path would zero the capped 15)
- Injection attempts flagged: 5
- Transform robustness: 0.800
- Metamorphic consistency: 0.667
- Lexical gap: 40 questions, 0 rewritten, mean 0.288 (unchanged before/after)
- Median latency: ~12.5 s per case (reported, not scored)

### Hermetic between-seed spread (`cmd/benchcal`, 30 seeds, v4)

| metric | mean | sd |
| --- | --- | --- |
| tool_mean | 0.371 | 0.0288 |
| composite | 0.4935 | 0.0160 |

Noise floor (repeat-seed) sd is exactly 0, confirming v4 generation is
deterministic. Note `benchcal` scores the deterministic reference routing
policy against a SYNTHETIC memory profile, so its absolute numbers are a
dataset-difficulty signal, not a harness score — only the spread transfers.

### Weakest categories (n = cases in this single run)

Memory, all at 0.00: `computed-answer` (1), `injection-resistance` (5),
`point-in-time` (3), `knowledge-update` (2), `preference` (1), `isolation` (4),
`assistant-recall` (3), `memory-write-read` (5).

Tool, all at 0.00: `multi_web_read` (1), `set_effort` (1), `web_result_usage`
(1), `calendar_create` (1), `web_recovery_result_usage` (1),
`arg_hallucination` (1). Then `web_search` 0.28 (2), `workflow_not_job` 0.30 (2).

Single-run per-category counts are small; these are directional, not stable
estimates. The memory half is still where the composite and the competition
live, unchanged from v2 and v3.
