# Coding private shadow execution v2

Status: proposed shadow-only validator execution contract. This document does
not start a worker, issue a private task lease, or persist a score.

For one immutable artifact and selected release authority, validators execute
all thirty arms independently:

```text
6 matched groups x V0-V4 = 30 sessions
```

Each arm receives a fresh harness process, workspace, task-scoped memory state,
repository epoch, capability set, and grading environment. No state, patch,
embedding cache, mutable store, or tool sequence crosses an arm boundary.

Each validator quorum member executes the same signed arm authority but does
not reuse another validator's attempt. Validator-owned evidence records the
artifact digest, group/condition commitments, replica, workspace freeze,
authoring evidence, grader evidence, terminal classification, resource usage,
and signed result digest.

Candidate failures are scored only from authoritative authoring and pristine
grading evidence. Pre-authoritative transport, unavailable private objects, and
grader/control-plane failures are classified as trusted infrastructure. Missing
or malformed candidate evidence fails closed and cannot improve the aggregate.

All private shadow results remain `weight_eligible=false`.
