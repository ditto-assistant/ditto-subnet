# Service collector automation: staged runtime

This runner implements bounded automatic recovery after chain pruning and
periodic distribution of finalized **liquid SN118 alpha** to the configured
holding coldkeys, on the same collector hotkey. Vendor purchases remain manual.
It does not buy GM credits, convert alpha to TAO, register extra collectors, or
alter validator weights. The #2602 Backroom controls/Gamma projection remain
shadow; enabling this runner alone does not enable the 10% weight route.

Release does not install or enable either timer. No activation files, keys,
grants, host identities, policy signatures or chain transactions are included.
The existing legacy vendor-payment dispatch remains blocked.

## Custody and authority

- Primary collector coldkey stays offline with Peyton. Holding-wallet mnemonics
  stay with Peyton too. Neither enters a signer host or Backroom.
- Separate isolated GCE hosts, OS users and service accounts for registration
  and transfer. Exact instance names are
  `sn118-collector-registration-signer` and
  `sn118-collector-transfer-signer`.
- Each service account may access only its own fixed Secret Manager delegate
  secret/version. It must have no secret administration, broad secret access,
  project administration, impersonation, or other delegate access. Provisioning
  those principals/grants requires the separate action-time approval.
- Registration delegate has only `Registration`; transfer delegate has only
  `Transfer`. Both immediate, revocable grants; no `Any` or extra grants. The
  watcher reads public finalized chain state and has no signing credentials.
- The primary coldkey signs the immutable public policy digest with domain
  `ditto-collector-policy-v1:<sha256>`. Deployment independently pins that digest.
  Unknown JSON fields are ignored; only validated known fields are signed.
  Signatures authorize exact identities, policy revision, bucket split, caps,
  intervals, runtime, service accounts and numerical secret versions.
- A delegate mnemonic is loaded directly through GCE metadata authentication
  and Google Secret Manager REST into the short-lived process memory. No CLI
  secret output, disk wallet, primary key or native-KMS sr25519 claim. Core
  dumps are disabled. Host isolation, root-controlled code/config, least
  privilege, and offline revocation remain necessary.

**A stolen delegate can sign outside this application.** Registration scope
also permits `burned_register`; Transfer permits other destinations. Signer
checks are not chain-enforced destination or lifetime-spend limits. Do not
describe these proxies as hardware-constrained service wallets.

## Registration recovery

Every tick verifies genesis, audited runtime bytes and public filter API;
finalized `Owner`, `Uids` and `Keys`; non-owner collector association; narrow
grants; and raw absence of fee-sponsorship consent. A numeric UID alone never
authorizes a recipient. Re-registration retains the configured Owner. First
registration also permits a previously unowned hotkey, only in the registration
role with no SN118 UID and independently proven raw `Owner` storage absence at
the same finalized block. A decoded default address is insufficient. Existing
ownership, uncertain storage, subnet-owner association and transfer bootstrap
refuse. Finalized registration must establish the exact Owner and reciprocal
UID/Keys binding; its parent must prove absence of the SN118 UID.

Only `register_limit(118, collector_hotkey, limit_price)` is signed. Runtime
The audited runtime enforces the execution-time TAO price limit before payment. No fallback
to unbounded registration, no Utility batch and no nested proxy operation.
Each attempted dispatch conservatively consumes **max burn + max fee** of the
immutable lifetime budget, including proved failed/expired attempts. Cooldown
is enforced between attempts; registration cost and collector/delegate fee
reserves must pass before signing.

Registration is never chain immunity. The collector can be pruned again.
Registration automation cannot prevent pruning or guarantee a UID. It never
deregisters a miner. #2606 preserves collector exclusion in the competitive
planner even when service funding is zero; existing production weight routing
must still adopt the independently verified treasury role and immutable ledger
pin before funding. No automatic deregistration code currently exists here.

## Distribution and attribution

The distributor scans at most 32 finalized blocks per tick from an explicitly
approved starting block. It requires parent/payout UID/Owner consistency and
self-pinned `AutoStakeDestination[collector_coldkey,118]` at both ends. Setting
that self route is a separately approved offline-primary action; neither narrow
delegate may change it.

Gross `IncentiveAlphaEmittedToMiners` occurs **before** collateral capture and
auto-stake routing. It is not spend authority. The runner attributes only the
matching initialization `AutoStakeAdded` liquid credit to the exact collector
position, bounded by the UID's gross incentive. No liquid receipt means no
income; deposited principal and redirected/captured incentive never authorize
a distribution. Block hashes and event digests remain in the immutable-policy
journal even after all bucket transfers finish.

Current alpha availability uses the audited StakeInfo runtime API, specific
`MinerCollateral` and aggregate conviction/collateral availability. Deprecated
`Alpha` storage contains shares; StakeInfo's `locked` field is hardcoded zero.
Neither is used as a spendable amount. Locked funds are conservatively retained.

The existing service split planner conserves each earned batch using integer
rounding. Buckets total exactly 1,000 bps and have distinct holding coldkeys.
Only bounded same-subnet `transfer_stake` to those coldkeys is permitted; the
move-all sentinel, arbitrary calls, other netuids and other hotkeys are refused.
Each bucket is durably reserved and finalized once. Failed/expired distribution
batches stay quarantined for reviewed reconciliation rather than being retried.

## Durability and receipt semantics

