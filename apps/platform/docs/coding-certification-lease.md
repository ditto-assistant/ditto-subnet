# Qualified coding-certification leases

Platform mints one shadow-only public-canary lease after it re-checks current
core qualification for the exact agent artifact. The lease is not a scoring
ticket, not a private coding assignment, and remains `weight_eligible=false`.

## Write authority

`POST /api/v1/validator/coding-certification-leases` issues a lease only when
all of these hold in one transaction:

- a permitted validator hotkey and valid sr25519 signature;
- the strict operator allowlist admits this exact
  `(agent_id, artifact_sha256, screened_image_sha256, validator_hotkey)` tuple,
  where the screened image is the agent's current verified
  `agents.screened_image_sha256` (see below; nothing is admitted by default);
- a complete, content-addressed screened image on the agent;
- a configured shadow core-qualification policy for the requested benchmark;
- the latest complete observation is `qualified` and binds the same agent,
  source artifact, screened image, benchmark, and current policy checksum;
- no `issued` or `claimed` lease already exists for that identity, after any
  overdue in-flight lease has been expired;
- no certification result for the identity
  `(agent, artifact, screened image, benchmark, coding contract)` is still
  valid on the database clock (see "Renewal" below), from any validator;
- fewer than 3 allowlist-admitted claims for that identity in the last 24 hours;
- the committed public `certification/v1` canary identity can be loaded.

Issuance and policy revision share a per-benchmark transaction lock, so a
lease cannot bind an observation that is already stale at commit. A later
incomplete observation does not hide an earlier complete qualified wave.

An absent policy, incomplete wave, unqualified observation, missing image,
stale artifact, still-valid certification, or exhausted attempt budget is not an error
against the normal submission. The endpoint returns `404`. An allowlist refusal
returns `403` with the fixed detail `coding certification is not allowlisted`,
before any lease, expiry, or grant row is written. Every refusal still consumes
the request nonce, and it commits instead of rolling back: an overdue lease the
issue expired (and the grant it revoked) stays expired even when the attempt
budget then refuses the request.

`POST /api/v1/validator/coding-certification-leases/{lease_id}/claim` is
exclusive to the named validator. Exact signed retries of an already-issued,
already-claimed, or already-aborted lease authenticate and return the stored
row. The request nonce is recorded on first success; a replay of that nonce
is accepted only when the mutation is idempotent. `POST .../abort` is allowed
only while the lease is still `issued`. A claimed lease cannot be aborted by
its validator, so a restart cannot create an immediate clean rerun. Expiry is
committed before the endpoint returns `404`. A `completed` lease is never
returned; claim and abort answer `404`.

## Retry and renewal semantics (contract v1)

An accepted receipt is the terminal result of its lease. Only a `certified`
receipt is a certification, and it holds for its identity until the receipt
expires. [`docs/coding-qualified-certification-lease-shadow.md`](../../../docs/coding-qualified-certification-lease-shadow.md)
makes `unsupported` and candidate-attributable `failed` results terminal for
their lease, not for the artifact: the same tuple may take a new lease at once,
bounded by the allowlist and the attempt budget. So:

- the transaction that accepts a receipt moves its lease from `claimed` to
  `completed`; `completed` is terminal and idempotent (an exact receipt replay
  returns the stored answer), never expires, and is never rewritten by an
  allowlist write;
- a claimed lease that ends without a receipt expires (below) and releases its
  identity, so a post-claim crash does not burn it.

### Renewal

A completed lease does not block its identity forever. Duplicates are blocked
only while the identity holds a valid certification: issue refuses the
identity while one of its `certified` receipts is still valid, and otherwise
permits a fresh lease for the same exact tuple, still subject to the allowlist
and the claimed-attempt budget:

