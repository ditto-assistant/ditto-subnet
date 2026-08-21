# Shadow coding validator ticket sets

Platform can atomically bind one reconciled coding run to exactly three
currently permitted validators. All three tickets reference the same immutable
run and task-set authority while retaining distinct deterministic ticket IDs,
validator hotkeys, certifications, and deadlines.

The caller supplies one stable ticket-set UUID and a unique, lexicographically
sorted k=3 validator list. Platform resolves one current chain neuron snapshot
before locking the run and refuses any validator without a current permit. Each
ticket additionally requires that validator's exact artifact certification to
remain valid through the shared deadline.

For first issuance, `issued_at` comes from PostgreSQL `clock_timestamp()` and
the shared deadline is derived from the bounded lease policy. Callers cannot
choose or backdate lease time. Exact replay uses the immutable stored times and
does not depend on chain availability.

Ticket IDs are UUIDv5 values over the ticket-set UUID, run UUID, and validator
hotkey. Exact replay is idempotent. A partial pre-existing set, changed validator
membership, changed time authority, or a competing ticket-set identity fails as
an immutable conflict. A nested transaction rolls back all three tickets if any
member fails, so the set is never intentionally published partially.

## Activation boundary

This is an internal issuer only. It does not select validators, run in the
background, expose task or capsule bytes, grant Luna access, deliver work to a
validator, score results, deploy anything, or affect emissions. Coding contract
v1 remains permanently `weight_eligible=false`.
