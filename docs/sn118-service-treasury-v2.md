# SN118 service treasury v2: one collector and configurable service wallets

Status: **implementation under review; no production allocation, wallet, or payment applied**.
The existing v1 policy is shadow only, has two fixed fields and a 500 bps cap.
This proposal supersedes its 25 bps GM / 25 bps maintenance example. It does
not reinterpret any stored v1 revision or turn the current signer prototype on.

## Economic contract

Carve approved service buckets from the **full miner emission vector before
burn**. Let `S = sum(bucket_bps) / 10_000` and `B = burn_share`. Bucket `i`
receives `bucket_bps[i] / 10_000` of that vector. The remaining `1 - S` is
the virtual miner share: `B * (1 - S)` routes to the existing burn hotkey and
`(1 - B) * (1 - S)` goes to eligible miners. At today's `B = 1`, the combined
1,000 bps service pool would still receive 10%; the other 90% would burn. An empty
eligible miner vector burns its entire `(1 - S)` remainder while preserving
approved service allocations. A missing, unregistered, or unverified service
recipient fails closed under a separately reviewed policy; it must never
silently redirect that allocation to another service or to a miner.
Scoring recovery, screening admission, treasury allocation and burn changes
remain independent decisions.

For the confirmed combined service pool (`S = 0.10`):

| Burn of miner remainder | Services combined | Burn | Eligible miners |
| ---: | ---: | ---: | ---: |
| 100% | 10% | 90% | 0% |
| 50% | 10% | 45% | 45% |
| 0% | 10% | 0% | 90% |

These percentages are of the full miner emission vector. They are forecasts,
not current routing: the active validator still sends all emission to burn.

The confirmed service pool is **1,000 bps total**, split between GM, Bitsec,
Bitcast and later buckets. The exact initial split and public wallet addresses
await Peyton's inputs. The schema permits a smaller pool for a guarded first
activation, but never more than 1,000 bps combined. No service bucket may borrow
another bucket's allocation. The collector cannot also compete for miner payout.

## Wallet identity and custody

