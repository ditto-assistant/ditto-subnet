# Coding private shadow terminal v2

Status: proposed shadow-only terminal contract. This document does not create a
live score ledger, ranking, weight, or emission.

The terminal accepts only complete quorum-resolved evidence bound to one
artifact, one registered release, one selection authority, and expected matched
groups. Competitive identity follows Coding Memory scoring v2:

```text
absolute_condition_score =
    0.15*p0 + 0.30*p1 + 0.15*p2 + 0.20*p3 + 0.20*p4

selective_group_success = mean(min(V1, V2, V3, V4) per matched group)

memory_coding_score =
    integrity_gate * reliability_gate * lower_confidence_bound(
        0.85*absolute_condition_score + 0.15*selective_group_success
    )
```

Useful, stale, and irrelevant deltas remain diagnostics. They are not the
reward identity.

Missing or untrusted candidate evidence is unresolved and stays in the
denominator as unsolved. It is never dropped from a rate in a way that can
raise the score. Explicitly audited task-invalid or infrastructure quarantine
is recorded separately, cannot be miner-controlled, and must not improve
`memory_coding_score`.

The terminal does not write the normal score ledger or change a miner's
weight. Every v2 terminal record and aggregate remains `weight_eligible=false`.
