# SN118 treasury activation and GM top-up runbook

This is a proposed operator sequence for the four draft PRs. **None of these
activation steps has been performed.** The Platform policy is shadow-only, the
Terraform signer and planner hosts are disabled in `prod.auto.tfvars`, and the daily timer is
only a template. A review of this runbook does not authorize a chain transfer.

## Initial allocation proposal for economic review

Propose **25 basis points of the released miner vector for maintenance bounties
and 25 basis points for GM credits**: 0.25% each, 0.50% together. The separate
budgets are not interchangeable. This is 50 bps of the code's 500 bps hard
ceiling and, at the usual 41% miner share, about **0.205% of total SN118 alpha
emission**. Publish this proposal before any policy write or weight routing.
It gives miners 99.5% of the released miner vector, subject to any separate
burn setting and eligibility rules.

Backroom burn revision **8** (read 2026-09-25) currently has `burn_share=1` and
`miner_emission_share=0`. Under that live setting, both proposed budgets accrue
**zero**. Do not change the burn setting as part of treasury activation. A
separate emission-recovery decision must establish what portion of the miner
vector is released, then remeasure actual finalized treasury receipts before
setting any spending cap.

For scale only: finalized Finney block 9,147,019 showed `alpha_out_emission`
of 1 DITTO alpha per block. At the observed 12-second block interval, 7,200
blocks/day and a 41% miner share would produce about 2,952 DITTO alpha/day in
the miner vector **if fully released**. Each 25 bps budget would then receive
about **7.38 DITTO alpha/day** (14.76 together). At finalized block 9,147,038,
the read-only SDK quote for a single 7.38 DITTO alpha sale returned
**0.053972522 TAO**; routing that TAO through SN28 returned
**2.397199384 GM alpha**. These are alternative paths for the GM budget,
not additive proceeds. The price, block rate, emission, 41% split, and GM
deposit conversion can change. These figures omit network fees and the USD
credit credited by GM at confirmation. The 7.38 DITTO example fits below the
signer's hard 10 DITTO alpha source cap, but an actual payment remains bounded
by finalized GM allocation, current billing instructions, a fresh quote,
separate review, and the live policy's tighter limits.

## 1. Approve the funding source and custody

1. Resolve the miner-economics and operator-abuse questions in the SN118
   discussion. Publish a small GM share and a separate maintenance-bounty
   share with their purposes, ceilings, and public spend reporting. Keep both
   at zero until that decision is accepted.
2. Register a dedicated SN118 treasury hotkey under the reviewed, non-owner
   coldkey. Independently verify registration and ownership on the finalized
   chain. Review validator weight routing and Platform ledger classification in
   a separate PR. A Backroom shadow revision alone changes no emission.
3. Review and merge the custody and signer code only after the chain operation,
   SDK version, fee reserve, GM recipient, and recovery model are understood.
   The existing 2-of-3 proposal in #2119 is not an approved replacement for
   this single-key prototype.

## 2. Review an exact infrastructure plan

The protected `Infrastructure plan or apply` workflow checks out **main** and
seals its binary plan in private GCS. It cannot plan an unmerged draft. A
follow-on activation PR must set `enable_treasury_host = true`,
`enable_treasury_planner_host = true`, and the exact reviewed
`treasury_operator_email` in `infra/terraform/stacks/gcp-platform/prod.auto.tfvars`.
Keeping that intent in the file prevents the next routine plan from proposing
host deletion. After that PR is reviewed and merged, run the protected
`gcp-platform` **plan** without `-target`; compare every proposed resource,
IAM binding, route, and unexpected deletion. Only a separately approved
`infra-apply` run may apply the exact sealed plan SHA and run ID. Do not use a
local `-backend=false` plan as evidence against production state.

The plan creates two private Shielded VMs with separate service accounts. The
signer alone can access the signing-key secret; the planner alone can access a
GM read-key secret. The plan also creates an **unbound** key-provisioner role.
It creates no secret version, wallet, GM credential, or timer. No Platform,
Backroom, CI, or planner account may read the mnemonic. Host runtime
installation and image pinning need their own reviewed deployment procedure.

## 3. Create and link the wallet once

On the named signer VM, temporarily bind the provisioner role **only on the
treasury secret** to its service account, run `scripts/treasury_create_key.py`
with its exact confirmation, then run `scripts/treasury_verify_key.py` with
`--project PROJECT --expected-address PRINTED_SS58_ADDRESS` **on the same
signer host**. It reads pinned Secret Manager version 1 and re-derives the
public address without printing the mnemonic. Independently check that the
reviewed address is registered and owned on the finalized SN118 chain; record
only that public address and finalized identity evidence. Remove the temporary
provisioner binding and re-read the secret IAM policy to prove the adder grant
is gone. The signing runner pins Secret Manager
version **1**; rotation needs a separate code/config review. Never copy the
mnemonic into a PR, terminal transcript, plan, Backroom setting, or GM account.

