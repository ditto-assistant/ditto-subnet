# Gamma Beta enforcing weight adapter

This adapter implements service-first weights behind an explicit, default-off
boundary. Shipping the code, an image or an offline proposal signature does not
activate it. This change installs no proposal file, deployment binding, key,
collector registration, proxy permission, funding, weight or settings action.

## Authority and compatibility

V1 ledger treasury observations remain shadow-only. A missing treasury field
preserves the historical ledger digest and receipt wire. V2 binds the exact
offline-signed public emission policy, its independently pinned collector-policy
digest, collector identity, epoch/block/hash and a sorted nonempty runtime fleet.
The approval message remains `ditto-treasury-emission-policy-v1:<policy digest>`.
The signature is checked with a public-only sr25519 key; no wallet is loaded.

The Platform producer is enabled only by
`DITTO_TREASURY_WEIGHT_ENFORCEMENT=true`, alongside the existing explicit proposal,
public approval file and immutable policy/collector digests. An unavailable
epoch pin, an old V1 pin, a corrupt pin or a failed producer cannot fall back to a
live unpinned ledger when enforcement is configured. Existing epoch rows are
immutable: turning on a producer does not rewrite a shadow epoch into V2.

The all-fleet gate starts with finalized `ValidatorPermit` storage. For every
permitted UID it reads `Keys` and reciprocal `Uids` at that same observation hash;
missing, malformed, duplicate or nonreciprocal identity refuses. Every permitted
setter, including an unscored or stale/rejoining validator, needs a fresh signed
protocol-30 heartbeat with both exact policy digests and V2 dispatch support.
Fresh extra runtimes must also support that policy. A subset of heartbeats cannot
stand in for the chain-active roster. Ledger requests recheck current finalized
identity, epoch and roster against the immutable fleet before serving V2.

The validator obtains capability from the authenticated identity-scoped Pylon
endpoint, never an image label. The Pylon capability is absent until its own
default-off enforcement fence is armed and it independently verifies the exact
public approval and deployment digests. Partial configuration refuses; it does
not advertise a capability. Heartbeat signatures cover the capability fields.

## Allocation, receipts and queued dispatch

The collector is excluded from competition before both memory and router folds,
including when its service share is zero. The canonical fold reserves the fixed
service basis points first, applies burn only to the remaining miner pool, and
burns empty or unpaid competitive shares. The legacy burn cap is not applied a
second time. The shared arithmetic is also used for Platform receipt/reveal
comparison. V2 can issue a signed receipt when there is no competitive champion;
such a receipt does not manufacture competitive winner attribution.

V2 receipt IDs/digests/signatures bind the exact treasury pin. Unsupported,
missing, timed-out or malformed receipt transport returns refusal; it cannot
invoke the legacy setter. Pylon repeats finalized identity/epoch checks before
hotkey-to-UID translation and immediately before committing the normalized
vector. UID reuse, owner drift or an epoch rollover terminates the queued attempt.
Integer rounding has a bounded allocation check; a positive pool cannot disappear
and a paused pool must receive exactly zero. Direct non-commit/reveal submission
is refused on the enforcing path.

An armed Pylon fence also refuses already queued legacy tasks and new V1 requests.
Thus an old validator with a cached V1 ledger cannot use this configured transport
to bypass the enforcing contract. The fence protects this deployment's dispatch
path. It cannot prevent a separately controlled key from submitting directly to
the chain; key custody, chain permissions and fleet rollout remain separate gates.

## Operational boundary

Deployment and activation require separate operator authorization. A reviewed
rollout must drain legacy dispatch, install and verify the same public proof and
immutable digests on every authorized setter transport/validator, arm the
transport fences, and observe fresh signed protocol-30 capabilities for the
complete finalized roster. Only then may a separately authorized Platform
enforcing producer create a new immutable epoch pin. Arming can deliberately
stop legacy weights; do not treat a capability flag alone as a safe rollout.

Read-only readiness preserves V1 shadow evidence and adds the stored V2 pin,
enforcement configuration, fleet gate and current epoch verification. A fresh
heartbeat subset alone leaves the complete fleet gate unchecked. Weight
readiness requires actual offline crypto, current finalized observations and
all-fleet equality; it does not grant transfer, registration, pruning, signing or
spending authority. Collector exclusion here concerns competitive weights only,
not chain immunity or a pruning implementation.

Do not disable a transport fence while a V2 epoch or queued V2 task remains in
use. A rollback needs a reviewed epoch boundary and drained tasks; no consumer
silently converts V2 to legacy arithmetic. Public activity ingestion, finalized
collector attribution, sweep execution, vendor credits and OUR pruning exclusion
are separate integration work. Synthetic Alice/Bob fixtures prove test contracts,
not any live collector, operator approval or funding state.
