# Coding private shadow terminal v2

Status: proposed shadow-only terminal contract. This document does not create a
live score ledger, ranking, weight, or emission.

The terminal accepts only complete quorum-resolved evidence bound to one
artifact, one registered release, one selection authority, and expected matched
groups. It persists:

```text
p0 through p4 condition rates
useful, stale, and irrelevant diagnostic deltas
absolute monotone condition score
selective matched-group success
expected, quarantined, missing, and untrusted evidence counts
repository-stratified confidence inputs
```

Missing or untrusted candidate evidence is represented as unresolved. It is
never silently excluded. Explicitly audited task-invalid or infrastructure
quarantine is recorded separately and cannot be miner-controlled.

The terminal records a conservative lower confidence bound for diagnostics, but
does not write the normal score ledger or change a miner's weight. Every v2
terminal record and aggregate remains `weight_eligible=false`.
