# Relay delay fingerprint

Deterministic, secret-keyed response delays on the trusted inference relay,
turning per-case latency into verifiable model-use evidence.

## Problem

The v9 model-use gate attributes every successful chat completion to the exact
case window it started in, so "did this case reach the model at all" is already
a trusted fact. Two evasions remain, both documented in the platform's
model-use rule (`apps/platform/ditto/api_server/model_use.py`):

1. **Partial substitution** — answer most cases from a deterministic resolver,
   call the model on the rest. Aggregate gates pass while ~90% of cases never
   see the model. Observed in production (bunny v57, cleared 2026-08-15 with
   prospective tightening as the stated remedy — this is that tightening).
2. **Latency forgery** — once operators screen on "suspiciously fast" cases,
   the counter-move is sleeping to mimic inference latency. A sleep is a
   perfect forgery of latency only while latency is unstructured.

## Mechanism

The broker holds each successful chat response for

```
delay = min_ms + HMAC-SHA256(K_session, "dittobench-delay-fp-v1" || generation || ordinal) mod (max_ms - min_ms + 1)
```

milliseconds before releasing it to the harness, where `K_session` is a
32-byte key minted with the broker session (never serialized, never visible to
the harness, the same custody rule as the session's DPoP key), `generation` is
the case window, and `ordinal` counts delayed calls inside that window.

Properties:

- **Reproducible by the trusted side.** The broker books `DelayedRequests` and
  `InjectedDelayMS` into the same case snapshot, under the same lock, as the
  success it delays. The scorer reads exact per-case totals.
- **Unpredictable to the miner.** The schedule is a fresh HMAC per (session,
  generation, ordinal); sessions are minted per run. A miner cannot precompute
  the delays and cannot observe them without actually calling the relay — at
  which point the model is genuinely in the loop, which is the outcome the
  rule wants (the same economics as the prompt-tokens-per-call gate).
- **Physically binding.** A case whose wall time is smaller than the delay the
  relay verifiably imposed inside its window returned its answer before the
  relay released a response that same window counts as delivered — direct
  evidence the response did not condition the answer.

## Verification

Per case (`v9RelayDelayEvidence`): concurrent calls overlap their holds, so
the wall clock is only guaranteed to cover the largest single injected delay.
The counters carry the sum and the count; the per-call mean is a floor on the
maximum, so `wall_time >= injected_total / delayed_count` can never flag an
honest case. The verdict is published per case in the transcript
(`relay_injected_delay_ms`, `relay_delay_consistent`) and summarized in the
operator log.

Beyond the hard floor, the recorded schedule enables residual analysis the
screeners can consume (submission-contract: "private timing … signals can only
pass or route to quarantine"): across the ~30+ delayed cases of a full run,
regress case wall time on injected delay. Honest cases show slope ~1 (every
injected millisecond appears in the wall time); cases that raced the relay or
padded with sleeps do not. That statistic needs no new wire contract — the
trusted side holds both series.

## Rollout

Follows the shadow-before-enforce precedent (ditto-platform#506 invariant 5),
and — after the bunny ruling — nothing here is applied retroactively:

1. **off** (default, this PR): no injection, no behavior change.
2. **shadow**: operator sets `DITTOBENCH_RELAY_DELAY_FP_MODE=shadow`.
   Injection on, evidence recorded and published, scores untouched. Announce
   to miners before enabling; measure the honest-cohort distribution.
3. **Screening signal** (follow-up): screener consumes the per-case verdicts
   and residual slope as a quarantine-routing signal, alongside the existing
   private timing signals.
4. **Gate** (follow-up, requires a bench-version bump): fold a published
   threshold into the signed v9+ gate evidence (`ditto-screening-protocol`),
   with explicit version negotiation and frozen calibration inputs.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `DITTOBENCH_RELAY_DELAY_FP_MODE` | `off` | `off` or `shadow`; unknown values are `off` |
| `DITTOBENCH_RELAY_DELAY_FP_MIN_MS` | `25` | schedule lower bound |
| `DITTOBENCH_RELAY_DELAY_FP_MAX_MS` | `250` | schedule upper bound; an invalid range disables injection rather than clamping |

Cost to honest runs at defaults: mean ~137ms per successful call, ~2 calls per
case honest median → ~275ms per case against a multi-second honest p50 and a
5-minute per-case budget.

## Non-goals

- Does not change any score, gate model, or wire contract in this PR.
- Does not detect a harness that genuinely waits for and then ignores the
  response *while* paying full wall time — that still requires
  response-conditioned evidence (ditto-platform#518, trace publication). The
  fingerprint prices that evasion at full honest latency, which removes its
  entire speed advantage.
- Legacy (v2–v8) sessions are untouched: injection is keyed to
  `BenchVersionV9` case windows, which only exist on the v9 path.
