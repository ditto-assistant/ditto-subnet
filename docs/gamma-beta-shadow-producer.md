# Gamma treasury: optional finalized shadow observation

The served ledger contract can now be prepared with an actual read-only chain
observation. This component remains disabled by default and cannot route
weights, sign a transaction, provision a key or verify offline policy approval.

## Producer

`DITTO_TREASURY_SHADOW_POLICY_JSON` is an optional public proposal, bounded to
8192 bytes and validated with the immutable shared policy model. It contains
public account addresses and a collector-policy digest reference, never seeds
or delegate credentials. No environment value or deployment binding is created
by this change. Configuring this unsigned proposal does not approve funding.

Only materialization of a new epoch reads collector evidence. A read-only
Substrate client observes one finalized hash no later than the
ledger's pinned block and within its epoch, verifies genesis, and reads Owner,
SubnetOwner, Uids and reciprocal Keys at that same hash. Missing registration,
UID reuse, owner association, wrong chain, stale finality, hash disagreement
and bounded RPC failure refuse the observation. The producer timeout is eight
seconds. Errors cannot silently substitute another collector or policy.

The observed pin is shadow-only. Its known fields and identity are bound into
the existing immutable epoch context and ledger digest. Existing epoch replay
uses the stored pin; it does not reread a changed proposal. Shared snapshot
objects are not modified. With no proposal configured there is no collector
chain read and legacy ledger behavior remains unchanged.

## Operator visibility

Authenticated `GET /api/v1/admin/treasury-settings/ledger-readiness` and public
Backroom MCP `get_treasury_ledger_readiness` expose:

- configured public proposal;
- status scoped explicitly to this Platform process;
- latest stored epoch index/digest and validated shadow pin;
- bounded missing, corrupt or proposal-drift blockers.

The read does not materialize an epoch, query chain or write settings. A latest
stored row is not attested as the current epoch. Corrupt known pin evidence is
marked unavailable while its epoch/digest metadata remains visible. Offline
policy verification is false, weight effect is none, and enforcement readiness
is false even when a valid observation exists.

## Remaining integration

This is source preparation and a shadow observation producer, not activation.
Actual collector weight routing still requires an independently verified
offline policy, finalized identity revalidation, runtime adapter and explicit
all-fleet capability gate. Durable public receipt/activity ingestion and vendor
credit confirmation also remain separate work. Provisioning, binding this
proposal, registration, signing, transfers and live weight changes require the
user's separate action-time authorization.

## Optional offline proposal approval

The public proposal can separately carry an offline collector-coldkey signature.
Its domain is `ditto-treasury-emission-policy-v1:<canonical public policy digest>`;
the existing collector executor policy uses a different signature domain. A
signature cannot be reused to approve another action or modified known fields.
Unknown envelope/policy fields are ignored and never enter canonical authority.

Platform accepts this optional proof only with all three deployment inputs:
`DITTO_TREASURY_SHADOW_APPROVAL_FILE` (a bounded public JSON envelope),
`DITTO_TREASURY_APPROVED_POLICY_DIGEST` (immutable public emission-policy digest)
and `DITTO_TREASURY_COLLECTOR_POLICY_DIGEST` (immutable executor-policy digest).
The existing explicit observer proposal must exactly match the signed policy.
Missing/partial inputs, malformed proof, wrong signer, changed destination,
revision or chain, and mismatched deployment digests refuse boot. No environment
value, file, secret or deployment binding is installed by this change.

Readiness re-verifies the configured proposal's proof and reports
`proposal_approval_status` and `proposal_approved_policy_digest`. This is scoped
to the configured proposal in this process. It neither approves a stored epoch
retroactively nor authorizes registration, signing, spending or weights.
The existing `offline_policy_verified=false`, `weight_effect=none` and
`can_enforce_weights=false` remain unchanged for V1 shadow epoch pins. A shared
consumer helper also binds approval to a validated epoch's policy, chain,
recipient and block window, but does not query live finality or registration.
The active adapter, all-fleet gate and fresh pre-dispatch identity revalidation
remain required.
