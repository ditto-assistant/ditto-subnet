# Shadow private coding-catalog selection core

Platform's selector core turns one immutable height assignment and one signed
catalog commitment into a reproducible one-task coding run manifest. It is a
pure shadow component: it exposes no route, stores no private bytes, issues no
ticket, and always produces `weight_eligible=false` authority.

## Inputs and authority

The selector accepts:

- a known-field `dittobench-coding-selection-assignment-v1` bound to the exact
  agent artifact, screened image, corpus commitment, coding run, and future
  block height;
- the registered signed catalog commitment;
- a finalized-block source that independently resolves block 0 and the assigned
  height and refuses an unfinalized or unavailable block;
- a private catalog interface returning one digest-only task record and its
  position-bound Merkle proof;
- the set of task-version IDs already irreversibly exposed for the release.

The selector revalidates assignment, commitment, task, and proof models from
their known-field JSON projections. It supports only `coding-selection-v1`.
It fetches finalized block 0 and rejects a chain whose genesis differs from the
catalog commitment. It then fetches the finalized assigned block itself; a
caller-provided or merely best-head block hash is never accepted as chain
authority.

The assignment digest makes later persistence auditable, but this layer does
not persist the assignment. Production anti-grind operation still requires an
append-only assignment row committed before the future block is revealed. That
ledger and its scheduler belong to the later shadow issuer PR.

## Deterministic selection

The seed is a canonical, domain-separated SHA-256 projection of:

```text
assignment digest
agent artifact digest
coding run and corpus IDs
catalog Merkle root and derivation ID
assigned block number and independently fetched hash
```

The seed derives an affine permutation over every catalog index. The start and
step use rejection sampling, and the step must be coprime with the catalog
size, so probing never repeats an index. Already-exposed task versions are
skipped in that deterministic order. Contract v1 selects exactly one task; an
exhausted catalog fails instead of reusing a task.

Selection alone does not consume the task. The later issuer must insert the
shared run and call the existing exposure ledger in one database transaction.
Its unique task-version constraint remains the final concurrency authority.

## Merkle contract

Each private task payload binds the release, zero-based catalog index, opaque
task-version ID, repository epoch, issue, runtime-policy, and budget digests,
case/variant/profile capabilities, and all visible/runtime/grader content
identities. Its canonical SHA-256 is wrapped in a position-bound leaf:

```text
H("dittobench-coding-catalog-leaf:v1\0" || uint64(index) || task_digest)
```

`issue_sha256`, `runtime_policy_sha256`, and `budgets_sha256` use the same
canonical known-field projections as the corresponding `/coding/run` objects.
The selected private task transport must revalidate those objects and reproduce
all three digests before seeding or starting the miner harness. Ticket IDs and
capability URLs remain intentionally lease-specific and outside the task root.

The tree is padded to the next power of two with domain-separated empty leaves.
Internal nodes bind their zero-based level and ordered children. Proof length
must equal `ceil(log2(task_version_count))`; left/right order comes from the
catalog-index bit at each level. The selector rejects a valid proof for the
wrong index, release, task commitment, count, or root.

The shared synthetic vector in
`packages/dittobench-coding-contract/testdata/coding_selection_v1.json` pins the
assignment, task commitment, membership proof, selected probe, private task-set
manifest, public run manifest, Platform run authority, and exposure projection.
It contains no repository, memory, grader, or hidden-test bytes.

## Outputs

One successful call returns:

- a private task-set manifest and digest;
- the public `dittobench-coding-run-manifest-v1` shared by all validators;
- the existing Platform `CodingShadowRunAuthority` projection;
- one `CodingCatalogTaskExposure` for atomic consumption;
- canonical membership and selection proof digests.

The selector does not accept validator choice, provider choice, URLs, task
bytes, a gold patch, or a miner-reported result.

Chain or private-catalog transport failure has a distinct unavailable error so
the later scheduler can retry infrastructure safely against the same immutable
assignment; it must not choose a different height. Malformed block identity,
wrong genesis, assignment drift, task-commitment drift, or invalid Merkle
membership is an integrity error and must never be retried as a different
selection under the same assignment.

## Activation boundary

This core is not a production selector service. It adds no private catalog
transport, assignment table, scheduler, run/ticket endpoint, validator task
transport, score fold, or emissions reader. A later PR must persist future
height assignments before revelation and atomically combine selection, shared
run insertion, and exposure. Coding contract v1 remains permanently shadow-only.
