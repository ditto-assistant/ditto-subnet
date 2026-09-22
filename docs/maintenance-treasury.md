# SN118 maintenance treasury and governance contract

Status: **proposed**. Before any treasury key is registered or any funds move,
maintainers must approve this contract, including every item in
[Decisions that need approval](#decisions-that-need-approval). The parent epic is
#2054. The contracts that build on this one are #2045 (claims), #2046 (acceptance
and payout), and #2047 (board).

This document defines where the maintenance treasury gets its money, who holds
it, what it may pay for, and how every movement is recorded. It sets policy
only. It does not register keys, change weights, or approve a bounty.

## Principles

1. **Only a signed treasury action moves funds.** Chain transfers are made only
   under an approved payout record. A GitHub issue, label, merge, deployment,
   Discord message, or DM never authorizes one.
2. **Every state change can be attributed.** Each inflow, reservation, approval,
   payout, cancellation, and policy change is an append-only ledger entry. Each
   entry names an actor and a reason.
3. **Failures stop outflows.** Missing, malformed, or unreconciled policy
   stops inflow and freezes outflow. It never falls back to paying out.
4. **Shadow before funds, and a cap before full funding.** Activation follows the
   epic order: approve, publish contracts, rehearse without funds, run a capped
   allocation, then pay against accepted work.
5. **Reuse existing mechanisms.** Every mechanism below already has a working
   precedent in this repository. Each section names it.

## Funding source

### Where the money comes from

SN118 emission is split by Subtensor between miners, validators, and the subnet
owner. Validators control only one part of it: the miner incentive vector they
submit with `put_weights`. The treasury can therefore draw on one of two sources:

| | A. Treasury UID in the miner vector (recommended) | B. Transfer from the owner's emission |
|---|---|---|
| How funds arrive | Every validator folds `treasury_share` of its miner vector onto one registered treasury hotkey | The owner coldkey periodically transfers a committed amount to the treasury |
| Who pays for it | Miners: a visible, bounded cut of KOTH emission | The owner: part of the owner's take |
| Enforcement | Yuma consensus over the whole validator fleet; every epoch is visible on chain | Relies on the owner's discipline; each transfer is a discretionary act |
| Opacity risk | Low: the share is served policy with revision history | High: this is the opaque discretionary fund the issue rules out |
| Chain prerequisites | One registered UID under a coldkey that is not owner-associated | None |

This contract recommends **option A**. Its inflow cannot be turned on or off
quietly. Every validator folds the same served number, the chain records what
the treasury UID earned each epoch, and anyone can compare the two. The cost is
real and must be stated plainly: at full activation, miners receive
`1 - burn_share - treasury_share` of the miner vector instead of
`1 - burn_share`.

### Why the burn share can't be reused

`apply_miner_emission_cap` (`ditto/validator/weights.py`) routes the residual to
`FINNEY_BURN_HOTKEY`, which is the subnet owner's hotkey (UID 0). Subtensor
burns miner incentive sent to an owner-associated hotkey. Weight sent there
never reaches a spendable balance. A fixed split of the existing burn share
would therefore still burn the money. The treasury needs its own destination:
a separate registered hotkey owned by a coldkey with no owner association. That
coldkey is the custody key described below.

### Denominator

