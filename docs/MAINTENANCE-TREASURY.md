# SN118 maintenance treasury and governance contract

This document is the governance contract for the SN118 maintenance treasury:
where its money comes from, who may hold and spend it, what work it pays for,
and how every movement is recorded and checked against the chain. It resolves
[#2044](https://github.com/ditto-assistant/ditto-subnet/issues/2044) under the
[#2054](https://github.com/ditto-assistant/ditto-subnet/issues/2054) epic.
Claiming ([#2045](https://github.com/ditto-assistant/ditto-subnet/issues/2045)),
acceptance and payout proof
([#2046](https://github.com/ditto-assistant/ditto-subnet/issues/2046)), and the
public board ([#2047](https://github.com/ditto-assistant/ditto-subnet/issues/2047))
implement it and must not contradict it.

**Status: specification.** No treasury account exists and no funds have moved.
Merging this document approves the rules below. It does not activate anything:
activation follows the phases in [Rollout](#rollout), and each phase needs its
own signed policy revision.

## Summary

| Question | Answer |
|---|---|
| Where does the money come from? | A governed share of submission-fee revenue. Not miner emission. |
| What is paid? | SN118 alpha, fixed per award when the award is approved. |
| How big can it get? | A payout plus all payouts in the 30 days before it may not exceed 5% of the alpha SN118 paid to miners in those 30 days, nor the activation cap. |
| Who holds it? | An on-chain 2-of-3 multisig of named custodians, separate from the owner and deposit keys. |
| Who decides? | Named keys. Reviewers judge work, custodians execute, and every decision is a signed ledger entry. |
| What can move funds? | Only a signed approval that traces to accepted work at an exact commit or evidence hash. Never a GitHub issue, label, comment, merge, or Discord message. |
| How is it checked? | A public, append-only, hash-chained ledger, reconciled weekly against the chain. A mismatch, or any data that cannot be read, closes a spending gate that every signer checks independently. |
| How does it start? | Shadow first (full ledger, zero funds), then a small reversible cap. |

## Principles

1. **No promise moves funds.** A bounty issue, label, comment, Discord message,
   pull-request merge, or deployment is evidence. Only a signed approval under
   the policy revision in effect can commit funds, and only a chain transfer
   that matches that approval counts as payment.
2. **States stay separate.** Claimed, submitted, accepted, merged, verified,
   approved, and paid are distinct states with distinct evidence. No state
   implies a later one.
3. **Everything is attributable.** Every inflow, conversion, reservation (a
   claim on a bounty or an earmark of funds), commitment, payout, release,
   cancellation, pause, custody change, and policy change is a ledger entry.
   It is either signed by the key that authorized it, or carries the chain
   receipt that proves it.
4. **No unfunded promises.** The board never advertises more reward than the
   treasury holds in alpha.
5. **Failures stop spending, not work.** Missing data, a failed reconciliation,
   or an unreadable ledger closes the [spending gate](#spending-gate). The gate
   fails closed and every signer evaluates it independently, so stopping
   payouts never waits for a signature or a writable ledger. Contributors keep
   working, and approved awards stay owed.
6. **Platform verifies, people sign.** The Platform records and verifies. It
   never holds a key that can move treasury funds or authorize a decision.

## Roles

| Role | Holds | May | May not |
|---|---|---|---|
| **Owner** | The SN118 owner coldkey, and the owner reviewer key it enrolls | Execute sweeps from the deposit address, co-sign policy revisions, pause, approve owner-tier awards with the [owner reviewer key](#approval) | Spend treasury funds or change policy alone; sign approvals with the owner coldkey |
| **Custodian** (3) | One signatory coldkey of the treasury multisig | Execute payouts, conversions, restakes, and rotations that match signed ledger entries; pause; co-sign policy revisions | Sign an extrinsic without a matching entry; approve work without a second, non-custodian reviewer |
| **Reviewer** (at least 3) | An sr25519 key in the policy's reviewer roster | Publish bounties, accept or reject work, approve awards within their tier, revoke claims and cancel bounties with a reason, decide appeals they took no part in | Act on a claim in which they have a conflict (see [Conflicts](#conflicts)) |
| **Contributor** | A key that signs claims | Claim, submit, renew, hand off, withdraw, appeal | Redirect payment through a GitHub account or any unsigned channel |

One person may hold several roles, but never decides, executes, or appeals
anything about their own claim. The names and public keys of custodians and
reviewers are published in the policy revision that activates them.

## Authority

Every decision is a ledger entry signed by the keys below. The Platform's admin
API token is shared, and the actor on an admin request is self-declared. So the
token may carry a signed entry to the Platform but never authorizes one.

The [spending gate](#spending-gate) is not a decision. Nobody signs it closed:
it closes on its own when a condition fails or cannot be checked. Signatures
are needed only to record what happened and to reopen it.

| Action | Signed by | Takes effect |
|---|---|---|
| Publish a bounty, set its reward range, earmark its maximum | One reviewer | Immediately |
| Accept or reject submitted work | One reviewer | Immediately |
| Approve an award | Reviewers for its [tier](#approval) | Immediately |
| Revoke a claim or cancel a bounty | One reviewer, with a reason | Immediately |
| Decide an appeal | A reviewer who took no part in the decision | Immediately |
| Pause | The owner or any one custodian | Immediately, from whatever channel it is first seen; appended to the ledger once it accepts writes ([Manual pause](#manual-pause)) |
| Close the spending gate on a failed or unreadable condition | Nobody: the [spending gate](#spending-gate) closes itself | Immediately |
| Record an incident or its resolution | The owner or any one custodian | When appended. Neither reopens the gate. |
| Unpause | Two different people among the owner and the custodians | Only when every gate condition holds; after a trip or an incident, only through [Unpause after repair](#unpause-after-repair) |
| Protective change: lower the cap or fee share, return to an earlier phase, remove a reviewer, replace a staking hotkey that breached policy | The owner and one custodian who is not the owner | Immediately. It never affects existing claims, earmarks, or commitments. |
| Emergency custody rotation | Two custodians | Immediately |
| Any other policy change, including adopting a new revision of this document | The owner and one custodian who is not the owner | After `policy_notice_days` |

## Funding source

### Decision

The treasury is funded from **submission-fee revenue**. Miners already pay a
TAO fee per evaluated submission. Each payment is verified on chain and stored
in `evaluation_payments` with its amount, payer coldkey, destination address,
block hash, and extrinsic index
([`apps/platform/ditto/db/models.py`](../apps/platform/ditto/db/models.py),
`EvaluationPayment`). The owner sets the deposit address that receives these
fees through a revisioned control with an expected revision, a typed
confirmation, a reason, and an actor
([`admin_submission_deposit_address.py`](../apps/platform/ditto/api_server/endpoints/admin_submission_deposit_address.py)).

The treasury receives `fee_share_bps` of settled fees through **sweeps**:

- Fees are grouped into consecutive windows of `sweep_window_days`, by payment
  block number. **Settled fees** are the `amount_rao` of every
  `evaluation_payments` row in the window, including unassigned credits.
- For each closed window and each deposit address that received fees in it,
  the ledger holds exactly one sweep entry. Its amount is
  `floor(settled × fee_share_bps / 10000)`. A zero amount is recorded without
  a transfer.
- A non-zero sweep is one TAO transfer from that deposit address to the
  treasury account, executed within `sweep_deadline_days` of the window
  closing. A missing or short sweep fails reconciliation.
- The ledger publishes the fee rows each sweep covers: block hash, extrinsic
  index, amount, and destination address. Anyone can then recompute every
  sweep. Fee rows are admin-only today
  (`apps/platform/ditto/api_server/endpoints/admin_miner_fees.py`), so
  publishing them is part of #2046.

This redirects income the owner currently receives. It is recorded as the
owner's contribution, and it costs miners nothing: miner emission is
untouched.

### Why not miner emission

The epic's first framing was "5% of SN118 emissions". Routing emission to a
treasury is not available in the current system, and building it would add
more risk than it removes:

- The only destination for emission not paid to miners is the compiled burn
  hotkey, SN118 UID 0 (`FINNEY_BURN_HOTKEY` in
  [`ditto/validator/config.py`](../ditto/validator/config.py)). Subtensor burns
  incentive sent there (`apply_miner_emission_cap` in
  [`ditto/validator/weights.py`](../ditto/validator/weights.py)). Raising the
  platform's `burn_share` would destroy the 5%, not fund anything.
- A second destination needs a validator release, a separately owned coldkey
  (hotkeys under the owner coldkey are burned too), a registered UID, and a
  fleet activation gate: a partial fleet would get the treasury weight clipped
  by consensus. It also needs Platform changes, because
  `classify_vector_against_pins` (`apps/platform/ditto/api_server/ledger_pin.py`)
  and the source-emission collector assume a single non-miner recipient.
- Letting the Platform serve an arbitrary payee would break the trust argument
  that the burn dial "is not new authority" (`resolve_miner_emission_share` in
  `ditto/validator/weights.py`): the Platform could then direct emission to any
  hotkey.
- A change on that path takes two to three tempos to reach chain. It lands at
  the next ledger pin ([`docs/VALIDATOR.md`](VALIDATOR.md)), validators commit
  late in that epoch, and a commit made in epoch N enters the fold at the end of
  N+2 (`Dockerfile.pylon`). A pause would take just as long.
- It would cut what miners receive.

A future policy revision may add an emission-funded source. It must go through
this document's change process and cover every point above.

### Size: the 5% ceiling

The epic's 5% becomes a hard, chain-checkable ceiling. A payout may be executed
only if it, plus every payout in the 30 days before its block, stays within
both:

- `emission_ceiling_bps` (500, meaning 5%) of the alpha SN118 emitted to its
  miners in those same 30 days; and
- the `activation_cap_alpha` in effect.

Miner emission is the sum of the `IncentiveAlphaEmittedToMiners` events for
netuid 118 in finalized blocks. The Platform already decodes that event
(`apps/platform/ditto/chain/source_emission_verifier.py`). If the total cannot
be read, the payout waits. This is not a pause.

The ceiling is measured against the miner pool, not total subnet emission. That
is the smaller of the two readings of "5% of emissions", on purpose: it keeps
maintenance spending small next to what miners earn. Both sides of the
comparison are alpha amounts, so the ceiling needs no price oracle.

## Denomination and conversion

- **Awards are paid in SN118 alpha.** Contributors end up holding the token
  whose value their work supports.
- **An award's amount is fixed in alpha when it is approved.** It never changes
  with the TAO or USD price afterwards. TAO and USD values in the ledger are
  informational, recorded at the time with their source, and never override an
  approved alpha amount.
- **Conversion is batched and recorded.** The treasury converts swept TAO to
  alpha by staking it on SN118 (`SubtensorModule.add_stake`) in batches it
  chooses, ahead of need. Each conversion is a ledger entry with TAO in, alpha
  out, and the receipt. Payouts draw on converted alpha, so no payout is timed
  against the market. A small TAO reserve stays unconverted to pay
  transaction fees.
- **Only alpha counts as available.** Available balance is held alpha minus
  commitments minus open earmarks. Unconverted TAO does not count, so the board
  can only advertise converted funds and no price is needed.
- **Payouts move stake ownership.** A payout is a
  `SubtensorModule.transfer_stake` to the payee coldkey on SN118, which leaves
  the stake on the staking hotkey. Executed through the multisig, it arrives
  wrapped in `Multisig.as_multi`. The payout verifier pins the exact call and
  event shapes from runtime metadata when it is built.

## Custody

### Account

- The treasury account is an on-chain **2-of-3 multisig** (Subtensor's
  `Multisig` pallet) of three custodian coldkeys. The chain enforces the
  threshold. Platform code does not.
- It is separate from the owner coldkey, the submission deposit address, and
  every validator or miner key.
- Custodian keys are coldkeys kept offline, controlled by three different
  people, independently backed up, and never stored on Platform, Backroom, CI,
  or any shared host. Custodians sign from these keys directly; the treasury
  uses no proxy accounts.
- The multisig address, the three signatory addresses, and the threshold are
  published in the activation policy revision and shown on the public ledger.

### Staking hotkey

- Treasury alpha is staked on one SN118 validator hotkey named in the policy,
  together with its owner and its take when chosen. It must not be controlled
  by a custodian or reviewer.
- The policy sets `max_staking_take_bps`. If the hotkey's take rises above it,
  or the hotkey loses its validator permit or registration, a protective policy
  revision names a replacement. Custodians then move the stake
  (`SubtensorModule.move_stake`) in a recorded `restake` entry.
- Contributors receive their payout as stake on this hotkey and may move or
  unstake it freely.

### Spending authority

Custody is not spending authority. A custodian signs an extrinsic only when:

1. the ledger holds a matching signed entry: an approval for a payout, a sweep
   for a conversion, or a policy revision for a restake or rotation;
2. the destination and amount equal that entry exactly;
3. the [spending gate](#spending-gate), evaluated by the custodian's own
   signing tool, is open, unless the transfer is a custody change; and
4. for a payout, the [5% ceiling](#size-the-5-ceiling) and the activation cap
   hold.

A second custodian independently checks the same conditions before
co-signing. A movement the ledger cannot explain is an incident (see
[Incidents](#incidents)).

An emergency custody rotation may execute while the ledger cannot accept
writes, because a compromised key must be removable during exactly that kind
of failure. The two custodians sign its `custody_change` entry when they sign
the transfer, and it is appended as soon as the ledger accepts writes, so
reconciliation can explain the movement.

### Rotation and recovery

- **Planned rotation.** Create the new multisig, record a custody-change policy
  revision naming it, move the full balance (staked alpha and TAO) in recorded
  transfers, then publish that the old address is retired. The old address is
  never reused.
- **One key lost or suspected compromised.** The remaining two custodians pause
  the treasury, rotate to a new multisig that excludes the affected key, and
  record why. A 2-of-3 threshold tolerates one lost key.
- **Two keys lost.** Funds in the account are unrecoverable by design. The loss
  is recorded, open earmarks are released, and approved awards that can no
  longer be paid are recorded as `unpaid`. The owner decides whether to start
  a new treasury under a new custody revision.

## Eligible work

Work is eligible only through a **bounty published before the work starts**,
with scope, acceptance evidence, reward range, reviewer, dependencies, and
expiry (see #2047). Eligible categories:

| Category | Examples |
|---|---|
| Security | A fixed, privately reported vulnerability; hardening with a demonstrated threat |
| Benchmark | DittoBench correctness, scoring fixes, datagen quality, adapter maintenance |
| Screener | Screening reliability, capacity, and protocol fixes |
| Validator | Validator correctness, operability, and upgrade safety |
| Platform | API, ledger, Backroom, and dashboard maintenance |
| Documentation | Operator and miner guides that close a demonstrated gap |
| Incident | Diagnosis and remediation of a live incident, with verified evidence |

Not eligible:

- improving a miner's own score (emission already pays for that);
- work already paid through employment, contract, or another bounty;
- exploiting a vulnerability before reporting it, or disclosing it publicly first;
- benchmark gaming, or work whose main effect is an advantage for particular miners;
- marketing, community management, or translation not scoped as a bounty;
- work on repositories other than `ditto-assistant/ditto-subnet`, unless a
  policy revision adds the repository.

### Security reports

A **standing security bounty**, published in the policy with a maximum reward
per severity, covers vulnerability reports, so it is always published before
the work. Reports go through [GitHub private vulnerability
reporting](https://github.com/ditto-assistant/ditto-subnet/security/advisories/new),
signed like a claim to bind the payee. The standing bounty always holds one
earmark equal to its largest reward, so it never advertises unfunded money.
Each accepted report gets a public
placeholder entry with its category, earmarked maximum, and a hash of the
private details, so the hash chain stays complete. The details are published,
and checked against the hash, after the fix is disclosed.

## Contributor identity and payee

The payee is bound to a key, never to a GitHub account. #2045 implements the
claim format. This contract fixes the rules it must satisfy:

- A claim is signed either by a hotkey registered on SN118 or by a coldkey.
  - **Registered hotkey:** the payee is the coldkey that owns that hotkey on
    the SN118 metagraph when the claim is recorded
    (`ChainClient.get_registered_coldkey`).
  - **Coldkey:** the payee is the signing coldkey itself. This lets outside
    contributors take part without buying a registration.
- At approval, a hotkey claim's payee is checked again:
  - If the hotkey is now registered to a different coldkey, payment waits
    until the claim is renewed.
  - If the hotkey is no longer registered, payment waits until the recorded
    payee coldkey signs a renewal confirming itself.
- The GitHub account named in a claim identifies who opened the pull request.
  It cannot change the payee. Changing the payee takes a new signed claim, plus
  a signed release from the old key if the claim is handed off.
- Claims are domain-separated, replay-resistant, and bound to repository,
  issue, bounty reward revision, signer, and expiry, following the signed-action
  convention of `ditto-owner-link:v1`
  (`apps/platform/ditto/api_server/attestation.py`).

## Awards

### Lifecycle

The states a bounty must pass through depend on its kind:

| Kind | Required states |
|---|---|
| Code | `published → claimed → submitted → accepted → merged → approved → paid` |
| Evidence (no commit, such as an incident diagnosis) | `published → claimed → submitted → accepted → approved → paid` |
| Security report | `reported → accepted → merged → approved → paid → disclosed` |

- A bounty that requires deployment evidence adds `verified` after `merged`,
  or after `accepted` for an evidence bounty.
- **Approved** needs signed approvals meeting the award's tier.
- **Paid** needs a finalized chain receipt that matches the approval exactly.
- No state that a bounty's kind requires can be skipped.

Side states:

- `appealed` is not final. It returns the bounty to `accepted` or confirms the
  rejection.
- `rejected`, `expired`, `withdrawn`, and `cancelled` become final when
  `appeal_window_days` passes with no appeal, or when an appeal is decided.
- Before a cancellation is final, reviewers may still approve a partial award
  for work already delivered.

### Funds accounting

A **claim** reserves a bounty for its claimant (#2045). An **earmark**
reserves funds for a bounty. Both are ledger entries.

- **Earmark.** Publishing a bounty earmarks its maximum reward. The available
  balance may never go below zero, so a bounty on the board is always funded.
- **Commitment.** Approving an award converts its approved amount from earmark
  to commitment.
- **Release.** Whatever earmark is not committed is released once the decision
  that frees it is final: after a rejection, expiry, withdrawal, cancellation,
  or partial award, and the appeal window or appeal. An appeal can therefore
  never raise an award beyond funds still held for it.

### Approval

An approval is a signature by a reviewer key in the current roster. It covers
the bounty id, the GitHub issue, the **merge commit on `main`**, the signed
claim, the reward revision, the alpha amount, each payee coldkey, the policy
revision, and the reviewer's statement of no conflict. For a bounty whose
evidence is not a commit, the approval binds the hash of the recorded evidence
instead.

| Tier | Bounty's total approved amount | Required approvals |
|---|---|---|
| Standard | up to `single_review_max_alpha` | 1 reviewer |
| Large | above `single_review_max_alpha`, below `owner_review_min_alpha` | 2 distinct reviewers |
| Owner | `owner_review_min_alpha` or more | 2 distinct reviewers, one of them the owner reviewer key |

The tier applies to the bounty's total: every share of a shared award, plus any
amount an appeal adds. No bounty's total may exceed `per_award_max_alpha`.
Larger work is split into separately scoped bounties.

**The owner approves through the roster.** The owner never signs an approval
with the SN118 owner coldkey, which stays offline. Every policy revision that
enables the owner tier enrolls one sr25519 **owner reviewer key** in the
reviewer roster and marks it `owner`. The owner coldkey co-signs that revision
(see [Authority](#authority)), which binds the key to the owner; replacing the
key is a policy change. An owner-tier approval is verified exactly like any
other approval: the same roster lookup, the same signed no-conflict statement,
and the same [Conflicts](#conflicts) checks. The only additional check is that
one of its signers is the key marked `owner`. There is no separate owner
verifier, so a roster of non-owner reviewers cannot satisfy the owner tier and
the owner cannot bypass the conflict rules.

If the owner is conflicted on an owner-tier bounty, the owner reviewer key
cannot sign it. That award instead needs three distinct reviewers, none of them
conflicted and at least one of them not a custodian.

### Partial, shared, and cancelled awards

- **Partial.** Reviewers may approve less than the maximum reward, with a
  recorded reason.
- **Shared.** A team discloses its members and the split in the claim. The
  approval fixes each member's alpha amount and payee, and each share is its
  own payout with its own receipt. Collaboration is not misconduct. An
  undisclosed payment arrangement is.
- **Cancelled after a claim.** A reviewer may cancel a claimed bounty with a
  reason. Reviewers may approve a partial award for work already delivered.

### Double payment

- Each bounty has a Platform-issued id. One GitHub issue backs at most one
  active bounty.
- A merge commit or evidence hash backs approvals for at most one bounty. If
  one commit completes several bounties, a single approval names all of them,
  and its total stays within their combined maximum.
- Rewritten or reopened pull requests do not create new payable work, because
  approval binds the merge commit on `main`.
- Each chain receipt pays exactly one award share. A payout receipt is unique on
  block hash, extrinsic index, and event index.

### Payment is final

A chain transfer cannot be reversed, and this contract promises no clawback.
The reversible states come before payment: earmarks and commitments can be
released. A problem found after payment, such as plagiarism or undisclosed
conflict, is recorded as a `dispute_finding`. It can make the contributor
ineligible for future bounties. It does not reverse the payment.

## Conflicts

- A reviewer may not act on work claimed by themselves, their team, their
  employer or client, or any hotkey in their payment-owner family
  (`owner_root_for` in `apps/platform/ditto/api_server/name_claim.py`).
- Every approval includes a signed statement that the reviewer has no conflict.
  A false statement removes the reviewer from the roster and is recorded.
- A custodian may also be a reviewer. An award approved by a custodian always
  needs a second reviewer who is not a custodian, whatever its tier.
- The owner sets the submission fee and also funds the treasury from it. Fee
  changes stay separately governed and announced. Raising fees to grow the
  treasury must be stated as the reason for the change.

## Disputes and appeals

- A contributor may appeal a rejection, a partial award, a revocation, an
  expiry, or a cancellation within `appeal_window_days` of the decision.
- An appeal is decided by a reviewer who took no part in the original decision.
  If the appeal raises the award into a higher tier, it also needs that tier's
  approvals.
- Appeal outcomes are final and recorded with their reasons. Private review
  text stays private. The public record states the outcome and its reason
  category.

## Public records and accounting

### Ledger

The treasury ledger is append-only and hash-chained. Each entry has a type, the
policy revision in effect, a reason, a timestamp, the previous entry's hash,
and its references (bounty id, issue, commit or evidence hash, claim,
receipt). Entry types:

- **Decisions**, signed by the keys in [Authority](#authority):
  `policy_revision`, `custody_change`, `pause`, `unpause`,
  `bounty_published`, `earmark`, `acceptance`, `rejection`, `approval`,
  `commitment`, `revocation`, `cancellation`, `release`, `appeal_decision`,
  `dispute_finding`, `reconciliation_resolution`, `incident`,
  `incident_resolution`, `unpaid`.
- **Contributor actions**, signed by the contributor's key: `claim`,
  `claim_renewal`, `handoff`, `withdrawal`, `submission`, `appeal`.
- **Chain facts**, carrying the receipt or chain state that proves them:
  `inflow` (sweep), `external_receipt`, `conversion`, `yield`, `restake`,
  `payout`, `anchor`.
- **Automatic records**: `expiry`, `reconciliation`, and `gate_trip`, which
  anyone can recompute from earlier entries and the chain.

An entry that is neither signed nor backed by a receipt, and is not one of
these automatic records, is invalid.

Construction requirements:

- Appends serialize on a transaction-scoped advisory lock and enforce
  `UNIQUE(prev_hash)`. The score audit log's head lock (`append_audit_entry` in
  `apps/platform/ditto/db/queries/audit.py`) must not be copied as-is. Under
  READ COMMITTED, a writer waiting on `ORDER BY … LIMIT 1 FOR UPDATE` gets the
  old head back once the lock is released, so two appends can link to the same
  parent. An empty table also has no head row to lock.
- Triggers reject `UPDATE` and `DELETE` on every row and `TRUNCATE` on the
  table, extending the coding catalog append-only guard
  (`apps/platform/alembic/versions/2026_08_29_add_coding_catalog_exposure_ledger.py`).
- With each weekly reconciliation, a custodian anchors the ledger head on chain
  with `System.remark_with_event`. The anchor is itself a ledger entry, so
  rewriting history before an anchor is detectable by anyone.

### Published

A public read endpoint and dashboard page show:

- the policy revision history and the current [Authority](#authority) roster;
- the custody account and staking hotkey;
- balances: held TAO, held alpha, earmarked, committed, and available;
- every ledger entry with its signature or chain receipt;
- the fee rows behind each sweep;
- ceiling and cap usage for the trailing 30 days;
- the latest reconciliation result.

Private review text, reviewer notes, and security details before disclosure
are never published. The public record carries outcomes, amounts, keys,
commits, receipts, and reason categories. It follows the public-column
discipline of `/api/v1/public/audit` and `/api/v1/public/admin-activity`.

### Reconciliation

Every week, a `reconciliation` entry checks each asset separately:

- **TAO:** held TAO equals sweeps and external receipts, minus TAO staked by
  conversions, minus recorded transaction fees, minus TAO moved by custody
  changes or dissolution.
- **Alpha:** held alpha equals alpha from conversions, plus yield and external
  receipts, minus payouts, minus alpha moved by custody changes or
  dissolution.
  `restake` moves alpha between hotkeys and leaves the total unchanged.
- **Increases:** staking dividends on the treasury's stake raise held alpha,
  and anyone can send funds to a public address. Each reconciliation records a
  transfer into the treasury as an `external_receipt` and the remaining alpha
  increase as `yield`. Both are treasury money like any other, and an
  over-sized sweep counts as an external receipt. Only an unexplained
  **decrease** is an incident, so nobody can pause the treasury by sending it
  dust.

It also checks that:

- every closed sweep window has its sweep, for the right amount, within
  `sweep_deadline_days`;
- every payout receipt maps to exactly one approved award share, and every
  approved and paid share maps to exactly one receipt;
- every payout respected the ceiling and cap at its block;
- the ledger's hash chain verifies from genesis through the last anchor.

Receipts count only from finalized blocks (`ChainClient.get_finalized_block_hash`).
A failed reconciliation trips the [spending gate](#spending-gate). A signed
`reconciliation_resolution` that explains the difference is one step of
[Unpause after repair](#unpause-after-repair); on its own it reopens nothing.

## Pause and incidents

### Spending gate

The treasury is **paused** whenever its spending gate is closed. This is the
only definition of "paused" in this contract.

The gate is a deterministic check, `spending_gate(ledger, chain, fee_rows,
block)`, over data anyone can read. It is **open only when it positively
establishes every condition below as of a finalized block**. Anything it cannot
read, parse, or verify leaves it closed. Its state is computed, never stored:
no signature, flag, or ledger write can open it, and none is needed to close
it.

| # | Condition | Established from |
|---|---|---|
| G1 | No [manual pause](#manual-pause) is in force. | Signed `pause` and `unpause` entries, and any signed pause seen outside the ledger |
| G2 | The ledger is readable, its hash chain verifies from genesis to its head, and the head extends both the last on-chain anchor and the head this evaluator last verified. | The ledger and the `System.remark_with_event` anchors |
| G3 | The latest reconciliation passed, or its failure has a signed `reconciliation_resolution`, and it ran within `reconciliation_max_age_days`. | `reconciliation` entries |
| G4 | Every sweep window whose deadline has passed has its full sweep. | `inflow` entries and the published fee rows |
| G5 | The treasury's finalized balances, held TAO and alpha staked on the staking hotkey, are no lower than the balances the ledger explains at that block. | A finalized chain read and the ledger |
| G6 | Every `gate_trip` and every `incident` has a later `unpause` that references its `incident_resolution`. | `gate_trip`, `incident`, `incident_resolution`, and `unpause` entries |

G5 is checked at every evaluation, so an unexplained decrease closes the gate
at once instead of waiting for the weekly reconciliation.

A closed gate is either waiting or tripped:

- **Waiting.** An input cannot be read or verified: the Platform, the ledger
  export, the fee rows, or the chain endpoint is unavailable, or the latest
  reconciliation is merely overdue. The gate reopens on its own once every
  condition is established again. This is not an incident and needs no
  signature. It is the same rule as the [ceiling](#size-the-5-ceiling): data
  that cannot be read makes spending wait.
- **Tripped.** A condition is affirmatively violated: a failed reconciliation,
  a missed sweep deadline, an unexplained decrease, or a ledger that can be
  read but fails G2 (a broken chain, or a head that does not extend an anchored
  or previously verified head). A trip latches: the gate stays closed after the
  violation clears, until [Unpause after repair](#unpause-after-repair).

**Independent enforcement.** No single component holds the gate. Each party
below evaluates it for itself, from primary sources, before acting:

- **The Platform's ledger append path** evaluates the gate inside the append
  transaction, under the ledger lock. It refuses `earmark`, `approval`, and
  `commitment` entries while the gate is closed, and when it finds a new trip
  it appends the `gate_trip` record first.
- **Each custodian's signing tool** evaluates the gate from the public ledger
  export and its own finalized chain read, never from a Platform "paused"
  field. It pins the last ledger head it verified, and refuses to sign any
  extrinsic other than a custody change while the gate is closed. The chain
  requires two signatures, so every payout and conversion passes two
  independent evaluations. A Platform that is down, wrong, or compromised
  cannot open the gate for a custodian.
- **The payout verifier and every reconciliation** recompute the gate at the
  block of each payout and conversion. One executed while the gate was closed
  is recorded as a breach and trips the gate.
- **The public board and Backroom** show the gate state and every condition
  holding it closed, computed by the same function.

While the gate is closed:

- no earmarks, approvals, or commitments are recorded, and no conversion or
  payout is executed;
- chain facts are still recorded: a transfer that lands is entered with its
  receipt, so the ledger can always explain the chain;
- custody-change transfers are still allowed, so a compromised key can be
  rotated out;
- claims, submissions, reviews, acceptances, rejections, and appeals continue;
- earmarks and commitments stay in place unless a final decision releases
  them, and approved awards stay owed.

### Manual pause

The owner or any one custodian may close the gate at their discretion with a
signed `pause`. A restriction may arrive by any channel; a relaxation needs the
ledger.

- A signed `pause` takes effect wherever it is first seen. Custodian signing
  tools and the Platform honor a valid pause signature from any channel, and it
  is appended to the ledger, with its original signing time, as soon as the
  ledger accepts writes.
- An `unpause` takes effect only as a ledger entry.
- An unreadable ledger needs no pause: it already holds the gate closed under
  G2.

A manual pause reopens with a two-person `unpause`. A pause called for an
incident, such as a suspected key compromise, reopens only through
[Unpause after repair](#unpause-after-repair).

### Unpause after repair

A tripped gate, or a pause called for an incident, reopens only after these
steps, in order:

1. **Trip record.** A `gate_trip` automatic record names the violated
   condition, the first finalized block at which it held, and the entries or
   chain figures that establish it, so anyone can recompute it. The append path
   writes it when it detects the trip. If the ledger could not accept writes
   then, it is appended as soon as the ledger does, before any other decision.
   An incident with no automatic trip, such as a suspected key compromise, is
   recorded from step 2.
2. **Incident record.** The owner or any one custodian appends a signed
   `incident` that references the `gate_trip`, or the `pause`, that closed the
   gate. It is due within
   `incident_record_hours` of the trip becoming visible, or first once the
   ledger accepts writes. It classifies the cause (reconciliation mismatch,
   missed sweep, unexplained movement, ledger integrity, key compromise, or
   breach) and lists the affected entries and receipts. A late or missing
   incident record never opens the gate; it keeps it closed longer.
3. **Verified repair.** The cause is repaired and every gate condition is
   established again from primary sources. The repair is itself recorded: a
   late sweep as its `inflow`, an accounting difference as a
   `reconciliation_resolution`, a rotation as a `custody_change`. A restored
   ledger must extend every on-chain anchor; if it cannot extend a head a
   custodian had verified, the resolution names the lost entries and the new
   head, and custodians re-pin only after the unpause. A new reconciliation run
   after the repair must pass. An assertion that the problem is fixed is not
   enough.
4. **Resolution.** The owner or any one custodian appends a signed
   `incident_resolution`: the cause, the repair, the passing reconciliation it
   relies on, and what changed to prevent a repeat. This is the written
   post-incident record.
5. **Unpause.** Two different people among the owner and the custodians sign an
   `unpause` that references the `incident_resolution`. The append path refuses
   an `unpause` while any gate condition fails, and custodian tools ignore one
   that does not reference a resolution for every open trip and incident.

A waiting gate needs none of this. It reopens when its inputs can be read
again.

### Incidents

An unexplained movement of treasury funds, a suspected key compromise, a breach
of the gate, or a reconciliation that cannot be resolved is an incident. The
gate is already closed, by the trip or by a manual pause. The affected keys are
rotated (see [Rotation and recovery](#rotation-and-recovery)), and the incident
follows [Unpause after repair](#unpause-after-repair) from its record to a
two-person unpause.

## Policy revisions

- The treasury policy is one revisioned object. It holds the parameters below,
  the reviewer and custodian rosters, the custody account, the staking hotkey,
  and the security reward schedule. Each revision records its parent revision,
  a checksum, its reason, and the signatures [Authority](#authority) requires.
  It follows the `expected_revision` and typed-confirmation pattern of the
  existing admin controls.
- Revisions never apply retroactively. A claim keeps the reward revision it was
  recorded under, and an approved award keeps its amount.
- A change to this document takes effect only when a signed policy revision
  adopts the merged document revision, after `policy_notice_days`. Merging a
  pull request alone changes nothing.

## Parameters

In the shadow phase, only `fee_share_bps` is really zero. Every other value is
the proposed activation value, used as a hypothetical so the rehearsal
exercises every rule. Values marked **owner sets** are fixed and published in
the activation revision. The relative defaults avoid depending on price.

| Parameter | Proposed value |
|---|---|
| `fee_share_bps` | 0 in shadow; owner sets at activation |
| `sweep_window_days` / `sweep_deadline_days` | 7 / 7 |
| `emission_ceiling_bps` | 500 |
| `activation_cap_alpha` (trailing 30 days) | owner sets; at most the ceiling |
| `single_review_max_alpha` | 10% of `activation_cap_alpha` |
| `owner_review_min_alpha` | 25% of `activation_cap_alpha` |
| `per_award_max_alpha` | 50% of `activation_cap_alpha` |
| `max_staking_take_bps` | owner sets |
| Custodians / threshold | 3 / 2 |
| Reviewer roster | at least 3 keys besides the owner reviewer key |
| `claim_ttl_days` | 14, renewable (#2045) |
| `appeal_window_days` | 14 |
| `policy_notice_days` | 7 |
| Reconciliation and anchor | weekly |
| `reconciliation_max_age_days` | 8: one weekly cycle plus a day of slack before the gate waits |
| `incident_record_hours` | 24 |

## Rollout

Each phase starts only after the previous phase's exit criteria are met and
recorded.

| Phase | What happens | Exit criteria |
|---|---|---|
| 0. Specify | This document is reviewed and merged. | Owner approval by merge. |
| 1. Contracts | #2045 claim format and #2046 approval, entry, and receipt formats are published, with test vectors shared by the CLI and Platform. | Formats merged; replay, expiry, key rotation, partial-award, and failed-transaction tests pass. |
| 2. Shadow | The board runs (#2047). The ledger records bounties, claims, approvals, and hypothetical payouts with zero funds, against a hypothetical balance equal to the proposed cap. | Two consecutive weekly reconciliations of the shadow ledger pass; a rehearsal report compares "would have paid" against the proposed cap. |
| 3. Capped | Custody is created and published, and the activation revision sets `fee_share_bps` and a small `activation_cap_alpha`. | Reconciliations pass for 60 consecutive days with no unresolved incident. |
| 4. Steady | The cap may rise by policy revision, never above the 5% ceiling. | Continues while reconciliations pass. |

Custody is created only in phase 3, after this threat model and custody design
are approved. Any phase can return to the previous one through a protective
policy revision.

### Founding bounties

Issues #2044 to #2047 were opened before this contract existed. Nothing said
about them on GitHub or Discord is a payment promise. After activation, they
may become the treasury's first bounties, under this contract and with
acceptance judged by its rules. Until then, they are recorded in the shadow
ledger like any other bounty.

## Dissolution

If the treasury is discontinued, a policy revision announces it after
`policy_notice_days`. Committed awards are paid, open earmarks are released,
and the remaining balance returns to the owner in recorded transfers. The
ledger stays public.

## Threat model

| Threat | Mitigation |
|---|---|
| A custodian key is stolen | 2-of-3 multisig on chain; offline keys, no proxies; pause by any one custodian from any channel; rotation allowed while paused, even before the ledger accepts writes |
| A mismatch happens while the ledger or Platform is unreadable | The spending gate is closed whenever a condition cannot be verified; each custodian evaluates it from the public ledger and the chain, so stopping payouts needs no signature and no writable ledger |
| The Platform reports "not paused" while broken or compromised | Custodian signing tools compute the gate themselves and never trust a Platform flag; two independent evaluations precede every payout |
| A tripped gate is reopened quietly | Trips latch; reopening needs a trip record, a signed incident, a repair verified by a new passing reconciliation, a signed resolution, and a two-person unpause that the append path refuses early |
| One insider pays themselves or a friend | Reviewer approval separate from custody; conflict rules; tiered approvals on bounty totals; public ledger with receipts |
| The shared Platform admin token is used to fake a decision | Every decision entry is signed by an authorized key; the token only carries entries |
| One person unpauses or changes policy alone | Unpause and policy changes need two different people |
| The owner rewrites this document alone | A document change takes effect only through a signed revision, after notice |
| A GitHub account takeover redirects payment | Payee bound to the claim's key; GitHub identity cannot change it |
| A Discord or GitHub promise is presented as owed | Only signed approvals commit funds; stated in every bounty template |
| Claim replay, or a claim reused on another issue | Domain-separated, nonce-bound, expiring claims bound to repository, issue, and revision |
| Double payment through duplicate issues or rewritten pull requests | Platform bounty ids; one bounty per merge commit or evidence hash; unique receipts per award share |
| An appeal creates an unfunded award | Earmarks are held until the decision is final |
| A reorg or non-final block is counted as payment | Only finalized receipts count |
| A payout is timed against the alpha price | Awards fixed in alpha at approval; conversion batched ahead of need |
| Someone sends dust to the treasury to force a pause | Unexplained increases are recorded as treasury money; only unexplained decreases are incidents |
| Fee revenue is withheld from the treasury | Mandatory per-window sweeps with a deadline; a missed sweep deadline trips the spending gate publicly |
| The staking hotkey raises its take or loses its permit | Take limit in policy; immediate protective restake |
| Ledger history is rewritten | Signed entries; triggers blocking `UPDATE`, `DELETE`, and `TRUNCATE`; fork-safe chaining; weekly on-chain anchors |
| The treasury grows into an opaque fund | Sweeps recomputable from published fee rows; 5% ceiling; activation cap; weekly reconciliation |
| The owner raises fees to grow the treasury | Fee changes governed separately, announced, and stated as the reason |
| Reviewers self-deal through anonymous coldkeys | Signed no-conflict statement; second reviewer above the standard tier; roster removal on discovery |
| Routine approvals expose the owner coldkey | The owner approves with a roster key bound by an owner-signed policy revision; the coldkey signs only policy revisions and sweeps |
| The owner approves an owner-tier award to their own work | The owner reviewer key passes the same conflict checks as any reviewer; a conflicted owner-tier award needs three non-conflicted reviewers instead |
| Operators cannot see treasury state during an incident | Backroom read tools for the policy, rosters, balances, ledger, and reconciliation |

## Implementation obligations

What the other epic issues must build to satisfy this contract:

- **#2045 claims:**
  - the key-bound payee rules above, including re-checking at approval and
    deregistered hotkeys;
  - claim, renewal, handoff, withdrawal, and appeal as signed actions with
    server-issued claim ids, and security reports signed the same way;
  - one shared signed-action verifier instead of a fourth copy of
    `verify_signed_action`;
  - unique nonces, with insert conflicts mapped to replay errors rather than
    server errors.
- **#2046 acceptance and payout:**
  - the lifecycle per bounty kind, and funds accounting;
  - signature verification for every decision and contributor entry type;
  - a payout verifier that checks finalized, multisig-wrapped
    `SubtensorModule.transfer_stake` extrinsics and their events against the
    approval;
  - sweep verification per window and deposit address, with the fee rows
    published and each row's block number stored or resolved;
  - the ceiling check from `IncentiveAlphaEmittedToMiners` totals;
  - the ledger construction above, and per-asset reconciliation including
    yield;
  - one shared `spending_gate` implementation, used by the ledger append path,
    the custodian signing tool, the payout verifier, and the board, with test
    vectors for every condition: an unreadable ledger, an overdue
    reconciliation, a failed reconciliation, a missed sweep, an unexplained
    decrease, and a head that does not extend a pinned one;
  - a custodian signing tool that evaluates the gate from the public ledger and
    its own chain read, pins the last verified head, and honors a signed pause
    from any channel;
  - the `gate_trip`, `incident`, and `incident_resolution` entries, and the
    append path's refusal of an `unpause` before every gate condition holds;
  - owner-tier verification through the same roster verifier plus the `owner`
    mark, including the three-reviewer rule when the owner is conflicted;
  - tests for replay, partial awards, key rotation, expiry, and failed
    transactions.
- **#2047 board:**
  - templates that link this policy, show the reward revision, reviewer, and
    expiry, and state that labels and comments are not payment authorization;
  - automation may mirror ledger state onto labels, but must not rely on
    `issue_comment` or `pull_request_target`, which the workflow security check
    bans (`.github/scripts/check_workflow_security.py`).
- **Backroom:** `backroom:read` tools for the treasury policy, rosters,
  balances, ledger, reconciliation status, spending-gate state with every
  condition holding it closed, open incidents, and the submission deposit
  address, which Backroom MCP cannot read today. Operators need all of these to
  diagnose an incident.

## Acceptance criteria for #2044

| Criterion | Where this contract meets it |
|---|---|
| Threat model and custody design precede treasury registration | [Threat model](#threat-model), [Custody](#custody), and [Rollout](#rollout): custody is created only in phase 3 |
| Every inflow, reservation, payout, cancellation, and policy change is attributable | [Principles](#principles) 3, [Authority](#authority), [Funds accounting](#funds-accounting), [Ledger](#ledger) |
| No GitHub issue or Discord promise alone can move funds | [Principles](#principles) 1, [Authority](#authority), [Spending authority](#spending-authority) |
| Chain receipts reconcile to accepted work | [Double payment](#double-payment), [Reconciliation](#reconciliation) |
| Activation begins with a reversible cap | [Parameters](#parameters) (`activation_cap_alpha`), [Rollout](#rollout) phase 3, [Spending gate](#spending-gate) |
