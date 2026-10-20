# Build bounties: operator runbook

Operators run build bounties through Backroom's MCP tools (see
[Backroom MCP](../apps/backroom/docs/mcp.md#build-bounties)). Every write names
its actor, takes an exact typed confirmation, and is recorded in the bounty's
private audit. The contributor side is in [bounty-guide.md](bounty-guide.md);
the rules are in [bounty-claims.md](bounty-claims.md).

v1 is rehearsal only: fees and transfers are simulated and no funds move.
Live funding needs separate operator authorization (#2944), and every write
here must first carry a proof-backed operator email: until then the actor each
write records is the staff email Backroom asserts in `X-Admin-Actor`, not
proof of a distinct operator (see [Deferred](bounty-claims.md#deferred)).
[Funded pilot (not authorized)](#funded-pilot-not-authorized) lists what a
capped pilot needs before any of that changes.

## Before publishing

1. Set the payout policy once with `set_bounty_payout_policy`: the largest
   amount one approver may approve, and the largest two distinct approvers may
   approve. Larger amounts can't be approved at all. Read it back with
   `get_bounty_payout_policy`.
2. Choose the reviewer actor (the Backroom identity who decides submissions)
   and the reviewer's public GitHub login.
3. Choose the dedicated fee escrow coldkey and hotkey for the revision.

## Publish

1. `preview_bounty_revision` with the terms and `expectedRevision` (0 for a new
   bounty). It returns the digest, the confirmation phrase and any refusal, and
   writes nothing.
2. `publish_bounty_revision` with the same terms, the reviewer actor, a reason,
   and the phrase from the preview. Revision 1 opens round 1 with three slots.
3. Check the public board at `/bounties`.

## Run the round

- **Watch the queues.** `get_bounty_queue` with `queue=reviews` lists
  submissions awaiting a decision, most overdue first. `queue=settlement` lists
  obligations that still need an operator. `queue=appeals` lists undecided
  appeals, the longest waiting first, with the event each appeals and its
  private statement.
- **Review.** The reviewer in force calls `review_bounty_submission` on the
  exact submission: accept with attribution, request changes with a rework
  deadline, or reject. Every decision has a public reason.
- **Overdue reviews.** `reassign_bounty_reviewer` names a new reviewer and gives
  overdue reviews one more window.
- **Appeals.** `get_bounty_queue` with `queue=appeals`, and `get_bounty`, show
  each appeal's private statement.
  `decide_bounty_appeal` upholds (restoring the claim with a new delivery
  deadline) or denies. A rejection's reviewer can't decide its appeal, and
  whoever set a missed deadline (by requesting changes, or by upholding an
  earlier appeal) can't decide the appeal of missing it.
- **Slots.** `control_bounty_slot` cancels a slot (refunding a paid fee in full,
  as `team` or `system_fault`) or reopens a closed one for a new claim
  (retaining a rejected, expired or withdrawn fee). Reopening waits for a
  pending appeal. A rejected or expired slot also waits out its claimant's
  3-day appeal window, counted from the rejection or the missed deadline,
  unless the appeal of it was already decided; the refusal names when the
  window ends. Withdrawn and cancelled slots reopen at once.

## Settle

1. **Award.** `award_bounty_claim` records an award on accepted work, with
   public attribution and a private reason. Awards never exceed the pool.
2. **Approve.** `approve_bounty_obligation` binds the exact payee, amount,
   policy revision and round. The award's decider and the accepting reviewer
   can't approve. An award is refused with `payout held` while the claim
   hotkey's finalized owner is not the payee of record. An amount above the
   payout policy's two-approver limit reads `over_policy_limit`: nobody can
   approve it until a new policy revision raises the limit.
3. **Dispatch.** `dispatch_bounty_obligation` sends an approved round once. The
   dispatch is recorded before anything is sent. In rehearsal, name the
   simulated outcome with `rehearsalOutcome`; it is required.
4. **Reconcile.** A transfer whose outcome is unknown is `uncertain`. Settle it
   with `reconcile_bounty_transfer` from what the chain shows. Never dispatch it
   again. Every reconciliation, an inconclusive one too, is recorded in the
   audit with its operator. A failed or expired transfer needs fresh approvals
   for the next round.
5. **Facts.** `record_bounty_fact` records a merge, deployment or adoption.
   None of them gates payment.

## Record the evaluation

Each bounty of a pilot is evaluated against building the same work
internally (#2944). Record the evaluation in Platform, where it is kept
append-only:

- **Estimates and actuals.** `record_bounty_evaluation` records an `estimate`
  or an `actual` of one metric: `build_hours`, `reviewer_hours`,
  `integration_hours`, `operator_hours` or `turnaround_days`, as a
  non-negative number up to 1,000,000, with an optional note and a reason. The
  phrase is `RECORD BOUNTY {KIND} {METRIC} {repo}#{issue}`. Record estimates
  before work starts and actuals as they become known.
- **Decision.** `decide_bounty_evaluation` records whether to `expand`,
  `revise` or `stop` the pilot, with its `integrationStatus`
  (`not_integrated`, `integrating`, `integrated`, `adopted` or `not_adopted`)
  and non-blank reasons. The phrase is
  `DECIDE BOUNTY EVALUATION {repo}#{issue} {DECISION}`.

Each write names the record it supersedes: `expectedEntryId` is the current
entry of the same kind and metric, and `expectedDecisionId` the current
decision, `0` before the first. A stale id is refused, so two operators never
overwrite each other unseen, and an exact retry returns the record it made.
Nothing is edited: the latest entry of each kind and metric, and the latest
decision, are current, and `get_bounty` lists every record with its operator
and reason under `evaluation` and in the audit.

## Pause and roll back

- **Pause.** `set_bounty_claims_paused` stops new holds on one bounty. Holds
  already paid still complete, so no fee is stranded, and existing work and
  reviews continue.
- **Stop a round.** Pause claims, then cancel open and live slots with
  `control_bounty_slot` (`team`). Each paid fee is owed back in full.
- **Change the terms.** Publish a new revision. It applies to new claims only;
  existing claims keep the terms they signed.
- **Code rollback.** Bounty history is append-only, and each bounty migration
  refuses to downgrade while records it would drop exist. Roll forward
  instead.

## Read the report

`get_bounty` includes a `report`: a source-safe account of the bounty for its
evaluation. Beyond what the public board shows, it adds only the current
evaluation values and decision with its reasons, and it counts operator
actions without naming operators, so it can be published as it is.

| Field | What it counts |
|---|---|
| `repo`, `issue`, `title`, `revision`, `spec_digest`, `published_at` | The bounty: its issue, the current revision's title and digest, and when its first revision was published |
| `publish_to_first_accept_seconds` | From that first publication to the first acceptance; `null` until work is accepted |
| `claims` | Each claim's own outcome, what became of its fee, its award and credit, its submissions, and when it was claimed and decided |
| `claims[].accepted` | For accepted work only: the accepted pull request and commit, the seconds from fee confirmation to acceptance, its delivery facts and `integration_status` (`pending`, then the furthest of `merged`, `deployed` and `adopted`), and the receipt of every confirmed transfer of its refund and award |
| `fees_paid` | Entry fees that claimed a slot |
| `fees_refunded`, `fees_refund_owed` | Paid fees refunded for accepted work or a cancellation, confirmed or still owed |
| `fees_retained` | Paid fees retained once a rejected, expired or withdrawn slot was reopened |
| `fees_undecided` | Paid fees whose outcome is not final yet |
| `payments_returned`, `payments_return_owed` | Payments that never won a slot: late, lost, changed identity or duplicate |
| `awarded`, `awards_paid`, `reward_pool_alpha_rao` | Award spend against the pool, recorded and confirmed |
| `transfers` | Transfers by state, including failed and reconciled ones |
| `decisions`, `median_review_seconds`, `reviews_overdue` | Review outcomes and reviewer turnaround |
| `appeals_filed`, `appeals_upheld`, `appeals_denied` | Appeals and their outcomes |
| `facts` | Recorded merges, deployments and adoptions |
| `operator_actions` | Operator records in the audit |
| `evaluation` | The current `estimates` and `actuals` of each metric, kept apart and `null` where none is recorded, and the current `decision`, `integration_status`, `reasons` and `decided_at`, all `null` before the first decision. It names no operator and leaves out entries' notes and reasons |

`checks` verifies the settlement invariants from the recorded history: awards
stay within the pool and go only to accepted work, each paid fee has at most one
outcome, accepted work is owed its fee back, an obligation has at most one live
transfer, every confirmed transfer has a receipt, and rehearsal money is
simulated while live money is not. Every check should pass. If one fails, pause
claims and investigate before dispatching anything.

## Rehearse

Rehearse the whole flow without funds before a funded pilot (#2944), in two
rounds. The first covers publication, three claims, a timely submission, a
missed deadline, a rejection, a shared award, the winners' fee refunds, a
system-fault cancellation refund, appeals upheld and denied, and transfer
reconciliation, including a failed and an unknown outcome. The second covers a
team cancellation of a paid slot, a withdrawal whose fee is retained when the
slot is reopened, a fee paid after its hold lapsed, a duplicate payment, an
award above the payout policy's limit until a new revision raises it, an
award's payout held while its hotkey has another owner, a transfer that
expires and one reconciled as failed, each paid in a fresh approval round.
Then read each report and confirm every check passes.

Platform's test suite runs exactly these rehearsals on every change, through
the real API with real signatures:

```bash
cd apps/platform
uv run pytest ditto/tests/api_server/endpoints/test_bounty_rehearsal.py
```

To rehearse on a staging Platform, follow the same steps with test contributors
(each with a linked GitHub account and a signed CLA) and the Backroom tools
above. Rehearsal bounties ask the simulated executor for each transfer outcome
with `rehearsalOutcome`. The simulated fee verifier accepts one receipt per
claim, so a duplicate payment is rehearsed only in the test suite, which
swaps in a verifier that accepts a second.

## Funded pilot (not authorized)

No funded pilot is authorized, and the code cannot run one. Publication
refuses a `live` revision ("live funding needs separate operator approval"),
a claim on one is refused ("live bounty funding is not enabled"), the only
fee verifier refuses live claims, and the only transfer executor is simulated,
so dispatching a live obligation through it is refused. Nothing dispatches a
transfer on its own: only an operator's `dispatch_bounty_obligation` does, and
`test_bounty_funding_guards.py` fails if any other Platform module or script
names the dispatch or the transfer executor. This section is the checklist
for the separate decision (#2944). The values in angle brackets are set by
that authorization, not here.

### Capped configuration

| Cap | Where it is set | Value |
|---|---|---|
| Entry fee | `entry_fee_alpha_rao` in each published revision's signed terms | `<ENTRY_FEE_ALPHA_RAO>` |
| Reward pool | `reward_pool_alpha_rao` in each published revision. Each award keeps the bounty's awards within the pool of the revision its claim signed | `<REWARD_POOL_ALPHA_RAO>` per bounty |
| Bounties | Operators publish them; the code has no limit | at most `<PILOT_BOUNTIES>` |
| Slots | Three per round, fixed by the code (`attempts_per_round` must be 3); each bounty has one round, and every reopen admits one more paid claim | at most `<PILOT_REOPENS>` reopens per bounty |
| Single-approver limit | `single_approver_max_alpha_rao` of the payout policy (`set_bounty_payout_policy`) | `<SINGLE_APPROVER_MAX_ALPHA_RAO>` |
| Dual-approver limit | `dual_approver_max_alpha_rao` of the payout policy. Nobody can approve a larger amount (`over_policy_limit`) | `<DUAL_APPROVER_MAX_ALPHA_RAO>` |
| Fee escrow | `fee_escrow_coldkey` and `fee_escrow_hotkey` in each revision | `<FEE_ESCROW_COLDKEY>`, `<FEE_ESCROW_HOTKEY>` |
| Award funding | Not modeled by Platform: the account awards are paid from | `<AWARD_FUNDING_SOURCE>` |

### Activation authority

Live funding starts only through a reviewed code change, released and
deployed like any other, made after all of these exist:

1. **A recorded operator authorization**, separate from this runbook, that
   names the funding source for awards, the fee escrow keys, every cap above,
   and who may activate the pilot, approved as
   [maintenance-treasury.md](maintenance-treasury.md#decisions-that-need-approval)
   requires.
2. **A live fee verifier.** It does not exist. It must accept only a
   finalized transfer of the claim's exact fee from its payee coldkey to the
   revision's escrow, behind the `BountyFeeVerifier` interface.
3. **A live transfer executor.** It does not exist. It must implement
   `BountyTransferExecutor` with `simulated = False`, report `uncertain` until
   an outcome is final, and pay refunds and awards from the accounts the
   authorization names.
4. **Proof-backed operator identity.** Every bounty operator write must
   require `require_operator_proof` (a short-lived HMAC from the signed-in
   Backroom session over its actor, method, path and body), as the L2 report
   canary endpoints do, so the distinct-approver and recusal checks name real
   operators rather than the asserted `X-Admin-Actor`.
5. **The treasury execution guard (#2331).** `execute_one_leg` in
   `ditto/treasury/execution.py` blocks every live treasury dispatch until a
   reviewed change binds finalized receipts, the current policy and burn,
   authenticated reviewers and a signer-side quote. A pilot paid from treasury
   funds goes through that guard once it is lifted, never around it.

The same change lifts the `live` refusals in `publication_refusals` and the
claim's terms check.

### Stop and roll back

1. **Pause new holds** on every pilot bounty with `set_bounty_claims_paused`.
2. **Let held slots end.** A held slot cannot be cancelled. Its fee may still
   be confirmed, which claims the slot; otherwise the hold lapses after the
   revision's `hold_minutes`, and a fee that arrives later is owed back in
   full (`hold_expired`).
3. **Cancel the remaining live slots** (claimed, submitted or rework) with
   `control_bounty_slot` and `kind: team`. Each paid fee is owed back in full
   (`cancelled`). Accepted work keeps its refund and any award; a rejected,
   expired or withdrawn fee stays undecided until its slot is reopened.
4. **Stop approving and dispatching.** Nothing dispatches by itself, so an
   approved round waits for an operator.
5. **Reconcile every uncertain or dispatched transfer** with
   `reconcile_bounty_transfer` before anything else. Never send it again.
6. **Roll code forward**, never back: bounty history is append-only and the
   bounty migrations refuse to downgrade over recorded rows.

### Reconciliation checks

Read `get_bounty` for each pilot bounty and `get_bounty_queue` with
`queue=settlement`:

- Every report check passes (`checks`, see [Read the report](#read-the-report)).
- Paid fees add up: `fees_paid` equals `fees_refunded` + `fees_refund_owed` +
  `fees_retained` + `fees_undecided`, in count and in alpha rao.
- Every receipt is real. Each of a claim's fee `receipts` is a finalized
  transfer of its exact fee from its payee to its escrow, and each confirmed
  transfer carries the receipt of the refund or award it paid, found on chain.
- Nothing is in flight: no settlement item is `dispatched` or `uncertain`.
- Awards stay funded: `awarded` minus `awards_paid` is what the award funding
  source still owes, and it must hold at least that.
- **Escrow balance.** With refunds paid out of the fee escrow, the balance
  in alpha rao that each escrow should hold from a bounty whose revisions all
  name it is

  ```text
  fees_retained + fees_refund_owed + fees_undecided + payments_return_owed
  ```

  that is, every fee receipt recorded (`fees_paid` + `payments_returned` +
  `payments_return_owed`) minus every confirmed fee refund (`fees_refunded` +
  `payments_returned`). Sum it over every bounty that names the same escrow,
  and stop and investigate on any difference from the chain balance. The
  escrow must always hold at least `fees_refund_owed` + `payments_return_owed`,
  the fees owed back.