- Validity is the receipt's own `expires_at`
  (`coding_capability_certifications`, at most 24 hours after its `issued_at`,
  which is at most the lease deadline), compared with the database
  `clock_timestamp()` read after the agent row lock. `expires_at` is exclusive:
  at that instant the identity renews. There is no separate lease TTL.
- Only `certified` receipts block. A `failed` or `unsupported` receipt is
  terminal for its own lease but is not a certification, so it never blocks a
  new lease, even while its `expires_at` is in the future; retries after it are
  bounded only by the allowlist and the attempt budget.
- A `certified` receipt blocks whatever its settlement binding, validator, or
  lease linkage, so a legacy certified receipt without a lease blocks exactly
  while it is valid.
- There is no minimum-validity knob: a certification blocks for exactly its
  own validity window.
- A `completed` lease is written only with its receipt. If that receipt were
  ever missing, its status is unknown, so the lease blocks until
  `deadline + 24h`, the longest validity a certified receipt for it could have
  had.
- Receipt acceptance holds the same agent row lock as issue, so a receipt that
  commits while an issue waits is seen by that issue, and two concurrent issues
  for one identity mint at most one lease.
- Renewed attempts and retries spend the same budget: a completed lease's
  claim was allowlist-admitted, so it counts toward the 3 claims per rolling
  24 hours whatever its receipt status. Three failed attempts in 24 hours
  exhaust the identity until the oldest claim leaves the window.

A new upload or screened-image rebuild is a new identity and needs its own
allowlist tuple.

## Deadlines, the receipt window, and recovery

Every lease has a 20-minute deadline. Harness launch, certification inference
grant offer and exchange, and relay inference (the grant's `expires_at` equals
the deadline) all end at the deadline. Only receipt submission continues, for
`CODING_CERTIFICATION_RECEIPT_GRACE_SECONDS` (120 seconds) after the deadline,
so a certifier run bounded by the deadline can still revoke its grant and
submit. The receipt's own `issued_at` must still be no later than the deadline.

Every one of these decisions reads the database `clock_timestamp()` after the
lease row lock (and any agent or grant lock) is held, never the API host clock
and never a time captured before a lock wait.

An `issued` lease is overdue at its deadline; a `claimed` lease only once its
receipt window has passed. An overdue lease is transitioned to `expired` the
next time Platform touches it: a claim or abort retry, a receipt submission,
or the next issue request for the same identity. The transition:

- keeps `claimed_at`, so an expired row still records whether it was claimed;
- terminally revokes a `pending` or `active` certification inference grant
  bound to the lease (bearer and broker bindings cleared, accounting kept);
- releases the in-flight unique slot, so the same agent, artifact, image, and
  benchmark can be certified again without a new upload.

Nothing is deleted. A post-claim failure without a receipt (validator or scorer
crash, Platform `503`, host mismatch) therefore costs at most one deadline plus
the receipt window instead of the identity.

A receipt that arrives after the receipt window is refused with `404` and no
receipt row, and the expiry commits.

An exact replay of an accepted receipt stays idempotent at any time, and it is
the one exception to the allowlist: it is answered from the stored row before
the allowlist is consulted, so a replay still returns `200` with
`idempotent=true` after the tuple was removed or the allowlist refuses
everything. It writes nothing and cannot create or change a certification. Any
receipt that is not an exact replay is refused with `403` under that allowlist.

Because expiry releases the identity, re-runs are bounded: at most 3 claims
per identity in any rolling 24 hours, counted from `claimed_at`. Only claims
an allowlist revision admitted count (`claim_allowlist_revision` is set at
claim time and is never cleared), so claims made before the strict allowlist,
or by a validator the allowlist never named, cannot exhaust the canary's
budget. An unclaimed lease does not count.

The validator-signed abort was deliberately not extended to claimed leases.
Doing so would let the claiming validator discard an attempt it has already
seen and immediately start a clean rerun, which the deadline bound prevents.

### Validator and scorer rule

