# Bounded manual collector requests

The manual core prepares a single transfer under the existing offline-signed
collector policy. It is not a generic wallet API, recurring activation, a new
destination approval, or a live Backroom-to-custody bridge.

Each public request file names a UUID, the exact previous operation ID, source
earning block, allocated bucket, alpha amount in integer rao, positive retained
stake floor, expiry block, and operator reason. Only an authenticated custody
operator may arm it. Request JSON alone grants no network access or authority.
Arming independently rechecks the prior successful chain effect and historical
earning, appends an event, and does not sign or broadcast. Unknown, failed,
expired, changed or stale prior claims refuse. The original failed canary and
proved replacement remain in the same journal unchanged.

Arming requires the SHA-256 of canonical request JSON (sorted keys, compact
separators) as `--confirm-manual-request`. The source must already exist in the
durable receipt journal. The amount cannot exceed its remaining bucket
entitlement, the signed maximum, or the available stake minus the retained
floor. Zero-allocated destinations refuse; arbitrary addresses are not accepted.

Execution requires the exact `--execute-manual-request` UUID and a private
`--selector-snapshot` path. Ordinary ticks may reconcile pending transactions
but return `manual_ready` without signing an unclaimed manual intent. Identity,
stake floor, maturity, spacing, fee cap and mortality are rechecked before the
exact signed bytes are committed, then broadcast once. Broadcast uncertainty
only permits reconciliation, not another signature. Terminal claims stop.

The private selector snapshot contains allowlisted public receipt coordinates,
not keys or signed bytes. Existing separate-user publisher/observer code can
durably deliver these to public Backroom receipt ingress. A snapshot is not a
published receipt; the independent historical chain checks must accept it.
Do not mark the operator control ready until that observer is deployed with
its dedicated normal-consent OAuth grant and a bounded receipt is visibly
published. No broad desktop token may be copied into the observer.

This change does not install a signer runtime, arm an intent, reset a journal,
enable a timer, provision OAuth/IAM, or move funds. Backroom control wiring and
public audit verification remain tracked in SN-54/SN-55/SN-57.
