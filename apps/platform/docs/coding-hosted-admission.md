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

## Operator cancellation (unstarted assignments only)

`POST /api/v1/admin/coding-hosted-assignments/{evaluation_id}/cancel` appends
one row to `coding_hosted_assignment_cancellations`. It requires the admin
bearer token, a reason of 8 to 512 characters after trimming (the same bound as
the assignment's registration reason), the actor, the current
`expected_assignment_sha256`, and the exact confirmation
`CANCEL SHADOW CODING HOSTED ASSIGNMENT {evaluation_id} {assignment_sha256}`.
A wrong phrase or an out-of-bounds reason is 422, a stale digest or a started
attempt is 409, an unknown evaluation is 404.

Only an assignment whose attempt never started can be cancelled: pending
admission, or admitted with `started_at IS NULL`, including one already past
its deadline. The row records `prior_state`, reason, actor and the database
time. Replaying the same reason and actor returns the same record with
`idempotent=true`; any other audit is a 409 and never rewrites the first row.
A replay also re-closes a private task it finds still open, so it converges
rather than assuming the first write removed access.

A started attempt is refused on purpose. Its candidate process, relay, object
grants and inference reservations belong to the Platform worker, which is the
only component that can quiesce them, settle or mark requests uncertain, and
close the task with an accountable reason. An administrator database write
cannot prove any of that, so running attempts stop through the worker's own
abort path (`HostedAuthoringControl.abort`), never through this endpoint.

Effects, all in the cancel transaction and never deleting or rewriting a row:

- The bound private task, if any, is closed first with the existing one-way
  `aborted` close, which already revokes both object phases and makes
  grading-claim and inference-grant inserts fail in PostgreSQL. The
  cancellation row is appended after that close.
- `_locked_assignment`, the shared lock step of admission (evaluate, status,
  acknowledge), start, private-task binding, object-grant lookup, launch
  inspection, authoring inputs, grading authority and inference issue/dispatch,
  refuses a cancelled evaluation with `HostedAssignmentCancelledError`.
  Operator create replays answer 409.
- PostgreSQL independently refuses every later change to a cancelled
  assignment row (so it can never be admitted or started), a private task
  insert for it, a cancellation insert for a started assignment, and a
  cancellation insert while a private task for it is still open. The
  cancellation table is append-only.

Lock order is assignment then task, the same as worker close. Cancellation
never takes the release or agent lock, so it cannot deadlock admission/start
and still works after release retirement or artifact drift. Concurrent cancel
and start cross exactly one boundary.

Every guard serialises on the assignment row. The cancellation trigger takes
`FOR UPDATE`; the private-task insert trigger takes `FOR SHARE` before looking
for a cancellation. Each later statement in those triggers gets a fresh READ
COMMITTED snapshot, so a task insert that waited behind a cancellation sees it
and fails, and a cancellation that waited behind a task insert sees the task.
Without the `FOR SHARE`, a raw task insert would pass its check and wait only
in the foreign-key lock, then commit after the cancellation.

`_locked_assignment` looks for a cancellation only while `started_at` is null.
Cancellation and start are mutually exclusive in PostgreSQL, so a started row
cannot have one, and the running-attempt paths (inference, launch, inputs,
grading, control) keep a single locked read. For an unstarted row the lookup is
a separate statement after the lock: an `EXISTS` column inside the locking
`SELECT` would keep the snapshot taken before the lock wait and miss a
cancellation committed by the lock holder. The start path then fails only in the
trigger, as an integrity error instead of a named refusal.

Expiry stays derived, not mutated: an unstarted assignment past `expires_at`
reads as `expired`, and admission, start and binding already refuse it. There
is no background expiry writer.

### What a validator sees

The signed hosted contract is unchanged and has no cancelled state. A validator
that sends `evaluate`, `status` or `acknowledge` for a cancelled assignment gets
the same bounded refusal as any other admission conflict: HTTP 409 with
`{"detail": "hosted Coding request refused"}` and no signed body. The validator
transport treats every non-200/202 answer as `HostedCodingTransportError`, a
control-plane failure that is never evidence of candidate failure, and it does
not retry automatically. Its nonce is not consumed.

Platform logs each refusal as
`hosted validator request refused status=409 reason=<reason> evaluation_id=...
validator=... operation=...`, where `reason` is
`hosted assignment is cancelled` for a cancellation, `nonce replay` for a
reused nonce, or the static admission message otherwise. The admin detail view
shows `state=cancelled` with the cancellation record.

## Redacted lifecycle views

`GET /api/v1/admin/coding-hosted-assignments?limit&offset` pages summaries,
newest first (`created_at`, then `evaluation_id`), with the untruncated total.
`GET /api/v1/admin/coding-hosted-assignments/{evaluation_id}` returns one
lifecycle. Both are admin-only and `no-store`, and use the database clock.

Each read runs in one `REPEATABLE READ, READ ONLY` transaction. The list's
total and page come from the same snapshot, so `has_more` agrees with the rows.
The detail is one row with outer joins over the 1:1 lifecycle tables plus the
inference and delivery aggregates, then at most one page of the newest
deliveries. Terminal presence and outcome come from the same row, so evidence
committed mid-read is either fully visible or not at all.

State precedence is `cancelled`, then the private task close reason
(`completed`, `failed`, `aborted`), then `expired`, `running`, `admitted`,
`pending_admission`.

`GET /api/v1/admin/coding-control-plane` uses the same derivation but keeps its
seven published states: a deployed Backroom rejects the whole response on an
unknown enum value, so Platform must be deployable before Backroom. A cancelled
assignment reads `aborted` there, with the additive `cancelled: true`. Backroom
defaults a missing `cancelled` to false, so either side may deploy first.

The detail view returns lifecycle timestamps, assignment and authority digests,
the cancellation record, private task bind/freeze/close timestamps and close
reason, authoring evidence and grading-claim presence, terminal outcome with the
sealed evidence digest, inference grant limits with request counts and totals,
and up to 20 newest result delivery digests with acknowledgement times plus the
full counts. `charged_*` totals count settled usage plus the full ceiling of
each reserved or uncertain request, which is what the grant enforces;
`settled_*` totals count provider-settled usage only; `verified` means revoked
with nothing reserved or uncertain.

The queries select named columns only. They never load the assignment authority
document, worker identifiers, the private selection or catalog index, patch
digest or size, grading binding, test groups or counts, the terminal domain or
sealed blob coordinates, settlement documents, result bodies or grant
identifiers. The terminal domain exists only inside sealed evidence; the view
exposes the Platform outcome derived from it.