The scorer and the validator certify call stay bounded by the lease deadline
itself (`certify budget = lease.deadline - now`, scorer operation deadline =
`lease.deadline`). After certify returns or fails, the validator revokes the
grant and submits the receipt once, and must not start the submission after
`lease.deadline + 120s - 15s` by its own clock (15 seconds of allowance for
host-to-Platform clock skew and request latency). A `404` for a claimed lease
after its deadline is a no-receipt infrastructure outcome, never retried with
the same receipt.

## Operator allowlist

`coding_certification_allowlist_revisions` is an append-only, admin-only,
strict restriction. It refuses everything by default: with no revision, a
latest `enabled=false` (refuse-all) revision, or a latest revision whose
entries fail to parse or whose checksum does not bind them, Platform refuses
every certification lease issue, claim, harness launch, grant offer, grant
exchange, and receipt. No revision can reopen global access: an enabled
revision must list 1 to 16 exact `(agent_id, artifact_sha256,
screened_image_sha256, validator_hotkey)` tuples, there are no wildcards, and
there is no admin certification bypass.

`screened_image_sha256` is the SHA-256 of the agent's verified screened-image
archive (`agents.screened_image_sha256`, set only once Platform has streamed and
verified the archive). It is the same digest every lease, receipt, and core
qualification observation binds. Issue compares the agent's current digest,
and every later gate compares the lease's frozen digest, so a screened-image
rebuild never matches a tuple written for the previous image: the rebuilt
image needs a new revision, and that write aborts the old image's in-flight
leases.

The check runs in the one lease authority gate shared by claim, harness launch,
grant offer, grant exchange, and receipt submission, and on lease issue and new
receipts before the agent row is locked. A refused grant offer or exchange also
terminally revokes the lease's live grant. A settlement-bound `certified`
receipt is never inserted for a tuple the current allowlist refuses.

Writing a revision, in the same transaction, aborts every `issued` or `claimed`
lease the new revision does not admit (status `aborted`, `aborted_at` set,
`claimed_at` kept, `aborted_allowlist_revision` naming the revision), re-stamps
every `claimed` lease it still admits with the new revision in
`claim_allowlist_revision`, and terminally revokes every live certification
grant it does not admit. The tuple filters run in SQL, so the only lease or
grant rows locked are the refused ones and the admitted claimed leases being
re-stamped. A `completed`, `expired`, or already `aborted` lease is never
rewritten.

The model relay enforces the same authority on every paid canary dispatch. It
admits a request only while the grant's lease is `claimed`, before its
deadline on the database clock, and stamped with the latest allowlist revision
(`claim_allowlist_revision` equals the newest revision number); otherwise it
terminally revokes the grant and returns `409` without calling the provider.
Claims stamp the revision that admitted them and allowlist writes re-stamp
admitted claimed leases, so no revision, a refuse-all revision, a revision
appended outside the admin write, and grants that were live before this
migration all stop at the relay, not at the grant's `expires_at`. The relay
compares revision numbers and does not re-verify a revision's checksum. The
residual is a latest revision that Platform wrote intact but later reads as
invalid (for example after a checksum-rule change): Platform then refuses every
call, but the relay keeps serving already-exchanged grants until their
`expires_at`, at most the 20-minute lease deadline.

Lock order is allowlist, agent, lease, grant. Every authorizing transaction
takes the shared transaction advisory lock before it locks any agent, lease, or
grant row, and a write takes it exclusively before it locks leases and grants,
so no lease, grant, or receipt commits against a stale revision and the two
orders cannot deadlock. The relay reads the lease without locking it, because
it already holds the grant row lock.

Admin API (`DITTO_ADMIN_API_TOKEN`):

