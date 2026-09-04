# Coding Memory v2 weighted-activation proposal

Status: proposal only. This document does not activate Coding, change weights
or emissions, provision Hippius, deploy workers, or authorize a release.

Coding Memory v2 can become eligible only through a new immutable version. No
v1 or v2 shadow evidence is retroactively weighted. The activation version
MUST bind exact contract, datagen, scorer, artifact-screening, provider-profile,
and policy digests; an unsigned configuration toggle is insufficient.

## Required evidence before an activation proposal

1. A reviewed private corpus release with matched V0–V4 groups, private
   condition labels, balanced visible-bundle commitments, and a documented
   memory-volume distribution. The initial profile contains 50 complete groups;
   each artifact selects six groups and executes all five arms for 30 attempts.
2. Reproducible shadow execution by independent validator quorums for the same
   immutable artifact/task/condition authority, with fresh workspaces and no
   retained state between variants.
3. Calibrated task groups: oracle/control sensitivity, valid supersession
   graphs, no gold-patch or hidden-test leakage, and quarantined invalid groups.
4. Statistical power and reliability analysis across repository and task-family
   strata, using a conservative lower confidence bound.
5. Passing adversarial baseline audit, including V0 sandbagging, irrelevant
   context stuffing, stale-memory following, missing-evidence handling, and
   cross-artifact pairing attempts.
6. Current Hippius provider profile, credential-revocation observation,
   metadata review, recovery drill, and any optional Object Lock profile.
7. Independent review of miner-visible/public evidence projections to verify
   that raw storage, group, condition, task, and secret material remain absent.

## Immutable activation record

```text
coding_memory_contract_version
scoring_formula_version and coefficients
contract/datagen/grader/terminal/backroom artifact digests
private release and test-manifest commitments
provider-profile and recovery-drill receipt digests
quorum, replication, confidence, and quarantine policy versions
effective block/epoch and rollback authority
```

The record pins a monotone condition-absolute competitive formula. Causal lift,
stale/irrelevant deltas, and efficiency remain published diagnostics unless a
later immutable version passes a separate incentive audit. A candidate cannot
gain score from omitted evidence, failing a control, or an untrusted replicate.

## Rollout and rollback

The first proposed version is canary-only with an explicit, bounded exposure
and independent monitoring of throughput, reproducibility, provider health,
quorum agreement, and score distribution. It has no implicit fallback to a
different object store or scoring rule.

Rollback disables only that immutable Coding Memory version and preserves the
evidence necessary for audit. It does not delete private objects automatically,
rewrite historical scores, or activate an older shadow contract. Any subsequent
activation requires a new signed version and fresh gates.
