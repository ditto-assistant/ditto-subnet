# Finalized shadow coding run issuer

Platform's shadow issuer is the only supported bridge from a pre-revelation
selection assignment to a shared coding run. It independently resolves the
assigned finalized block, verifies that the immutable database assignment time
predates the block timestamp, selects one committed private task, and persists
the run, irreversible exposure, and issuance link in one transaction.

## Authority and finality

Issuance requires the exact current:

- screened agent artifact and image;
- active catalog release and canonical commitment;
- latest complete qualified core observation;
- capability certification recorded by the assignment and still active.

The chain source must return finalized hashes for genesis and the exact assigned
height plus the selected block's canonical timestamp. The assignment database
time must be strictly earlier than the block timestamp. A five-second tolerance
is allowed only between the whole-second chain timestamp and the database's
microsecond issuance clock; it cannot make a post-revelation assignment valid.

The selector receives pinned genesis/selection hashes, not another caller value.
It walks the artifact-bound permutation, skips already exposed task versions,
and verifies the position-bound Merkle proof before producing the run authority
and exposure projection. A bounded issuer policy limits finalized-chain and
private-catalog I/O while database locks are held.

## Atomic persistence

One transaction and nested rollback boundary performs:

1. lock and validate the assignment, agent, catalog, policy, and certification;
2. lock the catalog release and read its consumed task-version set;
3. select and verify one private task;
4. insert the shared shadow run;
5. insert the complete irreversible exposure set;
6. insert `coding_shadow_run_issuances` linking the exact assignment and run.

Composite foreign keys bind both sides' agent, artifact, image, benchmark,
contract, run, corpus, height, hash, and task-count authority. PostgreSQL rejects
updates or deletes. Any exposure/issuance failure rolls the new run back rather
than leaving a leaseable partial result.

The private issuance row also retains the selected permutation probe, catalog
index, and selection-proof digest. These coordinates let later private
transport reconstruct the exact selected record and verify it against the run
and exposure; they never enter the operator response.

The assignment row lock serializes concurrent issuers. Exact replay returns the
stored run and exposure without touching the chain or private catalog. A run is
not ticket-ready unless it has both an issuance link and its complete active
catalog exposure.

## Visibility and privacy

The existing admin/Backroom coding-evaluation read reports the assignment link,
finalized block identity/time, and whether a run was issued. It does not expose
task-version IDs, catalog indexes, Merkle proofs, repository or memory bytes,
grader material, issue text, or patches.

## Activation boundary

This is an internal, caller-driven shadow transaction. It adds no automatic
scheduler, private catalog transport implementation, validator task delivery,
Luna grant, ticket issuer, score fold, deployment, or emissions reader. Coding
contract v1 remains permanently `weight_eligible=false`.