**Use one collector hotkey with three holding coldkeys, subject to custody
review before the active weight path is built.** The v2 shadow schema records
one collector hotkey/coldkey pair and a distinct holding coldkey for each
service. Validators would route only the reviewed aggregate service
share to the collector. After finality, a guarded, reconciled
[`transfer_stake`](https://github.com/latent-to/developer-docs/blob/main/docs/navigating-subtensor/subtensor-extrinsics.md)
could move each bucket's SN118 stake from the collector coldkey to its holding
coldkey while retaining the same hotkey. This would give each service a distinct
on-chain balance without adding three emission recipients or occupying three
registration slots. It introduces a collector custody window and requires
exactly-once sweep accounting, independent signer review, and a tested rollback.
Peyton creates and backs up each holding-wallet seed himself. Those seeds are
not GCP-managed and never belong in Platform, Backroom, screenshots, or PRs.
Holding wallets require no unattended signer: Peyton signs vendor payments.
The collector is a separate signing surface: an automatic sweep still needs
its coldkey available to a protected signer, with independent custody/recovery
review. Self-owned holding seeds do not by themselves authorize that signer.

Holding SN118 stake is not the same as a spendable TAO balance; a provider
payment would need a separate reviewed conversion and transfer.
This keeps the validator to one bounded treasury recipient and prevents
service wallets from occupying separate miner registration slots. The
alternative of one registered hotkey per service would remove the sweep step,
but adds validator recipients, registration slots, and independent signer
surfaces. Do not switch to that alternative without a separate review.

Under the recommended design, each purpose gets a dedicated, publicly
identified holding coldkey:

| Bucket | Purpose | Initial bps | Holding wallet | Spend destination |
| --- | --- | ---: | --- | --- |
| `gm_credits` | GM inference credit | pending split | dedicated coldkey holding swept SN118 stake | current GM Billing instructions |
| `bitsec_audits` | independent security audits | pending split | separate coldkey holding swept SN118 stake | approved Bitsec invoice |
| `bitcast_ads` | advertising campaigns | pending split | separate coldkey holding swept SN118 stake | approved Bitcast campaign invoice |

The collector hotkey must be registered on SN118 and independently verified
as owned by the collector coldkey, distinct from the subnet owner's burn
hotkey. The collector coldkey must also be distinct from the subnet-owner
coldkey, and finalized chain state must establish that the collector hotkey
is not owner-associated. A different hotkey under the same owner coldkey is
insufficient: [Bittensor's mining contract](https://www.bittensor.com/docs/guides/mining)
burns or recycles miner emission directed to owner-associated hotkeys.
The shadow settings do not prove this relationship; it is a mandatory
registration/ownership check in the future live weight adapter.
Each holding coldkey must be independently controlled and distinct
from the collector and the other holders. A wallet label or SS58 address alone
does not prove custody. Signing keys need separate access scopes; Platform and
Backroom retain no signing authority. The current single-key signer cannot
manage all bucket wallets without a custody and recovery review. Draft
host-activation PR #2327 stays dormant.

Historical personal payment research is not part of the public policy.
Configure a dedicated holding wallet and verify current vendor payment
instructions before classifying new service payments.

## Allocation, settlement and publication

1. A versioned policy lists stable bucket IDs, bps, registered receiving
   hotkeys, coldkeys, purpose, spending cap, and an independently approved
   revision. Unknown bucket IDs or missing wallet identity make a nonzero
   proposal invalid. A v1 revision remains subject to its original 500 bps
   cap; it is never upgraded by interpretation.
2. Validators must pin the exact policy revision and independently verify each
   recipient's registration and ownership before constructing weights. All
   serving validators must agree on recipients, rounding and the burn fallback.
   Any missing/stale recipient or policy discrepancy fails closed to the
   already reviewed miner/burn path, never to an arbitrary wallet.
3. Finalized chain receipts, validator weight telemetry, actual stake ownership
   and per-bucket balance form the source of spendable funds. A shadow quote or
   expected emissions cannot authorize spending. Every conversion, transfer,
   invoice and provider credit belongs to exactly one bucket and one immutable
   policy revision in a public receipt feed without leaking secrets.
4. GM's documented Billing flow currently requires the account owner to link
   the exact sender wallet and obtain **current** payment instructions. Its
   documented API exposes credit-balance reads, not a purchase endpoint. The
   existing daily timer may request a top-up, but unattended signing remains
   disabled until a documented, authenticated payment contract and reliable
   account-level reconciliation are demonstrated. An `X402-Relayed` row by
   itself does not establish that contract. Bitsec and Bitcast require their
   own reviewed invoices, payees, spending approvals and reconciliation rules.

The existing `ditto/treasury/store.py` journal is GM-specific (`gm_alpha_rao`,
`maintenance_alpha_rao`, and one top-up plan). Its `execute_one_leg` entry point
is deliberately blocked before signing. It must not be treated as a generic
three-service payment engine. Add a bucket-scoped, finalized-receipt sweep
journal first; keep each provider's payment adapter separate and blocked until
its own authenticated instructions and reconciliation proof exist.

## Configurable transparency

Backroom's **Emissions & treasury** page edits the collector, holding wallets,
bucket bps, distribution interval (1–168 hours), payment publication and exact
payee rules. The existing CAS revision and admin-activity boundary records
wallet/rule changes. Public addresses are syntax checked even in shadow mode;
this does not prove SS58 checksum, custody or chain registration.
Billing account references, private actors and reasons stay out of the public
allocation projection and public policy details.
Billing account references are optional for v2 holding-wallet allocation;
they become relevant to provider reconciliation, not collecting emissions
for later manually signed purchases. Legacy v1 payment prerequisites remain.

`GET /api/v1/public/treasury-allocation` and the dashboard **Gamma · Beta** page
show configured service allocations, a service-first forecast, collector and
holding addresses, and payment rules. They explicitly show effective service
funding, distribution and observation as inactive. The Beta page makes no
token issuance, redemption, or token-economic promise.

A rule matches the holding coldkey plus exact payee, asset and, for stake,
recipient hotkey. Disabled/ambiguous rules must not classify a payment. A
finalized transfer to GM's configured treasury is **GM credit payment**.
Only a matched provider receipt proves **GM credits confirmed** or its USD
amount. These are stages of one payment, not two purchases. Historical events
must retain the policy/rule revision in effect at their finalized block; a
new payee rule cannot rewrite old receipts. The existing treasury receipt
table is v1-specific and has no importer; a generic finalized payment observer
and bucket-scoped sweep journal are still required before activation.

`ditto/treasury/service_allocation.py` supplies tested service-first folding
and integer-conserving distribution planning without activating either live
path. The distribution input must be independently attributed collector
earnings, not its balance delta or principal. Production integration must
bind the fold to immutable fleet-supported ledger pins and the sweeper to
durable claims, finality proofs and explicit uncertain-outcome reconciliation.

## Activation sequence

1. Record the confirmed total-cap and denominator decisions publicly; review miner
   economics, custody, recipient identity and wallet recovery. Keep the
   policy shadow-only and all three bps zero in production while doing so.
2. Land backwards-compatible shadow-policy and read-only chain/receipt code.
   Prove the old 500 bps revisions retain their meaning and the new policy
   rejects duplicate buckets, repeated wallets and overflow. A shadow wallet
   string is a proposal, never proof of custody; the active weight path must
   reject every nonzero recipient until registration and ownership are verified
   on a finalized block. Hosted CI and an independent exact-head review are
   required before merge.
3. Review and register the collector and holding coldkeys through a protected
   ceremony, after an exact infrastructure plan and explicit action-time
   approval. Verify finalized
   ownership, signer isolation, recovery and public read-only visibility. Do
   not activate #2327 merely because its Terraform is valid.
4. Ship a validator implementation behind a default-off flag. Rehearse zero
   allocation, small shadow forecasts, rounding and every recipient failure
   path across every serving validator version. Rehearse the separate
   finalized-stake sweep and per-bucket reconciliation.
   Publish expected versus actual finalized receipts before positive routing.
5. Keep the burn setting and screening/scoring recovery independent. Activate
   one small, time-bounded service allocation with an immediate zero rollback,
   audit finalized funds and public receipts, then increase only through a new
   reviewed revision. At 100% burn, only the miner remainder burns; approved
   service allocations continue. No automatic service payment is implied by
   receipt of emissions.

No part of this design opens screening admission, changes GCE capacity,
adjusts live weights/burn, provisions a secret, registers a wallet, or pays a
provider.
