# Signed coding catalog commitments and exposure consumption

Platform stores signed commitments and irreversible task-version exposure for
private DittoBench Coding catalogs. It does not store repository archives,
memory content, hidden tests, reference patches, policy labels, or curator
notes in these tables or operator responses.

## Catalog registration

An offline curator signs the canonical
`dittobench-coding-catalog-commitment-v1` projection containing:

```text
coding contract version and weight_eligible=false
opaque corpus release ID
catalog Merkle root
selection derivation ID and chain genesis hash
grader contract and inference-grant digests
private task-version count
curator hotkey and commitment time
known-field commitment SHA-256
```

Backroom registers that immutable commitment only after verifying the curator's
sr25519 signature, membership in the server-side
`DITTO_CODING_CATALOG_CURATOR_HOTKEYS` allowlist, an operator reason, and exact confirmation
`REGISTER SHADOW CODING CATALOG {corpus_release_id}`. Exact retry is
idempotent; reusing either the release ID or commitment digest for different
bytes conflicts. No release exists by default, and an empty curator allowlist
disables registration.

Registration proves who committed which root before candidate selection. Run
creation additionally requires both the signed commitment time and the Platform
registration time to predate the candidate artifact upload. It does not prove
that a task is valid or that a later selection block is real.
The future selector must independently fetch the canonical block hash at the
predetermined height and verify selected Merkle proofs before inserting a run.

## Exposure before lease

For a selected shared run, the private selector must atomically consume exactly
the run's task count, ordered by manifest index. Each exposure binds an opaque
task-version ID and only content identities for its visible capsule, base tree,
memory bundle, runtime image/profile, grader bundle/image, test manifest, and
grader plan.

Each exposure also stores content-addressed roots for the independently verified
chain-selection proof and catalog-membership proof. The proof artifacts remain
private, but their immutable identities are available for later audit.

The database enforces:

- one use of a task-version ID per catalog release;
- one exposure per shared-run manifest index;
- the exposure's release ID, corpus ID, run ID, and task count through composite
  foreign keys;
- complete exposure before any new validator ticket can be issued;
- `weight_eligible=false` on every release and exposure.

Database triggers reject every update or delete of catalog releases,
retirements, and exposures. Foreign-key restrictions also prevent deleting a
catalog or candidate run after exposure; consumption cannot disappear through
an agent-cleanup cascade and silently make a task reusable.

Exposure happens before transport. A validator disconnect, candidate failure,
invalid result, or infrastructure failure never makes the selected task version
available again. Exact full-set replay is idempotent. Partial or different
replay conflicts and the transaction rolls back.

## Retirement

Retirement is a separate append-only terminal row. It requires the exact
commitment digest, an audit reason, and confirmation
`RETIRE SHADOW CODING CATALOG {corpus_release_id}`. Retirement blocks new runs,
new exposures, and new tickets. Existing ticket/result evidence stays readable,
exact retries remain idempotent, and already-issued work may settle.

Backroom exposes signed commitments, retirement state, and aggregate
exposure/run counts only. It never returns task-version IDs or the per-task
digest projection while the release is active.

## Activation boundary

This PR adds no catalog bytes, chain selector, Merkle-proof verifier, automatic
scheduler, validator task transport, model route, score fold, or emissions
reader. Coding contract v1 remains shadow-only. Any weighted activation still
requires contract v2, calibration, and separate owner approval.
