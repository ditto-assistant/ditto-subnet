# SN118 bounty claims, slots, and contributor identity

Status: **v1 implemented, in rehearsal funding** (#2942). This is the claim
contract for build bounties. It adapts the reservation proposal for #2045 to
paid slots: an alpha entry fee, an alpha reward pool, three slots per round, a
five-day delivery deadline by default and no renewals. The parts of the
proposal that v1 does not implement are listed under [Deferred](#deferred).
The step-by-step walkthrough for contributors is the
[contributor guide](bounty-guide.md).

A claim does one job: it binds a paid slot on a bounty to a verifiable subnet
identity and a payment destination, under terms that were public before the
work began. A claim does not approve work, promise an award, or move funds.

In v1 every bounty runs in **rehearsal** funding. Entry fees are verified by a
simulated verifier and payments by a simulated executor, behind the interfaces
the live adapters will use. Every rehearsal receipt, refund, award and transfer
is stored and shown as simulated. Publishing a live-funded revision is refused.

## Principles

1. **The hotkey decides who you are; GitHub is only a label.** Every claim
   action is signed by the claimant's hotkey or by the claim's payee coldkey. A
   GitHub login is recorded for display and for linking pull requests, but it
   never chooses or changes a payment destination.
2. **Payment goes to the payee recorded on the claim.** At claim time the payee
   coldkey must be the finalized chain owner of the hotkey, and it is signed
   into the claim. A later owner of the hotkey cannot act on the claim or
   redirect its payments. Because a new owner may mean the payee coldkey was
   lost or compromised, an award's payout holds while the hotkey's finalized
   owner is not the payee of record. v1 has no way to change a claim's payee.
3. **Every signature is used once, for one purpose.** Each signature binds a
   domain tag, the netuid, the claim or the bounty revision and its digest, the
   contributor, a nonce and the time it was issued.
4. **The rules are fixed before work starts.** The fee, pool, slot count, hold,
   delivery and review periods are part of the signed bounty revision. A new
   revision applies to new claims only.
5. **The Platform API is the only authority.** Claims and their history live in
   Platform's append-only tables. GitHub comments and labels never create,
   change or approve a claim.

These rules reuse the signed-action pattern of owner links
(`apps/platform/ditto/api_server/attestation.py`), the verified GitHub link and
the contributor licence agreement
(`apps/platform/ditto/api_server/bounty_identity.py`).

## The bounty revision

A bounty is identified by `{owner}/{repo}#{issue}` in its one lowercase
spelling, for example `ditto-assistant/ditto-subnet#2942`. An operator
publishes each **revision** through Backroom, with an exact typed
confirmation. Revisions are numbered from 1 and append-only. The
`spec_digest` is the SHA-256 of the canonical JSON (sorted keys, no
whitespace) of `{"domain": "ditto-bounty-spec:v1", "repo", "issue",
"revision", "terms"}`, where `terms` holds exactly these fields:

| Field | Meaning |
|---|---|
| `title`, `scope`, `public_interfaces`, `acceptance_evidence` | What is in scope, what it may change, and the objective evidence the reviewer checks |
| `dependencies` | Issues that must land first, as `owner/repo#issue` |
| `reviewer_login` | The reviewer's public GitHub login |
| `entry_fee_alpha_rao`, `reward_pool_alpha_rao` | The entry fee and the reward pool, in alpha rao (1 alpha = 10^9 rao), at most 2^53 - 1 so every JSON client reads them exactly |
| `attempts_per_round` | Paid slots per round. v1 requires 3 |
| `hold_minutes` | How long an unpaid hold keeps its slot (default 30) |
| `delivery_days` | Delivery deadline, counted from fee confirmation (default 5) |
| `review_days` | Review target, counted from each submission (default 7) |
| `funding_mode` | `rehearsal` or `live`. v1 publishes rehearsal only |
| `fee_escrow_coldkey`, `fee_escrow_hotkey` | The dedicated escrow that receives this revision's entry fees |

A new field needs a new domain version. Revision 1 opens round 1 with three
open slots. A later revision changes the terms for new claims and never touches
existing slots or claims.

## Identity

- **Claimant hotkey.** Any hotkey whose owner resolves at a finalized block. An
  SN118 UID is not required, so outside contributors don't need to buy a
  registration.
- **Payee coldkey.** It must equal `Owner(hotkey)` at a finalized block when
  the claim is held and again when its fee is confirmed. It is signed into the
  claim and is the **payee of record** for every refund and award.
- **GitHub identity.** The claim's `github_login` must be the hotkey's active
  verified link (`ditto link-github`; see [GitHub links](GITHUB-LINKS.md)).
  Platform records the link and its numeric GitHub user id, and stores the
  login exactly as signed.
- **Contributor licence agreement.** Every claim needs a current signature,
  whatever the upload gate's mode.
- **One live slot per person.** A GitHub account and a payee coldkey can each
  hold one live slot per bounty.
- **Teams.** v1 claims are solo. The claim still signs a `team_digest`, the
  digest of a one-member team, so team claims can be added without a new
  signature domain.

If the payee, link or CLA signature behind a hold changes before its fee is
confirmed, the fee is refunded and the slot is released.

## Signed actions

Each action signs the exact UTF-8 bytes of its fields joined by `:`.
`issued_at` is UTC ISO-8601 with microseconds, `nonce` is a UUID, `key_kind` is
`hotkey` or `coldkey`, and `signer` is the SS58 address that signed. Platform
never parses a message it receives: it rebuilds the bytes from the request's
fields and verifies the signature against them.

| Action | Domain | Fields after the domain |
|---|---|---|
| Claim | `ditto-bounty-claim:v1` | `netuid:repo:issue:revision:spec_digest:hotkey:payee_coldkey:github_login:team_digest:nonce:issued_at:key_kind:signer` |
| Submit | `ditto-bounty-submit:v1` | `netuid:claim_id:hotkey:repo:pr_number:head_sha:nonce:issued_at:key_kind:signer` |
| Withdraw | `ditto-bounty-withdraw:v1` | `netuid:claim_id:hotkey:nonce:issued_at:key_kind:signer` |
| Appeal | `ditto-bounty-appeal:v1` | `netuid:claim_id:hotkey:subject_event_id:reason_digest:nonce:issued_at:key_kind:signer` |

Notes:

- `team_digest` is the SHA-256 of canonical JSON
  `{"payee_coldkey": ..., "members": [{"hotkey": ..., "github_login": ...}]}`,
  with members sorted by hotkey.
- The claim signs no expiry. The delivery deadline starts when the fee is
  confirmed, and its length is part of `spec_digest`.
- `claim_id` is the server-issued UUID returned by the claim. Follow-up actions
  bind it, so a signature for one claim can't be used on another.
- A coldkey proof must come from the claim's payee coldkey; a hotkey proof from
  the claim's hotkey. The hotkey's current owner never qualifies by itself.
- `head_sha` binds the exact commit submitted for review.
- `subject_event_id` is the public event being appealed, and `reason_digest` is
  the SHA-256 of the private appeal statement.

### Replay protection

| Guard | Rule |
|---|---|
| Domain tag and version | A signature can't be replayed into the upload, owner-link, GitHub-link or name-claim lanes |
| `netuid` | A signature from another deployment is refused |
| Repository, issue, revision, `spec_digest` | A claim can't move to another bounty or to changed terms. A claim for a superseded revision is refused and names the current one |
| `claim_id` | Follow-up actions can't move to another claim |
| `nonce` | Unique across every bounty action, in one table. An exact retry returns the original result; any other reuse gets `409` |
| `issued_at` | At most 5 minutes in the future and 24 hours old, as for owner links |

## Slots

| From | What happens | To |
|---|---|---|
| open | A verified claim holds it | held |
| held | The fee is confirmed before the hold ends | claimed |
| held | The hold ends unpaid | open |
| claimed or rework | The claimant submits a commit | submitted |
| submitted | The reviewer accepts | accepted |
| submitted | The reviewer requests changes | rework |
| submitted | The reviewer rejects | rejected |
| claimed or rework | The claimant withdraws | withdrawn |
| claimed or rework | The delivery or rework deadline passes | expired |
| rejected or expired | An appeal is upheld | claimed |
| open, claimed, submitted or rework | An operator cancels | cancelled |
| rejected, expired, withdrawn or cancelled | An operator reopens | open |

- **Hold.** A verified claim holds the lowest open slot of the current round
  for `hold_minutes`. The hold reserves the slot only for its fee.
- **Claimed.** A fee receipt for the exact amount, paid by the payee, claims
  the held slot. The delivery deadline is `delivery_days` from that moment. A
  receipt pays at most one claim.
- **Refunds, never forfeits.** A fee that cannot win its slot is recorded with
  a full refund obligation: the hold had lapsed (`hold_expired`), the slot had
  left the claim (`slot_lost`), the payee, link or CLA signature had changed
  (`identity_changed`), or the claim was already paid (`duplicate_payment`).
- **Deadlines.** A deadline is reached at its instant. Each write first applies
  the bounty's due deadlines in its own transaction, so a refused request
  can't undo them, and a sweeper applies them to bounties nobody touches. Reads
  show the same outcome before any write records it.
- **Pause.** Operators can pause new holds on a bounty. Existing holds,
  payments and work are unaffected.
- **Reopening.** Operators reopen closed slots one at a time; nothing reopens
  automatically. Reopening is refused while the slot's claim has a pending
  appeal. A reopened claim keeps its own outcome on its record.

### Submission review

- **Submit.** While the slot is claimed or in rework, before its deadline, the
  claimant signs the pull request number and exact head commit.
- **Decide.** Only the bounty's assigned reviewer decides, on one exact
  submission: `accept` (with public credit), `changes_requested` (with a rework
  deadline), or `reject`. Each decision has a public reason.
- **Overdue review.** A submission past its review target stays submitted,
  since the contributor is not at fault. It is reported overdue once per due
  date, and the board and Backroom flag it. Reassigning the reviewer gives
  overdue reviews a new review window.

### Appeals

A claimant can appeal a rejection or a missed deadline, once per decision,
while the slot still shows that outcome. The statement is private; only its
digest is public. An operator decides, and a rejection is never judged by the
reviewer who rejected the work. Upholding restores the claim to claimed with a
new delivery deadline; denying leaves the outcome in place.

## Fees and awards

Every confirmed entry fee ends in exactly one disposition:

| Outcome | Disposition |
|---|---|
| Accepted | `fee_refund`, recorded when the work is accepted |
| Cancelled by an operator (`team` or `system_fault`) | `fee_refund`, recorded at cancellation |
| Rejected, expired or withdrawn | `fee_retained`, recorded when the slot is reopened, which is when the outcome becomes final |

An operator can record one **award** per accepted claim, with public
attribution, from the bounty's reward pool. Awards across claims never exceed
the pool. Each refund and award is an **obligation**. Paying one needs as many
distinct approvers as the payout policy sets for its amount. Each approval
binds the obligation, its payee, its amount, the policy revision and the
approval round, and whoever decided the award or accepted the work can't
approve it. An award can't be approved or dispatched while the claim hotkey's
finalized owner is not the payee of record; a fee refund returns to the
coldkey that paid it and is never held. An approved round is dispatched once
and reconciled against its receipt; a failed or expired transfer needs fresh
approvals. Merge, deployment and adoption facts are recorded publicly and
never gate payment.

## Public visibility

The public board (`/api/v1/public/bounties`) and claim records
(`/api/v1/bounties/claims/{claim_id}`) show:

- the terms, revisions, rounds and every slot with its effective status;
- each claimant's GitHub login, hotkey and payee coldkey, and the revision
  they signed;
- holds, deadlines, receipts and refunds;
- submissions with their pull request and commit, decisions with their public
  reasons and credit, and overdue reviews;
- appeal digests and outcomes;
- obligations, approval counts, transfers and delivery facts;
- a public event trail with ids, which appeals cite.

Operator identities, appeal statements, and the reasons for pausing,
cancelling, reopening and awarding are private. Backroom shows them to
operators.

## Threat model

| Threat | Mitigation |
|---|---|
| GitHub account takeover redirects payment | The payee of record comes from the signed claim. GitHub can't sign any bounty action |
| Replay of a captured claim or follow-up action | Domain, netuid, revision and digest, claim id, a shared single-use nonce, and the 24-hour freshness window |
| Terms changed after work began | The claim binds `spec_digest`. New revisions apply to new claims only |
| Squatting on slots | A short unpaid hold, a fee to claim, a fixed delivery deadline with no renewals, and one live slot per GitHub account and payee |
| Placeholder pull requests | A submission names an exact commit, and the reviewer decides on that commit within the review target |
| A pull request is rewritten after review | `submit` binds `head_sha`. New commits need a new signed submission |
| A hotkey leaks, is sold, or changes owner | The new holder can make new claims but can't act on or redirect existing ones. A changed owner before fee confirmation refunds the fee, and afterwards an award's payout holds until the owner is the payee again |
| A fee is lost to a race or a late payment | Every fee that can't win its slot is owed back in full |
| An insider pays themselves | Approvals need distinct operators by amount, and the award decider and accepting reviewer can't approve |

## Deferred

v1 deliberately leaves these parts of the #2045 proposal for later work:

- **Renewals and reservation expiry dates.** Slots have one delivery deadline
  from fee confirmation, and rework deadlines set by the reviewer.
- **Handoffs.** A claim can't move to another hotkey. A contributor who can't
  finish withdraws, and the slot can be reopened.
- **Team claims.** The claim message already carries `team_digest`; member
  signatures and payee lines are not implemented.
- **Payee rebinds and reassignment disputes.** A claim's payee is final, so a
  held award waits until the hotkey's owner is the payee of record again.
- **Revocations and stale-submission release.** Operators cancel slots
  instead, which refunds the fee; Platform does not watch pull requests.
- **A per-payee limit across bounties and a cooldown after expiry.**
- **Further rounds.** Each bounty has one round; closed slots are reopened
  individually. A new round will need a new revision.
- **Live funding.** Live fee verification and transfers need separate approval
  under the treasury contract in [maintenance-treasury.md](maintenance-treasury.md).
