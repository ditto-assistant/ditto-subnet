# Approved miner evaluation billing

This change is inactive by default. It leaves the submission fee numerically
unchanged and separates that fee from inference charges. The live fee observed
by the coordinator was 0.05 TAO; this implementation does not lower the global
fee or create a per-account discount.

## Admission and consent

A linked submission receives a durable, cycle-keyed competition proposal before
it can claim validator capacity. The linked Ditto user reviews the estimate and
maximum in Ditto and explicitly approves it. Approval reserves the whole cap in
the user's canonical credit wallet and app daily allowance. The Platform queue
filters funded plans before choosing its first candidate, so an older unpaid
proposal cannot starve a funded one. Preparing, approving and resuming a plan
does not upload another submission or charge another submission fee.

The miner console shows budgets, the funding link, and comparisons waiting for
a payer. `GET /api/v1/me/evaluation-budgets` returns only the signed-in miner's
runs and payer candidates. `POST /api/v1/me/evaluation-budget` accepts
`agent_id`, `phase` and an optional confirmation `bundle_id`. It creates a
proposal, never a spending grant. Shared comparisons require an explicit
candidate payer; adding an incumbent subject cannot redirect an approved payer.
The full frozen reader, judge and embedding maxima appear in that candidate's
confirmation quote. No subject is charged merely because it is compared.

An immutable grant-to-run binding prevents an old lease from spending a later
cycle. Maintenance admission requires an unused approved cycle; canonical
operator retests require a budget created after their queued request. A miner
can prepare a fresh evaluation after old work finishes. Replacing an active or
uncertain run is refused. Each new cycle requires a new explicit approval.

## Metering and receipts

Competition chat, competition embeddings, confirmation reader, confirmation
judge and confirmation embeddings reserve against one approved phase budget
before provider dispatch. The full mode preserves the existing frozen provider
and model paths. It bypasses the older competition-only Router inference path:
accounting uses `/v1/evaluation-billing`, so the same call is not also charged
through Router inference. Callers cannot choose their payer using HTTP headers.
The linked identity and grant binding determine it.

The trusted app server settles usage and provider-cost receipts. Browser users
cannot submit provider charges. Canonical receipts contain run, agent, grant,
operation, stage, provider route, observed provider, receipt identity, model,
usage and cost. Costs use trusted provider usage and the frozen lane's pricing
contract; a derived usage receipt is identified as such when the provider gives
no external receipt ID. Actual provider cost above the individual reservation
is recorded as an overrun, cannot exceed the miner's approved charge, and stops
further dispatch on that run.

Temporary occupancy by in-flight calls returns retryable 503 with Retry-After;
unapproved or truly exhausted caps return 402. Neither is a provider-health
failure. Duplicate operation IDs cannot dispatch twice. A lost or canceled
provider response remains unknown and held. It is never refunded merely because
a timeout elapsed. Unknown outcomes require an authoritative receipt or explicit
proof of no provider cost before an operator can settle or release them.

Cancellation persists stop_requested immediately, blocking new provider
reservations even while prior operations remain held. The API reports the
pending-receipt state clearly; existing settlements remain authorized. Platform
expires only leases actually bound to that run, never a newer cycle. The
reconciler closes completed, stopped or expired runs only after all in-flight
operations settle. It releases the unused whole-run hold and
unused app allowance. Settlement of an already-held operation remains available
after user spending consent is revoked; new dispatch is refused. A deleted or
disabled reporting key/app remains an operator recovery prerequisite.

Canonical wallet debits also see the evaluation hold. Ordinary Router app
reservations use a volatile PostgreSQL function that locks the wallet first,
then reads shared holds with a fresh statement snapshot, then reserves daily
allowance. A single locking CTE is insufficient: its pre-lock snapshot can miss
a concurrently committed cap approval.

## Configuration and bounded live-test prerequisites

Platform/model-relay use `DITTO_EVALUATION_BILLING_ENABLED=false` by default,
a trusted Ditto HTTPS billing URL, an app-owned user-billed server endpoint key,
and bounded estimate/cap/request limits (see `.env.example`). Backend separately
requires the enable flag and `DITTO_EVALUATION_BILLING_APP_ID` pin. Its default
maximum is USD 5; whole confirmation profiles can quote more, so the backend
maximum must be explicitly large enough for the exact frozen quote, never more
than USD 50. Competition estimates are configured operating estimates, not a
new live cost measurement; qualify them against the exact dataset before use.

Before a live test:

1. Verify the deployed Platform OIDC client ID, the owning app, its user-billed
   endpoint, callback origin, credits:spend rationale and the consenting linked
   miner. A similarly named CLI-visible app is not proof of that binding.
2. Use an isolated Platform/relay instance and a dedicated test miner, or add
   and qualify a cohort gate before enabling this global flag on shared
   production. No production activation or secret movement is included here.
3. Pin exact backend, Platform and relay commits, active benchmark version,
   frozen comparison profile, payer UID/hotkey, test artifact and treasury
   destination. Record the approved credit cap, payment amount, chain quote,
   slippage and gas limits before signing anything.
4. Prove proposal -> explicit approval -> real queue admission -> all five
   stage receipts -> run closure and unused-credit release, with exactly one
   submission fee. Assert the app-owner wallet stays unchanged. Recheck live
   identity and app consent immediately before provider dispatch.
5. Compare canonical receipts, provider receipts and wallet deltas. Unknown
   costs remain held and block closure. A successful API status alone is not
   acceptance. TAO/alpha conversion and treasury stake proof are qualified by
   the separate payment-worker runbook.

No live chain transfer, provider-paid inference, global fee change, merge,
deployment or production activation was performed for these tests. The miner
budget component has automated interaction coverage; hosted browser acceptance
and a single unified live five-stage run remain to be performed after those
configuration and bounded-spend prerequisites are satisfied.

## Qualification evidence

- Real PostgreSQL backend tests cover aggregate cap races, simultaneous wallet
  approvals, legacy debit protection, ordinary Router reservation serialization,
  consent revocation, receipt replay/deduplication, unknown reconciliation,
  provider overruns and lock order. The real HTTP-handler test covers trusted
  service authentication, wrong-user/cap refusal, approval, all five stage
  receipts, retryable occupied funds, revocation, unknown closure refusal and
  safe unused release. It asserts exactly five receipts and no owner debit.
- Real Alembic/Platform tests cover funded queue filtering, expiry/replacement,
  immutable grant binding, explicit whole-comparison payer binding, and fresh
  maintenance/operator-retest approvals. Five lane refusal tests cover local
  permit cleanup without provider-health observation.
- Model-relay tests cover frozen direct-provider dispatch, accounting-only
  requests, no duplicate Router dispatch, unknown holds, cancellation and
  credential-safe redirect refusal.
- Negative controls remove queue filtering, explicit shared payer binding,
  payment-health separation and ordinary-wallet hold protection. Each failed
  the corresponding real check; restored source passed again.
- Schema mirrors are generated from the real Alembic/migration chains; Backroom
  types are generated from the actual new OpenAPI schema. Exact commands and
  run identifiers are included in the PR, with remaining CI/hosted checks stated
  separately.
