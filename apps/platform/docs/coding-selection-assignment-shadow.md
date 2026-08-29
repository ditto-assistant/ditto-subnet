# Shadow coding selection assignments

Platform persists one immutable future-height assignment before any private
catalog task is selected. The ledger proves which exact artifact, catalog, and
height were committed while the selected block hash was still unavailable. It
does not select a task, create a coding run, or issue a validator ticket.

## Finalized anchor

The assignment coordinator uses a trusted finalized-chain source. It first
checks finalized block 0 against the catalog's committed genesis, then reads the
current finalized head as the anchor. The selected height is fixed as:

```text
selection_block_number = anchor_block_number + selection_delay_blocks
```

Contract v1 accepts a configured delay in `1..=10000` and exactly one task. The
anchor number/hash, delay, selected height, assignment time, artifact and
screened-image digests, catalog commitment, benchmark version, coding run ID,
and task count enter `assignment_sha256`.

The generic `ChainClient.get_block_hash()` is not sufficient. The selector and
assignment coordinator require the explicit finalized methods, so a best-head
or caller-supplied hash cannot silently become selection authority.

## Admission authority

A new assignment requires all of the following in one Platform transaction
after the finalized anchor is fetched:

- the exact current agent artifact and screened image;
- an active signed catalog whose commitment and registration predate the agent;
- the current policy revision's latest complete, qualified core observation;
- one unexpired `certified` capability receipt for the exact artifact, image,
  benchmark version, and coding contract.

The row stores the exact core observation and certification identities. A later
artifact/image change, catalog retirement, policy revision, loss of current
complete core qualification, certification expiry, or non-certified receipt
makes it stale without rewriting history.

## Persistence and retries

`coding_selection_assignments` is append-only. PostgreSQL rejects every update
or delete. `(agent_id, coding_contract_version, coding_run_id)` and the canonical
assignment digest are unique. A second coding-run ID for the same exact
artifact, screened image, and coding contract is also rejected even when it
names another catalog release; the control plane cannot commit multiple heights
and later favor one. A new attempt requires a new artifact identity.

An exact request replay returns the first row without contacting the chain
again. Reusing the coding-run identity after artifact, catalog, benchmark,
delay, or task-count drift conflicts. Concurrent inserts converge on one row;
they cannot choose multiple heights for the same coding run.

The assignment timestamp comes from PostgreSQL `clock_timestamp()` and must
equal the row's immutable `created_at`; callers cannot supply or backdate it.
The later issuer must additionally wait for the assigned height to become
finalized and prove the database insertion time predates that block's timestamp
before calling the private selector.

## Operator visibility

The existing `coding-shadow-evaluations` admin response and Backroom MCP read
include bounded assignment summaries and derived stale reasons. They expose
only artifact/catalog/block commitments and row identities—never task-version
IDs, Merkle proofs, repository bytes, memories, grader material, or patches.

## Activation boundary

This layer adds no automatic scheduler, private catalog transport, selector
invocation, shared-run insertion, exposure, ticket, validator task delivery,
score fold, or emissions reader. Coding contract v1 remains permanently
`weight_eligible=false`.