- `GET /api/v1/admin/coding-certification-allowlist?history_limit=` returns
  `enabled` (true only for an intact revision with exact tuples), `effective`
  (`refuse_all` or `exact_tuples`), `integrity` (`valid` or `invalid`), the
  current revision (revision `0` is the built-in refuse-all default), and
  newest-first history with actor, reason, checksum, stored `enabled`,
  `integrity`, and `effective` per revision. A corrupt revision reads as
  `integrity: "invalid"`, `effective: "refuse_all"`, and no entries; appending an
  intact revision repairs it.
- `POST /api/v1/admin/coding-certification-allowlist` appends one complete
  revision: `expected_revision`, `enabled`, `entries`, `reason` (at least 8
  characters), `actor`, and the exact confirmation
  `APPLY CODING CERTIFICATION ALLOWLIST ENABLED <entry count>` (1 to 16
  entries) or `APPLY CODING CERTIFICATION ALLOWLIST REFUSE ALL` (no entries).
  It returns `aborted_lease_count` and `revoked_inference_grant_count`.
- `GET /api/v1/admin/coding-certification-leases` pages lease rows newest
  first (`limit` 1-200, `offset`, optional `agent_id`, `validator_hotkey`,
  `status`). Rows carry identity, status (`completed` means receipted),
  issued/claimed/aborted/deadline timestamps, `receipt_window_ends_at`,
  `deadline_passed` (database clock), `claim_allowlist_revision`,
  `aborted_allowlist_revision`, and the bound grant and receipt status. They
  never include grant ids, bearer digests, broker keys, or image locators. The
  read never transitions a row, so an overdue lease shows its stored status
  with `deadline_passed=true`.

Backroom exposes the write as `set_coding_certification_allowlist`, the
allowlist and newest leases inside `get_coding_control_plane`, and one agent's
leases inside `get_agent_coding_certifications`.

For the one-agent canary, write the enabled revision naming only the canary
tuple before enabling Platform coding transport or the validator canary
worker, and confirm it with the read tool.

## Storage

`coding_certification_leases` stores the frozen authority JSON plus the
screened-image identity (digest, config id, reference, upload id). Rows are
never rewritten to look current except for the one-way status transitions
`issued → claimed | aborted | expired`, `claimed → completed` (receipt
accepted), `claimed → expired` (after the receipt window, keeping
`claimed_at`), and `claimed → aborted` (only by an allowlist revision, keeping
`claimed_at`). Coding contract v1 stays `weight_eligible=false`.

The migration normalizes legacy rows deterministically and deletes nothing:
every lease that carries a receipt of any status and is `claimed` (or
`expired` with `claimed_at`, as a downgrade of this revision leaves it) becomes
`completed`. Receipts without a lease, and receipts on a lease that is not
claimed, need no new linkage because renewal reads receipts by identity; the
same rule applies to them: a `certified` one blocks issue only while valid, and
a `failed` or `unsupported` one never blocks. The upgrade logs an audit of
those rows (counts only: receipts without a lease and how many still block,
claimed leases normalized, receipts on a non-claimed lease, valid certified
receipts blocking renewal, and unexpired failed or unsupported receipts that do
not block).

Downgrading this migration is not a safe rollback for a live canary. The prior
code has no allowlist, so a downgrade reopens lease issue to every qualified
agent and validator. It drops the allowlist revision history and each lease's
admitting and aborting revision, and maps allowlist-aborted claimed leases to
`expired` (clearing `aborted_at`). The prior schema admits one `issued` or
`claimed` lease per identity, so only the newest `completed` lease of an
identity with nothing in flight goes back to `claimed`; every other completed
lease (older renewals, or one beside a renewed in-flight lease) becomes
`expired` keeping `claimed_at`. The prior code refuses those identities from
their receipt rows. A later upgrade maps both back to `completed` and validates
every lease CHECK.

## Activation boundary

The validator public-canary worker claims this lease, drives
`codingcertifier` through the scorer control plane, and submits the terminal
receipt against the claimed lease. Private-task admission remains a later
reviewed step. Ordinary Tool + Memory scoring, weights, and emissions do not
read this table.
