# Separate shadow coding evaluation ledger

Platform persists DittoBench Coding results in tables and endpoints that are
separate from the ordinary tool-and-memory score pipeline. Coding contract v1
and every stored result are permanently `weight_eligible=false`.

## Authority chain

One immutable shared run fixes the following for every validator:

```text
agent UUID and source artifact SHA-256
screened image SHA-256
benchmark and coding contract versions
corpus release and catalog commitment
chain-derived selection authority
inference and grader contract digests
task-set and canonical run-manifest digests
task count
exact core-qualification observation
```

A run can be created only while that exact artifact has a current complete core
qualification. Each validator-specific ticket then binds the run to one active
artifact certification that remains valid through the two-hour maximum lease.
All validators under a run therefore share one task-set manifest while retaining
separate ticket IDs, deadlines, certifications, signatures, and result rows.

This layer provides no production run or ticket issuer. The signed
catalog/exposure layer requires a registered active commitment and full
irreversible exposure before any new ticket. The separate selector core can
verify one assigned block and private task proof, while the assignment ledger
persists the predetermined future height. Neither schedules nor inserts a run.
The separate issuer atomically creates the run and exposure. An internal k=3
ticket-set issuer can then bind that run to an explicitly supplied, currently
permitted validator set, but no scheduler or delivery route invokes it. The
validator submission route therefore still has no live lease to accept.

Persisting a selection block number/hash is not chain verification. The selector
core independently fetches the canonical block hash and validates the selected
manifest; re-hashing a Platform-supplied string is explicitly insufficient.
The future issuer must additionally prove that its immutable height assignment
predates revelation.

## Signed results

`POST /api/v1/validator/agent/{agent_id}/coding-shadow-result` accepts a result
only when:

- the permitted validator signs the agent, run, ticket, benchmark, deadline,
  screened image, and canonical run-evidence digest;
- the immutable ticket belongs to that validator and shared run;
- the agent artifact and screened image still match;
- the referenced capability certification is exact and unexpired;
- the coding run ID, contract, manifest digests, task count, and ticket identity
  match the lease;
- the run evidence reproduces its terminal-domain counts and binary repair mean.

Exact transport replay is idempotent. Reusing a run, ticket, or result identity
for different authority returns a conflict. Infrastructure, invalid-task, and
control-plane outcomes remain non-scoreable; resolved, repair-failure, and
candidate-integrity outcomes form the integer repair mean. The ledger never
writes `scores` and no rank, queue, validator weight, or emissions path reads it.

## Operator visibility

`GET /api/v1/admin/agents/{agent_id}/coding-shadow-evaluations` returns bounded
run, lease, and signed-result summaries. It exposes commitments and aggregates,
not full run evidence, private task identities, hidden tests, repository bytes,
or patches. Backroom serves the same read through
`get_agent_coding_shadow_evaluations`.

The view reports a repair median only after three validator results. A source
artifact, screened image, core-policy change, or loss of the latest complete
qualification marks historical runs stale without deleting or relabeling them.

## Activation boundary

This ledger is calibration infrastructure, not a second emissions authority.
Production still requires a shadow scheduler, validator task delivery and
scorer adapter, and measured calibration.
Any coding emissions allocation requires coding contract v2 and a separate
owner-approved PR.
