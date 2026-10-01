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
