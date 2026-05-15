# DittoBench scoring

Per-mechanism case scoring is split between the canonical Go scorer at
[`../../../go/bittensor/scoring.go`](../../../go/bittensor/scoring.go)
(what validators run in production) and the Python port at
[`../runner/scoring.py`](../runner/scoring.py) (used by the contributor
runner for fast local feedback). Parity tests in
[`../../tests/bench/test_scoring_core.py`](../../tests/bench/test_scoring_core.py)
and
[`../../tests/bench/test_scoring_retrieval.py`](../../tests/bench/test_scoring_retrieval.py)
keep both in lockstep.

This document specifies (1) the per-case weight breakdown, (2) the
**winner-takes-all weight-assignment policy** validators apply at the end
of a tempo, and (3) the recommended emission split between the two
mechanisms.

## Per-case weight breakdown

### `ditto_core` (Mechanism 0)

| Component             | Weight | Meaning                                                                                  |
|-----------------------|--------|------------------------------------------------------------------------------------------|
| `tool_selection_f1`   | 0.50   | Multiset name F1 between expected and observed tool calls; collapses to 1/0 for abstain. |
| `arg_quality_f1`      | 0.25   | F1 over argument matchers (exact, contains, regex, url_list, memory_id_list, forbidden). |
| `sequence_score`      | 0.15   | `1 - trajectory_penalty`; penalises extra hops and tool overuse.                         |
| `latency_score`       | 0.10   | 1.0 at or under budget, linear decay to 0 at 5x budget.                                  |

For no-tool ("abstain") cases the `tool_selection_f1` is 1.0 when the
miner correctly refused to call a tool and 0.0 when any tool was invoked,
so a single spurious tool call collapses the case to zero.

### `ditto_retrieval` (Mechanism 1)

| Component               | Weight | Meaning                                                                          |
|-------------------------|--------|----------------------------------------------------------------------------------|
| `evidence_metrics`      | 0.45   | 0.4 NDCG@5 + 0.3 MRR + 0.2 Recall@5 + 0.1 NeedleHit bonus.                       |
| `grounded_answer`       | 0.25   | LLM-judge score when `include_answer=true`; else falls back to evidence quality. |
| `abstain_contradiction` | 0.15   | 0.5 each for `abstain_correct` and `contradiction_pass`; 1.0 for normal cases.   |
| `stm_ltm_routing`       | 0.10   | 1.0 unless `expect_no_tools` was violated by the harness.                        |
| `latency_score`         | 0.05   | Same shape as Core.                                                              |

`mcp_parity` is **published** in the breakdown for visibility but is not
in the weighted sum. Failures below 0.9 emit an
`mcp_parity_below_gate` note; subnet operators surface this on dashboards
and may apply a separate discount to chronically failing miners.

## Aggregation across a tempo

For each mechanism the validator computes, per miner hotkey, the **mean
case score** across every case scored during the tempo:

```
mean_score[mech][hk] = mean(score for case in cases[mech] graded for hk)
```

Per-category and per-component means are also published in the
`Aggregates` block of the score report (see
[`../schemas/score.schema.json`](../schemas/score.schema.json)) so
auditors can verify weight assignment without re-running the cases.

## Winner-takes-all weight-assignment policy

The subnet uses Bittensor's **multi-mechanism** facility (each mechanism
gets its own Yuma Consensus bond pool and weight matrix). Validators
implement a **winner-takes-all weight policy *per mechanism***:

```
weights[mech] = {
  top_miner[mech]: 1.0,
  every_other_miner: ~0.0,
}
```

`top_miner[mech]` is the miner with the highest `mean_score[mech]` over
the tempo. Ties are broken by:

1. lower aggregate latency,
2. lower aggregate token cost,
3. earliest registration block.

`~0.0` is a tiny non-zero floor (e.g. `1e-6` after normalisation) so
Yuma Consensus weight matrices remain well-conditioned. Pylon normalises
the vector before commit; see
[`ditto/chain/client.py`](../../chain/client.py) ``put_weights``.

The two mechanisms are scored independently, so a miner that wins
`ditto_core` but loses `ditto_retrieval` still earns the `ditto_core`
share of emissions and vice versa. This is a deliberate split: tool
routing and memory retrieval have different optimization profiles, and we
do not want to force every miner to be expert in both.

### Why winner-takes-all?

Ditto's product needs **the best** memory stack and **the best** tool
routing, not a noisy weighted ensemble. With two on-chain mechanisms and
a winner-takes-all policy per mechanism, the subnet rewards focused
mastery and avoids paying for diluted middle-of-the-pack work. Bittensor
multi-mechanism support gives us this without forcing a single combined
score.

This is implemented at weight-assignment time, not at scoring time: the
per-case breakdown remains rich and continuous so contributors can see
exactly where they need to improve.

## Emission split between mechanisms

Recommended starting split (subnet owner can tune via
`sudo_set_mechanism_emission_split`):

| Mechanism        | Emission share | Justification                                                  |
|------------------|----------------|----------------------------------------------------------------|
| `ditto_core`     | 50%            | Tool routing is the user-visible "agent" surface.              |
| `ditto_retrieval`| 50%            | Memory retrieval is the value proposition unique to Ditto.     |

We expect the split to drift toward `ditto_retrieval` as the suite
expands LongMemEval-derived cases and the memory stack becomes the
dominant differentiator. Adjustments require an on-chain extrinsic and
are announced one tempo in advance.

## Audit trail

Every `Score` record published by a validator carries:

- `schema_version`: the same `dittobench/1` constant as the request.
- `case_id`, `category`, `domain` (Core only), `mechanism`, `visibility`.
- `score`: the 0..1 weighted composite.
- `breakdown`: every weight component above (including `mcp_parity` for
  retrieval cases).
- `notes`: any structured flags (`mcp_parity_below_gate`,
  `timeout_or_refusal`, …).
- `graded_at`: UTC timestamp of grading.

Validators are expected to publish their full score corpus per tempo to
the subnet operator's dashboard (or as on-chain commitments) so other
validators can audit weight assignment without re-running cases.
