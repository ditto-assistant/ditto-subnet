# Shadow coding run reconciliation

Platform can reconcile one explicitly named, qualified agent artifact from its
future-height assignment to finalized shadow issuance. This is an internal,
caller-driven core rather than a fleet scheduler.

The first phase calls the existing assignment authority, which revalidates the
active catalog, current complete core qualification, exact screened artifact,
and active coding certification before committing a future finalized height.
The reconciler then reads the finalized head only as a cheap readiness hint. It
does not select a task from that head value.

Once the assigned height is ready, the existing issuer independently resolves
the exact finalized block and timestamp, verifies the private record and Merkle
membership, and atomically persists the run, exposure, and issuance link. Exact
replay returns `already_issued`; a not-yet-final assignment returns
`waiting_finality` without touching the private catalog.

Typed assignment, chain, catalog, qualification, conflict, and integrity errors
are preserved for a later scheduler to classify into retry, terminal hold, or
operator review. This layer adds no catch-all retry loop.

## Activation boundary

There is no background task, candidate scan, environment switch, route,
validator task delivery, Luna grant, scoring, deployment, or emissions effect.
A separate internal issuer may create k=3 tickets and the task-lease builder may
reconstruct one, but no private task is consumed unless an explicit caller
invokes reconciliation after finality. Coding contract v1 remains permanently
`weight_eligible=false`.
