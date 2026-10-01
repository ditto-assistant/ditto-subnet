# Gamma / Beta deployment and activation packet

Status: reviewable preparation, **not authorization to install or activate**.
Screening and Discord remain paused. Financial routing, custody, receipt
observation and screening admission are independent controls.

## Exact source and current evidence

Based on main `282b72141bf1bb61bae1283a94510411f6fcc192` (#2621), after
receipt-only OAuth #2620, ingress #2618 and enforcing adapter #2612. Coordinator owns successor
release verification. Code/release/settings saves are not adoption or earnings.

Public read-only UI observation, October 1, 2026 approximately 07:00 UTC:
`https://dittobench.ai/gamma` showed revision 0/shadow, configured/effective
service share 0%, no collector/payees, distribution and observer inactive.
This is a display observation, not authenticated readiness or chain identity.
The public `/activity` view subsequently loaded and showed "No verified
treasury spending is recorded yet." No settings, login, grant or receipt write
was performed during these checks.

| Runtime | Implemented | Remaining gate |
| --- | --- | --- |
| Platform/Pylon/validator | Offline approval, immutable V2 epoch policy/identity/fleet, service-before-burn weights, queued-dispatch fence | Deployment mounts/bindings, complete finalized setter roster, drain/adoption, financial activation |
| Registration signer | Bounded `register_limit`, separate delegate, durable budget/finality claims | Identity/custody/IAM/reserves, exact installed runtime/policy and action-time authorization |
| Transfer signer | Attributed liquid earnings and same-hotkey SN118 bucket transfers with durable claims | Self auto-stake route, limits/reserves, durable journals and separate activation |
| Receipt ingress | Independent historical policy/ledger/finalized chain/effect verification and atomic publication | Exact deployed ingress and bounded accepted receipt |
| Observer CLI | Finalized holding-payment scan, optional private journal export, durable queue/checkpoints | Approved config/credential and separate installation |
| New observer unit | Private credential binding, dedicated user/state, internal bounded reconnect, terminal semantic/auth halt | Proposed only; not installed/enabled; no signer/journal mount |
| Same-host selector handoff | Readonly signer-owner exporter, durable public pages, separate-user import and whole-page receipt ACK | Exact config/identities/permissions/one-time state initialization and action-time installation approval; not installed |
| Vendor payment | Human-held wallet can sign a separately approved payment | Current invoice/payee/conversion/reserves/action-time approval |
| Legacy payment CLI | `execute` deliberately blocked before key loading | Unsupported; do not unpause as part of this rollout |
| GM provider credits | Provider-credit ingestion refuses pending independent verifier | Matching provider verifier/receipt; TAO payment is only `vendor_payment` |
| OUR pruning | No dispatcher exists | No chain immunity/protection claim; competitive weight exclusion is separate |

## Existing public inputs to finalize

No addresses, split, UID, fees or delegate identities are chosen by this packet.
The pending inputs remain the existing question; do not repeat or invent them.

- Finney genesis/netuid118, fresh finalized block/hash and current audited runtime
  code hash/SDK identity. Runtime drift from reviewed v470 refuses.
- Collector hotkey and **non-subnet-owner** owning coldkey; finalized reciprocal
  `Keys`/`Uids` and `Owner`. A new hotkey under the owner coldkey is insufficient.
- Distinct Registration-only and Transfer-only delegates, immediate grants,
  no fee-sponsorship consent, offline primary revocation evidence.
- Stable bucket IDs, purposes, distinct holding coldkeys and exact integer split.
  Collector policy requires exactly 1,000 bps combined; emission policy caps at
  1,000. A smaller emission proposal cannot silently change the signed
  collector distribution split. Resolve this in the exact approved policy.
- GM/Bitsec/Bitcast custody and current invoice/payee rules. TAO and stake payees
  are different rules. Private billing references do not enter public evidence.
- Explicit finalized starting block, registration burn/lifetime budget,
  cooldown, distribution interval/max batch, fee cap/reserve.
- Separate GCE signer hosts/service accounts and fixed numerical delegate
  secret versions; exact collector policy/digest/offline approval and historical
  Platform revision/checksum/emission approval/digest.
- Complete finalized permitted setter roster, every validator/Pylon runtime,
  immutable descriptor/image pins, drain owner and rollback epoch boundary.

### Reserves and custody

Collector free TAO must cover the bounded registration amount plus
`fee_reserve_rao`. Each delegate requires `max_fee_rao + fee_reserve_rao`, and
reserve must cover the maximum fee. Every registration attempt conservatively
consumes max burn plus max fee from the immutable lifetime budget, including
proved failure/expiry. Choose approved limits; forecasts cannot authorize funding.

Distribution uses matched finalized liquid `AutoStakeAdded` earnings and
conservative available alpha, capped by `max_distribution_rao`. Gross emission,
collateral, deposited principal, balance delta and quote are not source proof.
Holding stake is not TAO for a vendor; conversion remains a separate human action.

Primary/holding seeds stay offline with Peyton. Registration and transfer live
on separate signer hosts/principals, each able to read only its own fixed delegate
secret version. Observer has no signing secret/account/wallet. Dormant legacy
treasury Terraform is not a collector host plan; review exact new infrastructure.

**Stolen delegates can sign outside these programs.** Registration scope includes
other registration operations; Transfer has no application destination/lifetime
cap on chain. Fee caps here are quote/receipt accounting, not on-chain fee-price
caps. Limited delegate balances, custody and offline revocation remain necessary;
revocation does not undo a finalized effect.

## Reviewed installation package — separate approval

Pin exact release commit/archive SHA, SDK lock/Python runtime, unit hashes,
public config/proof digests and image/descriptor identities. Use advertised Git
ref plus immutable checksum, not floating refs. Code/config must be root-owned.
No users/hosts/keys/secrets/grants/timers are created by preparing this package.

| Role | Public installation | Private state/credential |
| --- | --- | --- |
| Registration | `/opt/sn118-collector`, role policy/digest, `sn118-collector@registration.service`/timer | Own journal and own fixed Secret Manager delegate version |
| Transfer | Same exact runtime, transfer policy/digest, `sn118-collector@transfer.service`/timer | Own journal and own delegate version |
| Observer | `/opt/sn118-treasury-observer`, `/etc/sn118-treasury-observer/config.json`, root-owned activation.env with `CONFIG_SHA256`, proposed unit | Private queue; separately approved exclusive OAuth credential via `LoadCredential` |
| Platform/every validator/Pylon | Read-only public approval file and independently pinned digests | Existing identity only; no collector/holding keys |

The observer unit takes only the mounted credential **path** in `--token-file`.
No token goes in arguments, environment file, unit, packet, screenshot or log.
Input is no-follow regular file owned by root/executing user, no group/other
permissions, bounded to 8,192 bytes. Empty/malformed/symlink/FIFO/wrong-owner/
oversized and ambiguous file+environment bindings refuse without fallback.

Unit uses a dedicated user, 0700 state, restricted writable paths and disabled
core dumps. It runs a long-lived observer, not a repeating restart timer.
`Restart=no` preserves auth/policy/identity refusals; transport alone reconnects
internally with 15–300 second backoff. Absent activation.env skips the staged unit.
The CLI's disabled config returns before credential/state/network reads even if
a token path is supplied. systemd itself copies a `LoadCredential` before
launching the CLI: the unit's default-off boundary is the **absent activation.env**
condition. Do not create that activation marker merely for disabled staging
without approved credential delivery. Release never installs or enables it.

## Distribution selectors: separate-user same-host handoff

New watcher unit deliberately has no transfer-journal mount. Export requires a private
signer-owned directory/file; a different watcher user/host cannot read it.
Do not weaken ownership/modes or share signed payloads, full journal or secrets.

The transfer role can already export bounded public-only selectors read-only
using `treasury_collector.py --role transfer --export-activity` against its own
existing journal. Export signs nothing. Selectors are untrusted coordinates,
not finalized proof or spend authority. For one bounded demonstration, an
authorized operator can submit one exact selector through the dedicated public
`record_treasury_receipt` tool; Platform reconstructs independent source/effects.

`scripts/treasury_selector_publisher.py` implements a **same-host** public-only
handoff. Its proposed unit runs as the existing transfer-owner UID with a
read-only snapshot namespace, without loading a delegate or signing. The watcher
is a different non-root UID. Both have a separately approved read-only shared
group: outbox directory 0750 owned by publisher; acknowledgments directory 0750
owned by watcher; canonical files 0640, singly linked, regular and no-follow.
Private journals and queues remain 0700/0600 and are not shared. The exporter
has the transfer owner's OS identity; this is not isolation against a compromised
signer UID. The watcher cannot write the outbox or read the journal; the publisher
cannot write watcher ACKs. No users, groups, directories or permissions are
created by shipping this code.
The publisher shares the transfer custody principal, including any separately
granted same-host/cloud credential access; its read-only mount is not a separate
signer identity. The watcher binds no signing credential, but a distinct Unix UID
alone does not prove cloud metadata/service-account isolation on a signer VM.
Verify the approved deployment's identity/network boundary before calling it
keyless in operation; no live isolation or installation is claimed here.

The exporter uses `/var/lib/sn118-collector-transfer/selector-snapshot.db`, not
the original WAL journal. Actual read-only mount QA found that a closed WAL
journal can require SHM initialization and fail read-only. An optional
`sn118-collector@transfer.service.d/selector-snapshot.conf` hooks the existing
signer's successful durable tick to create an allowlisted private DELETE-mode
snapshot. It takes one consistent source read transaction over the exact pin
and all ordered transfer operations (including unresolved/failed/expired),
streams 1,000-row chunks with an explicit 100,000-operation ceiling, and copies
no signed/call/event payloads or keys. No journal data is altered. Fully closed,
committed DELETE-mode output has no WAL/SHM sidecars; file and directory fsync
surround atomic replacement. Active writer, checkpoint and closed writer sources
are supported without giving the publisher journal write permission or using
`immutable=1` to ignore WAL.

Snapshot failure reports `selector_snapshot_failed` and the already durable
`signer_status`, then halts that invocation. It does not retry the money tick.
An explicitly approved `--snapshot-only --selector-snapshot ...` observation
repair uses the existing private journal without chain/signing/money tick.
This flag initializes neither journal nor publisher cursor. A previous valid
snapshot remains on pre-replace failure; it may lag new money, never authorize it.
Publisher validates snapshot version/count/last operation, monotonic retained
history and every previously published selector's exact coordinates before new
admission. Missing/truncated/rolled-back or conflicting history refuses. The
100,000-operation limit and retained history require separately reviewed archival;
there is no silent compaction.

Public JSON config pins absolute directories, numerical UIDs/GID, historical
collector policy digest and 1–10 pending pages. Example **shape**, not deployable
identities or authorization:

```json
{
  "enabled": false,
  "selector_handoff": {
    "outbox": "/var/lib/sn118-selector-handoff/outbox",
    "acknowledgments": "/var/lib/sn118-selector-handoff/acknowledgments",
    "publisher_uid": 10001,
    "observer_uid": 10002,
    "shared_gid": 10003,
    "collector_policy_digest": "<exact-approved-64-hex-digest>",
    "max_pending_pages": 10
  }
}
```

Observer config adds this same `selector_handoff` object alongside its existing
offline approval, exact historical settings checksum, starting block and disabled
flag. Collector digest must match the approval. The handoff is included in its
private queue pin; adding/changing it cannot reuse a legacy queue implicitly.
This does not change existing observer pins when the handoff is absent.

Publisher retains each allowlisted page and its operation cursor together in
private FULL/WAL SQLite **before** exposing the page. Each tick admits at most
100 ordered selectors; more than 100 drains over subsequent ticks. At the
approved page cap it recovers ACKs/drains but admits no new cursor work. Failed
and expired operations are terminal barriers passed over; any unresolved earlier
transfer holds later operations. An unresolved operation behind the checkpoint
refuses and requires explicit reconciliation. Missing legacy coordinates refuse;
signed payloads are not read to invent them.

Pages bind version, collector policy and exact canonical content identity,
including journal operation IDs. Temporary file + file fsync + atomic rename +
directory fsync publish immutable content. A committed cursor with a missing
visible page is reconstructed only from retained private authoritative state.
An orphan public page without that state, cursor/page inconsistency, unknown or
conflicting ACK, ownership/mode/pin change or directory size overflow stops work.
No automatic initialization/reset or adoption of an orphan is permitted.

The watcher admits a complete page and its selectors atomically to its existing
private queue. It independently asks canonical public MCP ingress to reconstruct
each receipt's historical policy, finalized source and effect. Existing result
matching persists each exact accepted/replayed receipt ID before a page ACK is
possible. HTTP 200, partial acceptance and unknown delivery are insufficient.
Unknown delivery replays the same selector through idempotent ingress; already
durably accepted selectors are not sent again. Whole-page ACKs bind exact page,
policy, ordered operation/selector IDs and every durable receipt ID. They confirm
delivery only; neither this page nor its ACK grants financial authority or
provider-credit proof. No independent signature on the ACK/returned receipt is
claimed.

Publisher durably retains the ACK before pruning its page; restart finishes
pruning without replaying a financial operation. Watcher prunes its own ACK only
after the page disappears. Accepted selector/receipt rows and publisher page/ACK
history remain retained privately. Disk growth needs an approved archival/recovery
procedure; this implementation does not compact away delivery evidence. Stale
temporary files count toward the bounded 64-entry directory admission and can
halt it; repair only under an explicit recovery procedure.

Initialization is deliberate and once-only: exporter `--initialize-state --once`
and observer `--initialize-selector-state --once` with exact enabled configuration
and an approved fresh state path. Recurring units have neither flag. Missing or
changed existing state halts. Initialization refuses existing state/ACKs (exporter
also refuses nonempty outbox); never use these flags to recover lost state or
reset a budget. No activation command is executed by this packet.

For future installation, independently stage the proposed publisher unit and
optional signer snapshot and observer `selector-handoff.conf` drop-ins, adding
the observer's read-only group
plus ACK writable path. The base observer alone needs neither spool nor group.
Both default-off
activation markers remain absent. Configure exactly matching UIDs/GID and
root-owned immutable config/proof/runtime. Verify default-off CLI and unit parsing,
actual cross-user access refusals, >100 drain, late reconciliation, partial/unknown
ACK replay and each crash boundary before granting activation. Publisher reconnects
only classified public chain transport failures with 15–300 second bounded
backoff and retained private state. Runtime/policy, authentication/RPC semantic
and state conflicts halt (`Restart=no`); operator recovery does not retry money.
Observer retains its existing transient-only bounded reconnect. The manual
`--export-activity` command remains first-page-only; the new publisher is the
durable automatic route. **No cross-host adapter is implemented or claimed.**

## Default-off staging proof

1. Run exact archive disabled with nonexistent token file/no state/chain/signer.
   Require only `disabled`/authority `none`. Parse unit with test paths/users
   substituted; syntax proof is not installation or real systemd execution.
2. Exercise synthetic private credential and wrong owner/modes/symlink/FIFO/
   size/content negatives. Capture statuses, never content. Prove no fallback.
3. Verify actual receipt-only provider/SDK/direct-request refusals and ordinary
   OAuth issue/refresh/revoke compatibility (#2620). Exact historical read has
   no latest/default substitution; scope never becomes generic read/write.
4. Rehearse durable unknown ACK/restart/backpressure/finalized-hash drift,
   terminal auth vs transient retry and queue pin. Never replay money to repair
   observation. Preserve pending selections during stop/recovery.
5. Rehearse signer loss/restart/unknown dispatch, budget/failure/expiry/policy
   drift. Missing journal fails closed; initialize once only. Deleting/recreating
   or restoring stale signer state can reset budgets/replay earnings: refuse.

## Exact rollout, drain, fences and rollback

These are prepared actions, each requiring separate action-time approval.

1. Set exact public identities/buckets/payee rules via normal Backroom CAS.
   Retain returned historical revision/checksum. Obtain separate offline
   collector/emission signatures and independently compare all fields/digests.
2. Install exact public proof/digests on Platform and **every** validator/Pylon.
   Config seams: `DITTO_TREASURY_SHADOW_APPROVAL_FILE`,
   `DITTO_TREASURY_APPROVED_POLICY_DIGEST`,
   `DITTO_TREASURY_COLLECTOR_POLICY_DIGEST`; Platform also resolves
   `DITTO_TREASURY_SHADOW_POLICY_JSON`. Review per-runtime env/mount wiring;
   these names do not prove existing compose/Ansible already supplies them.
3. Drain legacy weight work at the agreed epoch boundary. Arm each Pylon
   `DITTO_TREASURY_WEIGHT_ENFORCEMENT=true` fence; cached/new/queued V1 refuses.
   Verify matching capability and fresh signed protocol-30 heartbeats for the
   complete finalized permitted roster, including stale/rejoining setters.
4. Only then authorize Platform enforcing producer/new immutable V2 epoch pin.
   Require signed receipt and normalized vectors bound to that pin. No V1
   fallback, existing shadow epoch rewrite, burn/admission change or subset gate.
5. Rollback stops new work, drains/reconciles tasks/claims and preserves journals.
   Never disarm a Pylon fence while V2 epoch/queued V2 work remains. Review next
   epoch/complete roster; no silent legacy reinterpretation. Drift halts until
   audited rebind, not operator override.

## Shortest path to visible finalized earnings/distribution/payment

1. Resolve existing public inputs/custody/limits. Review/authorize exact infra
   and offline-primary ceremonies. Verify finalized non-owner collector and
   self auto-stake route; no UID/immunity shortcut.
2. Install isolated pinned signers/policies, initialize each journal once,
   fund bounded reserves, prove narrow grants/revocation/loss drills before
   separately enabling registration/distribution ticks.
3. Complete all-fleet drain/fences/V2 pin and separately authorize service route.
   Observe actual attributed finalized liquid earning, not forecast/weight alone.
4. Permit one bounded bucket distribution. Retain source earning, exact
   block/hash/extrinsic index/hash/effects/historical pin. Submit one exported
   selector via separately approved **exclusive** receipt-only consent. Verify
   accepted receipt, private persistence and public `service_distribution`.
5. Obtain current invoice/payee and separately authorize Peyton's manually
   signed holding-wallet payment/conversion. Observer may publish verified TAO
   transfer as `vendor_payment`; funding/conversion attribution remains unproved
   unless separately verified. No legacy payment CLI or GM-credit claim.
6. Verify historical publication policy and exact accepted identities on public
   activity. GM credits/USD need future independent provider evidence.

No earnings ETA exists while identity/funding/routing are inactive. Approval of
this packet/code does not authorize installation, consent, secret/IAM/proxy,
registration/funding, policy/weight writes, observation or payment.
