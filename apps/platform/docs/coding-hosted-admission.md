# Durable hosted Coding admission

Status: Platform persistence and transaction functions. The separate hosted
control route supports admission/status only with explicit signer injection.
No worker, selector, object grant, key grant, terminal result or scoring path
is enabled.

The operator-side creator accepts an immutable assignment authority and an exact
confirmation digest, plus actor/reason audit fields. Its caller must enforce
operator authentication and approval; the confirmation digest is not a bearer
credential or a signature. Validator admission cannot call the creator or name a
private task. Release registration alone cannot create an executable assignment.

Assignments bind evaluation and attempt UUIDs, the registered private release,
current screened artifact/image, validator audience, opaque selection commitment,
policy and execution/grading profile digests, and a deadline of at most one hour.
The normal screened-agent lifecycle is checked. This is not proof of v2 public
canary qualification or an approved competitive sampling policy; those remain
activation prerequisites. The assignment contains no task contents or storage
credentials. The selection commitment must come from a separately verified
Platform-private selection authority, not a validator claim.

Admission verifies the existing signed hosted request, checks audience/artifact/
assignment/policy, and consumes the existing global validator nonce guard in the
same transaction as admission. A repeated nonce is rejected. A new nonce for the
same evaluation returns the same attempt; only the first evaluate call changes
`assigned` to `admitted`. Status never admits work. Request expiry is rechecked
after acquiring locks. No terminal acknowledgement is accepted by this layer.

The start function records a worker UUID and start time once. The caller MUST
commit its transaction before launching candidate code, and MUST launch only
when `newly_started` is true. Reconnecting with the same worker gets the original
attempt without start permission. Another worker is refused after the boundary.
A crash after commit does not grant a retry: later recovery must reconcile that
attempt or record an explicit infrastructure failure, never clear its start.

Lock order is release, agent, assignment. Admission/start consult release
retirement/quarantine and current artifact identity under those locks. Any release
lifecycle event blocks these operations. The registry stays non-selectable and
weight-ineligible; this explicit shadow approval is separate from registration.

PostgreSQL forbids changing assignment identity, deleting its row, clearing or
rewriting admission, or transferring/resetting a started attempt. Nullable state
fields use paired-presence checks, so SQL NULL semantics cannot bypass the state
constraints. The global nonce guard keeps its existing expiry/cleanup policy.

Tests use real migrated PostgreSQL, synthetic registered releases/artifacts and
real validator signatures. They exercise concurrent admission/start, replay,
rollback, retirement, artifact drift and raw SQL guard violations. They do not
prove live Hippius access, production KMS custody or candidate isolation.

The validator route and bounded signed pending responses are described in
`docs/coding-hosted-control-v2.md` at the repository root. Next integration:
authenticated operator provisioning; Platform worker launch,
patch freeze, pristine grading and evidence finalization; then signed terminal
receipts and acknowledgements. These steps must preserve this irreversible start
boundary and keep private data away from validator hosts.

The injected task-scoped grant store and patch-freeze ledger are now described
in `coding-hosted-private-grants.md`; they remain unavailable to validator routes.
