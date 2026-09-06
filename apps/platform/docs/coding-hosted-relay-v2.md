# Native source-bound inference relay

Status: concrete Go source relay and Python local-socket bridge, tested together
with the native provider adapter and real PostgreSQL. Provider responses remain
synthetic in tests. There is no startup/factory wiring, production key loading,
live provider call, benchmark activation, scoring, weight or emission change.

## Ownership and identity

The trusted Platform worker constructs `HostedRelayBridge` around its approved
`HostedProviderAdapter`, full `HostedRelayBinding`, a fresh random 32-byte local
credential and a mandatory asynchronous evidence-retention callback. The worker
owns a mode-0700 directory; `start` creates a new mode-0600 Unix socket and never
replaces an existing path. It rejects symlinked directories and wrong ownership.
The Go client independently checks socket/directory ownership and permissions.
Same-UID Platform processes and host administrators remain trusted; this is not
isolation from the machine owner. No socket directory or credential is mounted
inside a miner container.

`codinghostedrelay.Publish` uses the existing `codingsource.Router` and exact
`HostedBinding` registration. The registered address must come from the screened
container runtime. Only the direct socket peer can use the miner-facing route;
forwarded-address headers are not authority. The binding commits evaluation,
attempt, worker, grant, assignment, policy, artifact, harness instance, profile
capability and expiry. PostgreSQL checks its grant/assignment-owned fields; the
trusted runtime/source registry supplies container and profile identity.

The Python bridge verifies that binding against active native authority before
opening and before dispatch/output. The Go side opens the private bridge before
publishing the public capability. An ambiguous open or partial publication returns
a **non-nil cleanup handle with an error**: retain it and call `Revoke`. Do not
retry publication, regenerate a capability or recreate a started attempt. One
bridge incarnation accepts one successful open. This in-memory lifecycle is not
a replacement for the durable, one-shot worker start authority.

## Private framing and model policy

Each Unix connection carries one request and response:

1. Fixed `DITTO-HOSTED-INFERENCE-V2\n` magic and the 32-byte transport credential.
2. Big-endian uint32 length, then one bounded JSON command (maximum 6 MiB).
3. Big-endian uint32 length, then one bounded JSON result (maximum 12 MiB), then EOF.

Authentication happens before reading the command body. At most four connections
are admitted and authentication/body reads have a ten-second deadline. Commands
are `open`, `complete` and idempotent `revoke`; every command carries the binding
digest and request UUID. There is no TCP listener, arbitrary URL, proxy, redirect
or retry. Tokens and private envelopes must never be logged.

The Go route accepts only bounded JSON POSTs to `/chat/completions` and serializes
model requests. The UUID is worker-generated, not supplied by the miner. Two
intentional HTTP calls with identical chat text are separate budgeted requests;
replaying a private request UUID never dispatches twice.

The Platform locks the existing miner chat ABI using `HostedInferencePolicy`:
fixed prompt/tool digests, model/reasoning, provider route, no fallback, no storage,
single output/tool call and usage reporting. Miner provider, streaming, retry or
extra control fields are rejected. Only version-neutral chat shapes are reused;
no v1 ticket, grant, policy digest, settlement or retry authority is manufactured.

The native provider adapter reserves in PostgreSQL before outbound activity and
settles before returning. The bridge retains full private provider evidence via
the trusted callback and rechecks active authority before returning the normalized
response/settlement. The Go relay verifies response digest and native result
binding, then projects only the miner chat response. Neither provider evidence,
settlement, local credential nor provider key reaches the miner. The callback must
durably bind provider evidence to the settlement; an in-memory test collector is
not a production evidence implementation.

## Revocation and failure

`Relay.Revoke` closes local admission, cancels active transport, drains and revokes
the source route, then asks the private bridge to cancel/await its provider task
and revoke its ledger grant. Reader disconnect or invalid trailing input cancels
the Python task; no further provider request is admitted on that bridge after an
ambiguous failure. Concurrent cleanup must not interrupt an already-running
cancellation cleanup or mistake control cancellation for provider completion.

The private reply's `ledger_drained` describes only database reservation state.
A reserved/unknown request returns `ErrUnsettled` and keeps full budget ceilings;
it is not automatically zero usage, `mark_uncertain`, permission to freeze, or
permission to restart. Retain cleanup handles. Socket closure does not attest
remote provider quiescence. Any failure or missing evidence still prevents a
successful terminal result, even if a previously committed settlement makes the
ledger drain successfully. A successful revoke is not scoring evidence.

## Verification and next boundary

The Platform integration test starts the real private Unix bridge and a Go child
that publishes through the real source registry. Wrong-container/forwarded-source
requests are rejected; a correct request reaches the native adapter, synthetic
provider, real PostgreSQL reservation/settlement and evidence callback; revoked
URLs cannot be reused. Separate tests exercise malformed policy, replay, socket
permissions, mismatched responses, disconnects and cancellation accounting.

Next: wire the approved budget estimator/provider profile and durable evidence
publisher into the worker lifecycle, then complete private authoring, freeze,
grading and terminal-result orchestration. No production-capability claim follows
from these synthetic-provider tests.