"5%" means **500 basis points of the miner incentive vector**, capped as
described in [Activation](#activation-and-caps). Measured against total subnet
emission, that is 5% of the miner part (about 2% of all SN118 emission at the
current 41% miner split). If maintainers want 5% of total emission, the miner
vector share becomes about 1,220 bps. That is a larger miner cut and needs
separate approval (decision D1).

### Validator fold contract

The validator changes in the implementation stack follow the `burn_share` and
`track_shares_bps` precedents exactly:

- Platform serves a resolved `treasury_share_bps: int` on `LedgerResponse`,
  stored as a revisioned `TreasurySettings` policy. It uses the same
  `expected_revision`, typed confirmation phrase, reason, actor, and append-only
  history as `endpoints/admin_burn_settings.py`.
- The validator compiles in two constants:
  - `FINNEY_TREASURY_HOTKEY`, the treasury UID's hotkey;
  - `MAX_TREASURY_SHARE_BPS`, which is 500.
- The platform serves **only the share, never the destination**. A compromised
  or misconfigured Platform therefore cannot redirect emission to an arbitrary
  hotkey. Changing the treasury hotkey or the ceiling requires a public
  validator release.
- The fold resolves `treasury_share_bps` to **0** in each of these cases:
  - the field is missing, `bool`, non-`int`, negative, or above
    `MAX_TREASURY_SHARE_BPS`;
  - `burn_share + treasury_share > 1`;
  - the treasury hotkey is absent from the metagraph.

  Resolving to 0 pays miners. This is the opposite failure direction from the
  burn fallback, and it is intentional: an unreadable treasury policy must never
  take emission.
- The treasury hotkey is excluded from the miner pool, the same way the burn
  hotkey is. With no eligible miners, the safe idle vector still routes 100% to
  burn, never to the treasury.
- Validator telemetry reports `weights/treasury_share` next to
  `weights/burn_share`.

A share change reaches the chain only as each validator reaches its next epoch.
Epochs last about an hour and are not synchronized across the fleet. Caps,
pauses, and reconciliation therefore count **per epoch**, not per block.

## Custody

| Key | Holder | Can do | Can't do |
|---|---|---|---|
| Treasury hotkey | Registration only, held offline after registration | Hold the treasury UID and receive incentive | Move stake or TAO |
| Treasury coldkey | A Substrate `Multisig` account, 2-of-3 by default (D2) | Unstake treasury alpha and send payouts, only when signatories co-sign | Act on a single signature |
| Signatory keys | Three named maintainers who do not share a machine or a secret store | Approve one multisig call | Spend alone |
| Platform | No treasury key | Record ledger entries, serve `treasury_share_bps`, and verify receipts | Sign or submit any treasury extrinsic |

Rules:

- **Holding keys is not spending authority.** A payout needs an approved ledger
  record (see #2046) and a multisig extrinsic whose call data matches that
  record. Signatories co-sign only after checking the call hash against the
  published approval.
- **Platform never holds a treasury key.** Secrets live in Secret Manager or
  offline hardware. They are never in this repository, CI, or Backroom.
- **Signatory rotation** creates a new multisig address. The old signers move
  the balance to the new address in one recorded `custody_rotation` entry, and
  both addresses stay listed in the ledger. Each new signatory proves the key
  change by signing a `ditto-treasury-signer:v1` payload with both the outgoing
  and incoming keys. This follows the two-key `ditto-owner-link:v1` attestation
  pattern in [OWNER-LINKS.md](OWNER-LINKS.md).
- **Treasury hotkey rotation** uses Subtensor's hotkey swap, together with a
  validator release that changes `FINNEY_TREASURY_HOTKEY`. Inflow resolves to 0
  until the release is adopted, and the ledger records the gap.
- **Recovery.** Losing one signatory key leaves 2-of-3 able to rotate. Losing
  two keys is unrecoverable by design. The exposure is bounded by the outflow
  and balance caps below, not by a recovery backdoor.
- **Registration order.** The treasury UID is registered only after this
  contract, the threat model, and the custody design are approved (acceptance
  item 1). It is registered at activation, not during shadow. An unfunded UID
  would drift out of immunity with zero incentive and could be pruned.

## Eligible work

A bounty is eligible only if it improves the public SN118 product or its
operations, and it must fall into one of these classes:

| Class | Examples |
|---|---|
| Security | Vulnerability fixes, hardening, dependency review, secret-handling fixes |
| Benchmark | DittoBench correctness, determinism, adapters, dataset integrity |
| Screener | False-positive and false-negative fixes, capacity, protocol conformance |
| Validator | Fold correctness, updater reliability, telemetry |
| Platform and Backroom | API, migrations, operator visibility, MCP read tools |
| Documentation | Miner, validator, and operator guides that close a real support gap |
| Incident | Diagnosis or remediation of a live incident, with a linked postmortem |

These do not qualify: mining a better agent (KOTH already pays for that), work
already paid by another program, private or closed-source work, and anything
without objective acceptance evidence.

## Roles and conflicts

- **Proposer** opens the bounty, stating scope, evidence, reward range, reviewer,
  dependencies, and expiry (template in #2047).
- **Reviewer** accepts or rejects the work against the stated evidence.
- **Approver** binds the award to the exact commit, claimant, reward revision,
  and amount (#2046).
- **Signatory** co-signs the chain transfer.

Conflict rules:

- A person may not review, approve, or sign for a bounty they claimed, share a
  payment coldkey with, or co-authored.
- Teams disclose their payment coldkey and members when they claim. Working
  together is not misconduct. An undisclosed shared payee is.
- A maintainer may claim bounties. Their award then requires one more approver
  who is not conflicted.

## Approval thresholds

The thresholds are proposed defaults. Maintainers set the final numbers (D2).

| Award | Required |
|---|---|
| Up to `SMALL_AWARD_TAO` (proposed 5 TAO) | 1 reviewer + 1 approver who is a different person, then 2 multisig signatures |
| Above `SMALL_AWARD_TAO`, up to `LARGE_AWARD_TAO` (proposed 25 TAO) | 2 distinct reviewers + 1 approver, then 2 multisig signatures |
| Above `LARGE_AWARD_TAO`, or above 20% of the treasury balance | 3 distinct non-conflicted operators, then all multisig signatures |

The distinct-operator shape follows the name-claim `ENDORSEMENT_THRESHOLD = 3`
precedent (`apps/platform/ditto/api_server/name_claim.py`). No single person can
be both the last approver and the only remaining signatory.

## Lifecycle, disputes, and reversals

Each stage is its own recorded state: `claimed` -> `submitted` -> `accepted` ->
`merged` -> `deployed/verified` -> `approved` -> `paid`. The terminal alternatives
are `rejected`, `cancelled`, and `expired`. #2046 owns the full state machine.
This contract fixes these rules:

- **Reservations are the only reversible state.** A reservation earmarks
  treasury balance for a claimed bounty. It may expire, be released, or be
  revoked with an audited reason. Revoking a reservation is the only kind of
  clawback.
- **Payment is final.** Chain transfers cannot be reversed, so this contract
  never promises post-payment clawback. Funds move only after approval. A
  problem found later becomes a `dispute` entry and can lead to a reduced future
  award or a conflict finding. It is never presented as a reversal.
- **Partial and shared awards** are one approval with several payee lines. The
  lines must add up to the approved amount, and each line is bound to its own
  signed claimant.
- **Cancellation** before approval releases the reservation. Cancelling an
  approved but unpaid award needs the same threshold as the original approval.
- **Disputes** are filed within 14 days of the terminal state and decided by a
  reviewer who is not conflicted. The decision is a ledger entry that references
  the entry it disputes.
- **Failed transactions** are recorded as `payout_failed` with the extrinsic
  receipt. Nothing retries automatically: a new payout needs a new co-signed
  call, consistent with [no-automatic-retries.md](no-automatic-retries.md).

## Public accounting

The treasury ledger is an append-only, hash-chained log. It uses the same
construction as the score audit log (`apps/platform/ditto/db/queries/audit.py`):

- The genesis entry links to `GENESIS_HASH`.
- Each entry embeds `prev_hash`, and its `entry_hash` is the SHA-256 of its
  canonical JSON.
- Appends serialize on a `SELECT ... FOR UPDATE` head lock.
- Each entry is written inside the transaction that makes its state durable.

Entry kinds: `policy_revision`, `custody_rotation`, `inflow_epoch`,
`conversion`, `reservation`, `reservation_release`, `approval`, `payout`,
`payout_failed`, `cancellation`, `dispute`, `dispute_resolution`, `pause`,
`correction`.

A `correction` references the entry it corrects. Nothing is edited or deleted.

Every entry carries `actor`, `reason`, `policy_revision`, and a timestamp. Chain
entries also carry the block number, canonical block hash, and extrinsic index.

- **Inflow** is observed on chain from the treasury UID's incentive and stake
  per block range. It is never self-reported.
- **Conversion.** Emission arrives as alpha staked to the treasury hotkey. An
  unstake to TAO is a `conversion` entry, with the alpha given up, the TAO
  received, and the receipt.
- **Payouts** are denominated and verified in TAO rao. They reuse the check
  order in `PaymentVerifier` (`apps/platform/ditto/api_server/payment_verifier/`)
  in reverse:
  1. canonical block hash resolved from the block number;
  2. `Balances.transfer_keep_alive`, nested in `Multisig.as_multi`;
  3. `ExtrinsicSuccess`;
  4. the treasury multisig as signer;
  5. the approved payee coldkey as destination;
  6. the exact approved amount.
- **Valuation.** Rao is the only authoritative unit. USD figures from the
  existing price oracle are informational only and never decide an award.
- **Reconciliation.** Every day, and before every payout, two balances must
  match the chain. The alpha stake on the treasury hotkey must equal inflows
  minus the alpha given up in conversions. The multisig's TAO balance must equal
  the TAO received from conversions minus payouts and fees. A mismatch writes a
  `pause` entry that freezes outflows until a `correction` explains it.
- **Public read.** Anyone can read and verify the ledger on the existing audit
  line. Payloads are limited to public inputs: issue, PR, commit, claimant
  hotkey and payee coldkey, amounts, and receipts. Nothing from private review
  text or screening evidence goes in.

## Emergency pause and governance changes

- **Inflow pause:** a `TreasurySettings` revision to 0 bps. Any single
  maintainer may make it, because lowering the share is the safe direction. It
  takes effect within one epoch across the fleet.
- **Raising the share** needs two distinct maintainers, recorded as proposer and
  confirmer, and cannot exceed the compiled `MAX_TREASURY_SHARE_BPS`.
- **Outflow freeze:** a `pause` entry. Any signatory may add one, and a failed
  reconciliation adds one automatically. Removing it needs the large-award
  threshold. Signatories can also refuse to co-sign, which is the multisig's
  built-in veto.
- **Policy changes** such as thresholds, eligible classes, or caps are
  revisioned `policy_revision` entries with `expected_revision`. They apply to
  bounties opened after the revision. Each bounty is bound to the reward
  revision in force when it was opened.

## Activation and caps

| Stage | `treasury_share_bps` | Funds move | Exit criteria |
|---|---|---|---|
| 0. Approval | n/a | No | This contract and D1-D3 approved |
| 1. Shadow | 0 (UID not registered) | No | Ledger records hypothetical awards against accepted work for at least 14 days. Rehearsal reconciles "would have paid" against the stage-2 cap |
| 2. Capped | 100 | Yes, outflow cap `MONTHLY_OUTFLOW_CAP_TAO` (proposed 25 TAO/30 days) | 14 days of clean daily reconciliation and at least one verified payout |
| 3. Target | 500 | Yes, cap reviewed by revision | Ongoing |

Every step is a reversible `TreasurySettings` revision. Moving back one stage is
an inflow pause and a new revision, never a code rollback. Reservations may
never exceed the reconciled balance minus the outflow still available under the
cap.

## Threat model

| Threat | Mitigation |
|---|---|
| Compromised or buggy Platform redirects emission | The destination hotkey and ceiling are compiled into the validator. Platform serves only a capped share, and invalid values resolve to 0 |
| Platform or Backroom compromise tries to spend | Platform holds no treasury key. Spending needs a multisig co-signed against a published approval hash |
| One signatory is compromised or coerced | 2-of-3 multisig, plus all signatures for large awards. The outflow freeze is available to any other signatory |
| An insider pays themselves or a friend | Conflict rules, distinct-operator thresholds, and a public ledger bound to exact commits and payees |
| A GitHub account takeover redirects a payout | The payee is the claimant's hotkey-signed coldkey (#2045). GitHub identity is never a payment destination |
| A Discord or issue promise is treated as a debt | Principle 1: only an approved ledger record can lead to a payout |
| Double payment across duplicate issues or rewritten PRs | The approval binds the exact commit and bounty. #2046 enforces one payout per bounty revision and payee line |
| Replay of a claim or approval | Claims bind repository, issue, contributor, revision, and expiry (#2045). Approvals bind the policy revision |
| Silent ledger rewrite | The hash chain can be verified from the public read, and chain receipts can be checked by anyone |
| Treasury UID is deregistered | Registered only at activation with non-zero incentive. Inflow resolves to 0 if the hotkey leaves the metagraph |
| Alpha price drop between inflow and payout | Awards are fixed in TAO at approval, and conversions are recorded, so drops show in the balance, not in awards |
| Validator fleet disagrees mid-change | Served, already-resolved scalar; caps count per epoch |

## Acceptance mapping (#2044)

| Acceptance item | Where it is satisfied |
|---|---|
| Threat model and custody design precede treasury registration | [Custody](#custody) (registration order), [Threat model](#threat-model), activation stage 0 |
| Every inflow, reservation, payout, cancellation, and policy change is attributable | [Public accounting](#public-accounting): each entry has an actor, reason, and revision |
| No GitHub issue or Discord promise alone can move funds | Principle 1, [Custody](#custody) rules, threat model |
| Chain receipts reconcile to accepted work | Payout verification and daily reconciliation in [Public accounting](#public-accounting) |
| Activation begins with a reversible cap | [Activation and caps](#activation-and-caps), stage 2 |

## Decisions that need approval

- **D1: Funding source and denominator.** Choose option A (treasury UID,
  recommended) or option B (owner transfer). For option A, choose 500 bps of the
  miner vector (about 2% of total emission) or about 1,220 bps (5% of total
  emission).
- **D2: Custody and thresholds.** Name the three signatories. Confirm 2-of-3,
  `SMALL_AWARD_TAO`, `LARGE_AWARD_TAO`, and `MONTHLY_OUTFLOW_CAP_TAO`.
- **D3: Bootstrap bounties.** Issues #2044-#2047 predate the contract they
  create. Either record their awards as the ledger's first entries after
  genesis, approved under this contract once it is adopted, or declare them
  unpaid founding work. Until that decision is recorded, no one may treat them
  as owed.

## Implementation handoff

| Work | Owner issue |
|---|---|
| Hotkey-signed claim payload, reservation, handoff, and appeal | #2045 |
| `TreasurySettings` revision API and `treasury_share_bps` on the ledger; validator fold and constants; ledger tables and hash chain; payout verifier variant; reconciliation job; Backroom MCP read tools for treasury policy, balance, and ledger | #2046 |
| Board views, bounty template fields (`policy revision`, `reward revision`, `reviewer`, `expiry`, `conflicts`), contributor guide | #2047 |

Every Platform setting above must also be readable through Backroom MCP, not
only shown in the UI, as required by the repository's operator-visibility rule.
