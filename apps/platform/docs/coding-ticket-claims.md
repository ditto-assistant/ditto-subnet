# Shadow coding ticket claims

Platform exposes three signed validator endpoints:

- `POST /api/v1/validator/coding-shadow/claims/next`
- `POST /api/v1/validator/coding-shadow/claims/{ticket_id}/start`
- `POST /api/v1/validator/coding-shadow/claims/{ticket_id}/heartbeat`

Every request binds validator hotkey, stable worker-instance identity, fresh
nonce, and timestamp. Start and heartbeat additionally bind the exact ticket
and monotonically increasing claim generation. Responses are no-store,
contract-v1, and permanently `weight_eligible=false`.

Claim acquisition is serialized per validator/instance with a transaction
advisory lock and per ticket with `FOR UPDATE SKIP LOCKED`. One unstarted claim
has a two-minute heartbeat lease. If that lease expires before start, another
instance may claim the ticket with a new generation.

`start` is the no-clean-retry boundary. Once set, `claim_started_at` is never
cleared merely because the heartbeat expired, so a different instance cannot
execute the candidate again. The same stable instance may renew the exact
generation after a restart and continue from its validator-local durable
outbox. If that instance is permanently lost, the ticket remains stranded for
a later explicit terminal-infrastructure reconciliation; it is not silently
reassigned.

New authoring-freeze, grading-lease, and terminal-result publication require a
currently active started claim. Exact already-stored Platform publication
replays remain idempotent after the lease expires. Terminal result acceptance
does not immediately clear a started claim: the same stable instance may
recover or heartbeat it while the exact claim generation lacks finalized
`terminal-publication-acknowledgement` evidence. The ticket remains excluded
from new-claim selection and cannot transfer to another instance. Once that
finalization is appended, heartbeat/start fail and the next claim lookup clears
the completed authority. The validator-local outbox has already persisted the
redacted upload identity at that point, so it may replay the exact finalized
receipt after cleanup and finish its local release. That replay grants no new
candidate, grading, upload, or heartbeat authority.

The separate validator worker now consumes this ledger only behind its
default-off gate. The ledger itself does not start a container, score a result,
or change weights.
