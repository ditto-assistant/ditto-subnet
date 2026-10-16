# Build bounties: operator runbook

Operators run build bounties through Backroom's MCP tools (see
[Backroom MCP](../apps/backroom/docs/mcp.md#build-bounties)). Every write names
its actor, takes an exact typed confirmation, and is recorded in the bounty's
private audit. The contributor side is in [bounty-guide.md](bounty-guide.md);
the rules are in [bounty-claims.md](bounty-claims.md).

v1 is rehearsal only: fees and transfers are simulated and no funds move.
Live funding needs separate operator authorization (#2944).

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
  obligations that still need an operator.
- **Review.** The reviewer in force calls `review_bounty_submission` on the
  exact submission: accept with attribution, request changes with a rework
  deadline, or reject. Every decision has a public reason.
- **Overdue reviews.** `reassign_bounty_reviewer` names a new reviewer and gives
  overdue reviews one more window.
- **Appeals.** `get_bounty` shows each appeal's private statement.
  `decide_bounty_appeal` upholds (restoring the claim with a new delivery
  deadline) or denies. A rejection's reviewer can't decide its appeal.
- **Slots.** `control_bounty_slot` cancels a slot (refunding a paid fee in full,
  as `team` or `system_fault`) or reopens a closed one for a new claim
  (retaining a rejected, expired or withdrawn fee). Reopening waits for a
  pending appeal.

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
evaluation. It names only what the public board shows and counts operator
actions without naming operators, so it can be published as it is.

| Field | What it counts |
|---|---|
| `claims` | Each claim's own outcome, what became of its fee, its award and credit, its submissions, and when it was claimed and decided |
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

`checks` verifies the settlement invariants from the recorded history: awards
stay within the pool and go only to accepted work, each paid fee has at most one
outcome, accepted work is owed its fee back, an obligation has at most one live
transfer, every confirmed transfer has a receipt, and rehearsal money is
simulated while live money is not. Every check should pass. If one fails, pause
claims and investigate before dispatching anything.

## Rehearse

Rehearse the whole flow without funds before a funded pilot (#2944):
publication, three claims, a timely submission, a missed deadline, a rejection,
a shared award, the winners' fee refunds, a cancellation refund, an appeal, and
transfer reconciliation, including a failed and an unknown outcome. Then read
the report and confirm every check passes.

Platform's test suite runs exactly this rehearsal on every change, through the
real API with real signatures:

```bash
cd apps/platform
uv run pytest ditto/tests/api_server/endpoints/test_bounty_rehearsal.py
```

To rehearse on a staging Platform, follow the same steps with test contributors
(each with a linked GitHub account and a signed CLA) and the Backroom tools
above. Rehearsal bounties ask the simulated executor for each transfer outcome
with `rehearsalOutcome`.
