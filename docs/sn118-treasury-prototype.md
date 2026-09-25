# SN118 treasury prototype: maintenance and GM credits

Status: **shadow allocation; execution code present but not activated**.
Deploying the Platform/Backroom change records a proposed policy but does not
change validator weights, create a production key, move tokens, buy GM credit,
or approve a bounty. The two allocation fields default to zero and the API
rejects `mode=active`. The isolated signer runner has not been installed or run
on a production host.

## Why this exists

In the September 25 SN118 Discord discussion, const proposed using **some**
emissions for product inference credits, while keeping miners free to do their
own work. Mog raised the legitimacy and abuse risk of diverting emissions to
operator expenses and suggested scored-miner inference as an alternative.
Peyton asked for a practical prototype before any chain-native credit primitive.
The prototype therefore shows two separately governed purposes: subnet
maintenance bounties and Omni Aura's GM inference credit. Any activation must
publish the allocation, wallet identity, spending receipts, and evidence of
actual product use. No operator reimbursement can be disguised as a bounty.

SN66 Conjectures publicly [describes](https://conjectures.io/how-it-works) a
treasury receiving its miner emissions and paying solved-proof bounties. Its
[validator](https://github.com/conjectures-io/conjectures-validator) sets
100% weight to a treasury UID. SN118 must preserve at least 95% of the miner
vector under this initial hard ceiling. The current SN118 `burn_share` routes
residual weight to the subnet owner's burn hotkey; it **destroys** that share and
must never be used as treasury funding. An actual treasury recipient needs a
separately owned, registered hotkey, chain ownership verification, a validator
release and synchronized policy serving. This PR adds none of those live paths.

## Proposed flow and controls

1. Backroom `get_treasury_settings` reads the complete, append-only revision
   history. `record_treasury_settings` writes a **shadow** revision with an
   expected revision, operator email, reason, checksum, and exact confirmation.
2. `maintenance_bps` and `gm_bps` are separate. Their sum is bounded at 500 bps
   of the miner vector, and `miner_bps` is derived. The record also pins the
   proposed registered hotkey, coldkey, GM account reference, daily and single
   top-up limits, and price-impact limit. A nonzero GM proposal requires an
   account reference. The Platform and Backroom never receive wallet secrets.
3. A later, separate activation change must verify the hotkey is registered on
   SN118, owned by the dedicated coldkey and **not** the subnet owner; verify
   all validators and Platform ledger-pin/emission diagnostics understand the
   new recipient; and rehearse at zero and then a small cap. Empty eligible
   miner vectors must remain burn, never treasury. A failed identity or policy
   read must keep the prior weights or fail to the existing miner-preserving
   default, not route to an arbitrary address.
4. Inflows to one dedicated treasury wallet are accounted into two virtual
   budgets by the policy revision pinned to each epoch. Spendable balances are
   limited to finalized, reconciled inflows. Bounty approval binds an issue,
   exact accepted commit, claimant proof, payee, amount, independent reviewer,
   policy revision, and finalized payout receipt. GM budget spending binds a
   live quote, linked sending wallet, current GM payment instructions, chain
   receipt, and before/after `GET /v1/credits` readings. No GitHub label,
   Discord message, merger, or quote can authorize a payment.
5. A paused or unreconciled state prevents signing. Each attempt gets an
   idempotency key. The signer journal commits `dispatch_started` before any
   chain call; an interrupted or ambiguous call stays blocked and is never
   retried automatically. Daily/single limits, fresh quotes, independent
   review, and current instruction digests are checked on the signer host.
   Recovery from an ambiguous submission remains a separate reviewed action.

## Custody boundary

Terraform defines a disabled-by-default private, Shielded GCE host with no
public IP or app ingress, a dedicated service account, and one Secret Manager
container. The signer service account may **read only this one secret**. It has
no Platform DB token, Backroom session, validator wallet, CI/WIF impersonation,
or GM API key. Peyton's explicit Google identity is the only IAP OS Login
principal in this module. Terraform stores no secret version or mnemonic.

`scripts/treasury_create_key.py` is a one-time, host-checked ceremony: it
generates a 24-word sr25519 mnemonic in process memory, sends it to Secret
Manager on `gcloud` stdin, and prints only the public address. To run it,
Peyton must review the Terraform plan, apply through the protected infra path,
temporarily grant the host service account `secretmanager.versions.add` on that
one secret using the unbound `sn118TreasuryKeyProvisioner` custom role (which
also permits listing versions), install the pinned runtime, run the ceremony
on the host, verify
the public address independently, then revoke the temporary adder grant. The
script refuses a second version. **None of these steps has been executed.**
The current single-key design needs an explicit custody review before funding;
the open 2-of-3 multisig design in PR #2119 cannot be assumed compatible with
the registered hotkey until its exact Subtensor call path is tested.

## Conversion and GM settlement

[GM's billing instructions](https://docs.saygm.com/platform/billing/) accept
card, USDC, TAO, or **SN28 GM alpha**. DITTO SN118 alpha cannot be sent as SN28
alpha. Two supported *planning* routes are:

| Route | Chain conversions | GM direct deposit | Main tradeoff |
|---|---|---|---|
| DITTO → TAO → credits | Sell SN118 alpha once | Finney TAO from a GM-linked wallet | One pool trade; GM credits USD at confirmation |
| DITTO → TAO → GM alpha → credits | Sell SN118 alpha, buy SN28 alpha | Transfer **SN28 stake on the same hotkey** from a GM-linked wallet | Two pool trades and second price impact; may be attractive only if GM's live conversion terms justify it |

At finalized Finney block **9,146,534** (`0xca7aa7a326779eb1570dbcc21f0a40a3525cba2d09271bc58ab88592cbd6a42e`),
the read-only Bittensor SDK quote for 1 DITTO alpha was 0.007417979 TAO, then
0.332113301 GM alpha if that TAO entered the SN28 pool. SN118 reserves were
862,003.973679115 alpha and 6,394.334606861 TAO; SN28 reserves were
1,035,343.187444650 alpha and 23,125.094774575 TAO. SDK-reported price
impact was 9 rao of TAO in the first pool and 99 rao of GM alpha in the
second. These are **estimates from one block**, before network fees, possible
stake movement restrictions, changing reserves, and GM's confirmed-deposit
USD conversion. A fresh quote and executable call simulation are mandatory
before any payment. Backroom's `quote_treasury_topup` reads both finalized
pools live without a wallet and returns both route amounts and price impact;
it never estimates GM USD credits. `scripts/treasury_live_quote.py` provides
the same independent read, while `scripts/treasury_quote.py` accepts a reviewed
snapshot and records a bounded dry run in a hash-chained local JSONL ledger.
Backroom's `preview_treasury_topup` evaluates one selected route against the
proposed single-payment and impact caps; it reports wallet linking, current GM
instructions, and daily spending as unverified and always disables execution.

GM documents no public credit-purchase API. The human account owner must sign
in at [GM Billing](https://saygm.com/dashboard/credits), choose TAO or Alpha,
link the exact sending CLI wallet once through Taostats Auth by signing a
message, select that wallet, and obtain **current** payment instructions. GM
says links are permanent. The transfer must come from that linked wallet, not
an exchange; an SN28 alpha payment must preserve the subnet and hotkey shown
by GM. The current receiving address must be re-read before each payment.
After finality, compare the deposit in GM Billing and query the authoritative
[GET /v1/credits](https://docs.saygm.com/api-reference/operations/getcreditbalance/)
using a separately scoped GM API key. Do not blindly retry an ambiguous
deposit. GM converts to USD when it confirms the deposit; the estimate can
change. An unexpected balance delta or missing deposit freezes further top-ups.
`scripts/treasury_gm_credits.py --ledger <private-jsonl-path>` reads the exact
credit balance with `GM_API_KEY` in its environment and records a read-only
observation. It cannot purchase credits and cannot prove which deposit caused a
balance change; the operator must correlate GM's deposit row, chain receipt,
and intervening inference usage.

## Durable execution and daily trigger

The [operator runbook](sn118-treasury-operator-runbook.md) gives the exact
review, custody, planner, plan-file, and one-leg payment sequence.

`ditto/treasury/store.py` uses a private SQLite WAL journal with full synchronous
commits and an append-only, hash-chained event table. It starts paused. A plan
binds one idempotency key, policy revision, GM account reference, linked sender,
destination, hotkey, finalized quote, input amount, minimum outputs, spending
caps, and the digest of current Billing instructions. Only one unresolved plan
may exist. A different reviewer approves the exact plan hash. Each chain leg
needs a fresh review and quote. The journal reserves the proposed TAO amount
against the UTC-day cap even if a call later fails. A separate allocation table
records finalized epoch inflows for GM and maintenance with an independent
reviewer. The signer deducts every noncancelled GM plan from **GM-allocated**
alpha; maintenance and unallocated alpha cannot fund a top-up. Allocation
entries currently require human verification of the finalized chain block and
epoch accounting, since emission routing is still inactive.

`scripts/treasury_payment.py` supplies status, pause/unpause, proposal,
approval, one-leg execution, cancellation of an unapproved plan, and GM
reconciliation. `execute` reads the mnemonic only on the named Shielded signer
VM from its one Secret Manager secret. The key stays in process memory. It
uses Bittensor SDK price-protected `unstake` on SN118, optionally price-protected
`add_stake` on SN28, and either `transfer` of TAO or `transfer_stake` of SN28
alpha to the current GM Billing recipient. All calls request finalization. The
SDK receipt's extrinsic and block hashes are recorded before the next leg may
be approved. An ambiguous response pauses the journal and leaves the leg in
`dispatching`; the tool will not submit it again. An independently checked
Billing deposit reference, credited nano-USD amount, intervening usage, and
before/after API balances must satisfy exact arithmetic to complete
reconciliation. The CLI does not infer deposit or usage values from a balance
delta; the operator must check both in GM Billing and usage records.

The daily timer under `infra/systemd/sn118-treasury-daily.*` is for a separate
planner host with a read-only GM API key and no wallet key. At 09:00 UTC it
reads `GET /v1/credits` and, below a reviewed floor, writes one private,
idempotent request for that UTC date. The request is capped at 10 DITTO alpha
and names the route and credit target. It does not sign or purchase. The
operator must obtain current Billing instructions, take a fresh chain quote,
and turn that request into a reviewed signer plan. GM currently documents a
Billing UI instruction flow and no public purchase/instruction API, so an
unattended daily payment would rely on stale or unverified recipient data.

The instruction JSON file contains exactly `asset`, `source`, `destination`,
`hotkey`, and `account_ref`; its canonical SHA-256 is pinned in the plan. The
TAO route uses an empty `hotkey`; the SN28 route uses the exact hotkey in GM
Billing. The signer will not dispatch if the file, route, sender, recipient,
or reviewed digest differs. Approvals must be renewed for each leg against
current instructions and a finalized quote no more than 120 seconds old. The
account owner must link the wallet through Taostats Auth before the first plan.

The local runner is an executable implementation, **not** an activation
decision. No live signing, testnet transaction, key creation, protected plan,
or transfer has been performed. Before enabling it, reviewers must inspect
the exact Terraform plan against production state, test SDK receipt and fee
semantics with a funded disposable wallet, verify SN28 same-hotkey transfer
behavior, review every operator plan file and GM account linkage, and establish
an independently audited recovery procedure for an ambiguous dispatch.

## Existing work audit

- [#2119](https://github.com/ditto-assistant/ditto-subnet/pull/2119) usefully
  specifies custody, signed decisions, and fail-closed reconciliation, but
  changes the funding source to submission fees. Its exact current head has
  **CHANGES REQUESTED**: `ledger_recovery` is missing from the entry/authority
  contract, and the unpause obligation contradicts the scoped recovery precheck.
- [#2097](https://github.com/ditto-assistant/ditto-subnet/pull/2097) and
  [#2073](https://github.com/ditto-assistant/ditto-subnet/pull/2073) propose
  a 5% miner-vector cut to a treasury UID. Their registration, custody,
  source-emission classification, and activation assumptions need exact-chain
  review before reuse. #2097's broad combined implementation should not be
  merged as a shortcut.
- [#2074](https://github.com/ditto-assistant/ditto-subnet/pull/2074) has useful
  domain-separated claimant signatures and payee binding;
  [#2075](https://github.com/ditto-assistant/ditto-subnet/pull/2075) has useful
  acceptance, approval, and receipt concepts; and
  [#2096](https://github.com/ditto-assistant/ditto-subnet/pull/2096) has a
  public board template. They remain separate draft work and do not confer
  spending authority. [#2056](https://github.com/ditto-assistant/ditto-subnet/pull/2056)
  is a duplicate placeholder. The issue epic is
  [#2054](https://github.com/ditto-assistant/ditto-subnet/issues/2054).

## Activation gates still open

1. Resolve whether the funding source is a miner-vector share, submission fees,
   or a combination, with explicit miner economics and Mog's operator-abuse
   concern reviewed publicly. This prototype models the requested small
   miner-vector share but activates neither source.
2. Review registered treasury hotkey and ownership, signer architecture,
   source-emission accounting, chain dispatch/quote semantics, fee schedule,
   and destination allowlist. Register the hotkey only after this review.
3. Review and deploy the isolated signer journal/runner and separate daily
   planner. The earlier JSONL still records **dry runs only**; the newer SQLite
   journal can drive bounded live SDK legs after explicit activation. Build a
   chain-verified ambiguous-dispatch recovery tool and a public spend report.
4. Reconcile GM account ownership and current payment instructions, test a
   human-linked wallet with a small reviewed payment, compare GM credit balance
   before/after, and publish a source-safe receipt. Keep GM API keys away from
   the treasury signer.
5. Finish bounty claim/approval/receipt contracts, a public spend report, and
   hard caps; stage validator rollout separately from the active screener and
   emission-eligibility recovery. No weight change accompanies this prototype.