Each signer owns a **separate private persisted journal**. Directory/file must
be owned by the executing user, with no group/other permissions or symlinks.
SQLite FULL/WAL plus `BEGIN IMMEDIATE` serializes claims. The exact signed bytes,
hash, policy, source block, bucket, nonce-containing payload and mortal lifetime
are committed before network submission. RPC timeout/crash never causes another
signature or automatic replay of those bytes.

Finalized blocks are scanned in bounded durable chunks. Successful outer
extrinsic status is insufficient: `ProxyExecuted` must contain a supported Ok,
with exact same-extrinsic phase, registration UID event or paired alpha
StakeRemoved/StakeAdded effects, and finalized identity/holding-position reads.
TAO-equivalent StakeTransferred amount and rounded quote deltas are not alpha
effect proofs. Fee evidence must name the approved delegate, tip zero, and
actual fee within the accounting cap. Unknown/missing evidence preserves the
claim and halts; proved mortality expiry requires every eligible finalized
block scanned.

**Fee limits are quote/receipt accounting gates, not an on-chain fee-price cap.**
The runtime-enforced registration burn cap is stronger than a fee quote. A
fee increase already charged at inclusion cannot be undone by receipt checking;
an over-cap receipt halts further operations. Keep delegate balances bounded,
monitor fees, and budget this separately from custody and revocation.

## Initial setup and recovery boundary

Sealed custody defaults to Google-only egress. Before a runner can observe Finney,
a separately reviewed custody plan may set `collector_runtime_rpc_egress=true`.
This requires both roles sealed and restores private Cloud NAT plus TCP 443 to
`65.109.251.221/32`, the `entrypoint-finney.opentensor.ai` IPv4 independently
resolved on 2026-10-03. It preserves own-version-only secret access, private
hosts and the private/other-traffic deny rules. DNS changes require a reviewed
source/plan update; do not broaden the firewall to recover connectivity. TLS
hostname, genesis, runtime and proxy-filter checks remain required. This is an
IP/port boundary, not a proof of confinement of every request to that server.
Network provisioning does not install a runner, approve a policy or enable a
timer. The binary-plan checker refuses a broadened RPC rule or unsealed roles.

The root-owned systemd templates are deployment preparation only. The enabled
policy must be approved and signed offline before initial journals are created.
As each dedicated signer user, initialize once using
`scripts/treasury_collector.py --role <role> --policy <public-envelope.json>
--policy-sha256 <approved-digest> --journal <role-private-directory>/journal.db
--initialize-journal`. This command performs no network/signing operation.

Recurring ticks **open existing state only**. Missing journal, changed policy,
lost volume, invalid state or wrong role fails closed. Never delete/recreate a
journal or restore a stale backup to repair a service: that would reset budgets
or replay already-distributed earnings. Reconcile exact signed hashes and
finalized effects before an independently reviewed state/policy migration.
This PR provides no policy migration or unresolved-operation override endpoint.

Before any activation: approved public collector/bucket addresses and split;
offline owner proof and non-owner chain binding; exact scoped delegates and
independent IAM/custody review; self auto-stake route; bounded funded reserves;
durable journals and loss/restart drills; independently verified validator
ledger-pin/UID routing; current runtime fingerprint; public/admin activity
ingestion and historical-policy observer; bounded production canary and receipts.
None of those live actions is performed by this PR.

## Verified contract sources

The active collector fingerprint is now the independently source-audited v472
`0x43bc67be9df30636d7e948e7bdb1ed065f2fb92029458cc939abf89d76d8ada3`.
See [the reconstruction audit](audits/collector-finney-v472/README.md) for exact
source/tree/patch, bounded srtool build and the isolated build-time hash-seed
constant difference. The unmodified rebuild is **not** byte-identical; every
other function body and section is identical. Collector contract sources below
were confirmed unchanged. A zero stake position still means zero available
alpha when the runtime omits its empty aggregate-map entry. All positive stake
and collateral checks remain required.

Historical v470 evidence:

Pinned SDK10.5.0 source:
`opentensor/bittensor@b9af04ad3452dde398460d464598837313226101`.
Finney runtime v470 source:
`opentensor/subtensor@923fd1fa7d6eadad3ec16f3941826b86c9c3aa1d`.
Official [v470 digest](https://github.com/RaoFoundation/subtensor/releases/download/v470/subtensor-digest.json)
and compressed WASM were independently downloaded and hashed; 2,556,358 bytes,
SHA256 `e5abec692e3988352da818823d9729f139820e205ea17048f93816974106c005`,
Blake2-256/live `:code` hash
`5675b684d69a07f6f224c2ba9cabef719804911fba40fbe1a2295198c9cb7c47`.
Finalized observation block 9,184,001,
hash `0x7eb21ba70db88c32f7bfb511c42c19a6665f4f6663b76e7a6ed1f872c1de89e5`,
genesis `0x2f0555cc76fc2840a25a6ea3b9637146806f1f44b090c175ffde2a7e5ab36c03`.
These are historical audit evidence, not a current deployment claim.

SDK's singular get_proxy_filter helper mismatches the live plural API;
the adapter uses public `ProxyFilterRuntimeApi.get_proxy_filters`. Narrow
filter sets are checked exactly and runtime hash drift stops the runner until
another source/artifact audit. This is a public chain API, not a private
Backroom/control-plane bypass.