The one-time ceremony uses these exact command shapes after substituting the
reviewed project and public address. `PROJECT` is the GCP project ID. The
secret IAM change is deliberately scoped to the **one secret**, and the
temporary role must be absent from its final policy:

```bash
gcloud secrets add-iam-policy-binding sn118-treasury-signing-key --project=PROJECT --member=serviceAccount:sn118-treasury-signer@PROJECT.iam.gserviceaccount.com --role=projects/PROJECT/roles/sn118TreasuryKeyProvisioner
uv run python scripts/treasury_create_key.py --project PROJECT --confirmation 'CREATE SN118 TREASURY KEY'
uv run python scripts/treasury_verify_key.py --project PROJECT --expected-address PRINTED_SS58_ADDRESS
gcloud secrets remove-iam-policy-binding sn118-treasury-signing-key --project=PROJECT --member=serviceAccount:sn118-treasury-signer@PROJECT.iam.gserviceaccount.com --role=projects/PROJECT/roles/sn118TreasuryKeyProvisioner
gcloud secrets get-iam-policy sn118-treasury-signing-key --project=PROJECT --format=json
```

The `gcloud` binding commands run as the reviewed IAM operator. The two Python
commands run **on the signer VM**; do not copy the mnemonic or secret payload
through SSH. Inspect the final IAM JSON for the absence of the temporary role
and retain only the public address, version number, policy evidence, and chain
ownership proof. If any step fails after version 1 is written, stop and review
recovery; never run `create_key.py` again to make version 2.

