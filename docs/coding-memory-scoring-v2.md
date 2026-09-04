# Coding Memory scoring v2

Status: proposed shadow-only delta. This document does not alter Coding v1,
weights, emissions, deployment, or any activation gate.

Coding Memory v2 measures whether a miner's memory system improves executable
software-engineering outcomes while safely handling irrelevant, stale, and
overridden history. V0–V4 already exist in the private execution protocol; v2
operationalizes their matched execution and scoring rather than redefining them.

## Invariant: reward monotonicity

For a fixed immutable artifact and all other evidence held constant, changing an
eligible condition result from unsolved to resolved MUST NOT reduce the
competitive score. Missing, malformed, quarantined, or untrusted evidence MUST
not improve a score.

This rules out direct emissions formulas based on `V1 - V0`: a candidate must
not gain by deliberately failing a no-memory control.

## Separate research from reward

For each matched base task group, the private aggregator records:

```text
p0 = V0 no-memory solve rate
p1 = V1 relevant-memory solve rate
p2 = V2 irrelevant-memory solve rate
p3 = V3 stale/conflicting-memory solve rate
p4 = V4 relevant-memory plus current override solve rate

useful_lift       = p1 - p0
irrelevant_delta  = p2 - p0
stale_delta       = p3 - p0
override_success  = p4
```

These are scientific and diagnostic measurements. They are not directly
rewarded in the first weighted release. Oracle attempts validate task-group
sensitivity; they do not normalize candidate scores. A task group with a weak,
unstable, or contradictory oracle/control result is quarantined.

The initial competitive component is monotone and condition-absolute:

```text
absolute_condition_score =
    0.15*p0 + 0.30*p1 + 0.15*p2 + 0.20*p3 + 0.20*p4

selective_group_success = mean(min(V1, V2, V3, V4) per matched group)

memory_coding_score =
    integrity_gate * reliability_gate * lower_confidence_bound(
        0.85*absolute_condition_score + 0.15*selective_group_success
    )
```

The coefficients are calibration inputs, not protocol constants. All are
non-negative, so solving an additional task cannot lower the score. V1, V3,
and V4 receive more weight because private task construction makes their
knowledge decision-critical and unavailable from the visible repository alone.

## Assignment and blinding

The immutable artifact is the unit of comparison. Each condition has an
independent validator quorum executing the same artifact, base task group,
repository epoch, and condition authority. Quorum validators reproduce a
condition; they do not share one agent attempt. Condition labels and matched
group identifiers remain grader-private.

The initial private release contains 50 complete matched base-task groups. For
one immutable artifact, Platform selects six groups only after the artifact is
frozen, using the committed release root and a designated future finalized
block. Every V0–V4 arm in each selected group is executed, producing exactly 30
isolated attempts: six per condition. Selecting 30 unrelated groups with one
arm each is not equivalent and cannot feed the matched-group metric below.

Treatment concealment is defense in depth, not the incentive defense: an empty
bundle or a useful record can be recognized. Bundles therefore use placebo
padding and balanced approximate context sizes, while reward monotonicity makes
condition recognition unprofitable.

## Anti-context-stuffing and evidence rules

Task groups span small, medium, and large seeded-memory volume tiers. For a
meaningful private stratum, seeded bytes substantially exceed the fixed
model-visible memory budget. Validator evidence records seeded bytes,
model-visible bytes, relay tokens, tool calls, wall time, CPU, peak memory, and
accepted patch size. Miner-declared retrieval traces remain diagnostic only.

Every v2 result is tied to an artifact digest, opaque base-task-group ID,
private condition digest, repository epoch, replica, quorum group, policy
version, and grader evidence digest. Cross-artifact or cross-epoch pairings are
invalid. Aggregation uses conservative confidence bounds stratified by task
family and repository.

## Scope boundary

V2 covers isolated selective-memory tasks only. Stateful, multi-task episodes
(memory creation, consolidation, retention, restart recovery, and forgetting)
are a later contract with its own lifecycle and security review. No v2 result
is weight-eligible until calibration, adversarial-baseline, reliability, and
activation requirements are separately approved in an immutable version.
