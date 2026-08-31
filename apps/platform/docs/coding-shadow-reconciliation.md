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

There is no background task, candidate scan, environment switch, validator task
delivery, Luna grant, scoring, deployment, or emissions effect. The only
production caller is the admin-only `POST /api/v1/admin/coding-shadow/reconcile`
route, and it is disabled unless all of the following are true:

- `DITTO_CODING_SHADOW_RECONCILIATION_ENABLED=true`;
- a separate private-catalog credential set is configured; and
- `DITTO_ADMIN_API_TOKEN` is configured for the operator request.

The caller names one agent, benchmark, catalog release, and immutable run ID;
it must repeat the server-rendered confirmation. Platform supplies the bounded
selection delay, so a request cannot choose a reveal boundary. The route returns
only `waiting_finality`, `issued`, or `already_issued` and never reveals task
identity or private bytes. It does not issue the existing k=3 validator ticket
set. Coding contract v1 remains permanently `weight_eligible=false`.