The GM account owner signs in to [GM Billing](https://saygm.com/dashboard/credits),
selects CLI wallet, links that exact public sender through Taostats Auth, and
checks the link in the correct Omni Aura account. GM says the link persists and
can also sign into the account. Before **each** payment, the account owner
reopens Billing and copies its current TAO recipient or SN28 alpha transfer
instructions. A stale address, different source, subnet, or hotkey aborts the
plan. A dedicated GM API key is used only for credit observation and belongs on the
separate planner host, never the signer.

## 4. Install the daily request timer separately

Provision a least-privilege planner host with no signer-key access and install
the pinned runtime plus `infra/systemd/sn118-treasury-daily.service` and
`.timer`. Create the `sn118-treasury-planner` user and private outbox. Put a
dedicated GM API key in the host's systemd credential source and a reviewed
`daily-policy.json` under `/etc/sn118-treasury/`. Its fields are
`gm_account_ref`, `route` (`tao` or `gm_alpha`), `floor_nano_usd`,
`target_nano_usd`, `source_alpha_rao`, and `max_source_alpha_rao`. The code
rejects a source cap over 10 DITTO alpha. Enable the timer only after a dry
read of `GET /v1/credits` and an outbox permission check. It writes at most
one request per UTC day when credit is below the floor; it never signs.

GM documents no API for purchasing credits or fetching the current Billing
recipient. The timer therefore cannot safely complete an unattended payment.
Its outbox must be monitored by an operator; a due request is an input to a
reviewed plan, not approval to spend.

## 5. Construct a reviewed top-up plan

1. Read the **current** Backroom treasury revision and check its GM account,
   sender, daily and single-payment limits, maximum slippage, and zero/active
   emission status. Verify the epoch's finalized treasury inflow against chain
   and ledger evidence. Write an `allocation.json` with `epoch`,
   `policy_revision`, `source_block_hash`, `gm_alpha_rao`,
   `maintenance_alpha_rao`, `operator`, and distinct `reviewer`. Record it in
   the signer journal; only GM-allocated alpha can fund a GM plan.
2. Read GM's current credit balance, recent wallet deposits, and intervening
   usage. Read the current Billing instructions with the linked account. Put
   exactly `asset`, `source`, `destination`, `hotkey`, and `account_ref` in an
   `instructions.json` file; the TAO route has an empty hotkey. Record its
   canonical SHA-256 in the payment plan. Different instructions require a new
   plan.
3. Run `scripts/treasury_live_quote.py` for the selected source amount. Bind
   the finalized block hash, quote time, TAO output, optional SN28 GM-alpha
   output, price impacts, and conservative minimum outputs to `plan.json`.
   Choose TAO or SN28 alpha using the fresh on-chain quote and GM's current
   terms. GM's eventual USD credit is unknown until deposit confirmation.
   `plan.json` has `intent` (`route`, `policy_revision`, `quote_block_hash`,
   `quoted_at`, `source_alpha_rao`, `tao_value_rao`, `price_impact_bps`,
   `linked_wallet`, `payment_instructions_sha256`, `idempotency_key`) plus
   `destination_coldkey`, `treasury_hotkey`, `gm_hotkey`, `gm_account_ref`,
   `operator`, `instructions_observed_at`, `gm_balance_before_nano_usd`,
   `min_tao_proceeds_rao`, `min_gm_alpha_rao`, and `max_slippage_bps`.
4. Put policy-derived `max_source_alpha_rao`, `max_single_topup_rao`,
   `max_daily_outflow_rao`, and `max_slippage_bps` in `bounds.json`, along with
   the linked sender, instructions digest, and an explicit reconciled flag.
   The signer recomputes daily spending from its journal and enforces the hard
   10 DITTO alpha source ceiling. A distinct reviewer checks all three files,
   the current Backroom revision, GM account, route, and allocation evidence.

The signer journal starts paused. Its operator CLI takes files rather than
inline transaction bodies. The sequence, with paths substituted only after
review, is:

```bash
uv run python scripts/treasury_payment.py --database /var/lib/sn118-treasury/treasury.db status
uv run python scripts/treasury_payment.py --database /var/lib/sn118-treasury/treasury.db allocate --allocation-file /secure/reviewed-allocation.json
uv run python scripts/treasury_payment.py --database /var/lib/sn118-treasury/treasury.db unpause --operator OPERATOR --reviewer INDEPENDENT_REVIEWER --reason 'reviewed exact first payment and chain evidence'
uv run python scripts/treasury_payment.py --database /var/lib/sn118-treasury/treasury.db propose --plan-file /secure/reviewed-plan.json --bounds-file /secure/reviewed-bounds.json --instructions-file /secure/current-gm-instructions.json
uv run python scripts/treasury_payment.py --database /var/lib/sn118-treasury/treasury.db approve --key PAYMENT_KEY --plan-hash EXACT_PLAN_SHA256 --reviewer INDEPENDENT_REVIEWER
```

## 6. Execute one leg and reconcile before the next

The `execute` subcommand is confined to the named signer VM. It requires an
exact `EXECUTE SN118 TREASURY PAYMENT_KEY` confirmation. The runner commits
`dispatch_started` before calling the Bittensor SDK and requests finality.
For TAO the legs are SN118 unstake then TAO transfer. For GM alpha they are
SN118 unstake, SN28 stake, then SN28 same-hotkey stake transfer. After each
successful nonfinal leg, inspect the finalized extrinsic and actual balances,
obtain a new finalized quote and current Billing instructions, and run
`approve-next` with a different reviewer. Both the quote and instructions
expire; the next leg cannot auto-run from the first approval.

```bash
uv run python scripts/treasury_payment.py --database /var/lib/sn118-treasury/treasury.db execute --key PAYMENT_KEY --project REVIEWED_GCP_PROJECT --instructions-file /secure/current-gm-instructions.json --confirmation 'EXECUTE SN118 TREASURY PAYMENT_KEY'
uv run python scripts/treasury_payment.py --database /var/lib/sn118-treasury/treasury.db approve-next --key PAYMENT_KEY --plan-hash EXACT_PLAN_SHA256 --reviewer INDEPENDENT_REVIEWER --instructions-file /secure/current-gm-instructions.json --quote-block-hash FINALIZED_BLOCK_HASH --quote-observed-at 2026-09-25T20:00:00+00:00
```

Run `execute` again only after `approve-next` and a fresh preflight for that
specific leg. Replace the example quote time with the actual UTC observation;
the code rejects values older than 120 seconds.

If a call errors, times out, lacks a finalized receipt, or yields an unexpected
amount, the journal pauses and remains `dispatching`. **Do not retry.** Read
the finalized chain and prepare an independently reviewed recovery change.
The current CLI intentionally has no automatic retry or manual shortcut that
claims a missing receipt succeeded. Do not unpause with an ambiguous or
unreconciled plan.

After the last finalized deposit leg, match GM Billing's recent-wallet-deposit
row to the chain receipt. Read `GET /v1/credits` again and account for
intervening inference usage. `reconcile` requires the before/after balances,
credited deposit nano-USD, usage nano-USD, Billing deposit reference, and an
independent reviewer; the arithmetic must match exactly. A balance delta by
itself does not prove attribution. Publish a source-safe receipt and keep the
signer ledger private. The bounty budget has separate governance and does not
inherit GM payment authority.

```bash
uv run python scripts/treasury_payment.py --database /var/lib/sn118-treasury/treasury.db reconcile --key PAYMENT_KEY --before-nano-usd BEFORE --after-nano-usd AFTER --deposit-nano-usd CREDITED_DEPOSIT --intervening-usage-nano-usd USAGE --deposit-reference GM_BILLING_ROW --reviewer INDEPENDENT_REVIEWER
uv run python scripts/treasury_payment.py --database /var/lib/sn118-treasury/treasury.db status
```
