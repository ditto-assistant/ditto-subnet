# Private shadow coding attempt supervisor

`internal/codingsupervisor` defines the authenticated private control-plane
boundary between the Python validator coordinator and a future trusted Go
attempt backend. `ditto.validator.coding_supervisor.CodingSupervisorRuntime`
implements the existing `CodingAttemptRuntime` protocol against that boundary.

## Private wire

Every request fixes:

- one operation: `prepare`, `author`, `grade`, `abort_authoring`,
  `abort_grading`, or `recover`;
- a canonical operation UUID, ticket UUID, coding-run ID, and deadline;
- one bounded lease object when the operation requires it;
- the complete authoring outcome only for grading.

`prepare` asks the trusted process-local session backend to generate and retain one Ed25519
broker key and returns only its public key plus an opaque session UUID. Python
uses that public key to request/exchange the exact ticket-bound Platform grant.
Only `author` carries the active exchange object; the broker private key never
leaves the Go backend and the provider credential never enters either process.
Python revokes the exact active (or still-pending) grant generation in a
terminal `finally` path before it accepts the authoring outcome.

`SessionBackend` binds that preparation to the exact ticket/run, canonical
authoring lease object, and deadline. It lends a deep-owned private-key buffer
to the injected phase runner exactly once and zeros both the runner-visible
alias and its retained copy immediately after the call, including failure and
cancellation paths. A successful authoring or grading outcome is deep-owned
and cached, so response-loss retries return the exact evidence under the new
operation UUID without rerunning candidate code or pristine grading. A failed
phase is terminal and cannot receive a clean retry. Abort is idempotent for a
known session and zeros any key before delegating cleanup.

The production constructor always uses operating-system cryptographic entropy
for both key and session identity generation; entropy hooks exist only in the
package-private test constructor. Duplicate session IDs or public keys fail
closed without creating a second session.

The session table is bounded. It keeps terminal tombstones for the lifetime of
the process rather than evicting them into a possible clean retry. Close fails
while a phase is active and otherwise zeros retained private and evidence
buffers. Recovery only delegates to the durable phase runner; it never
calls authoring or grading.

The handler accepts only fixed `POST` paths, unencoded `application/json`, no
query string, a single exact bearer, bounded duplicate-free JSON, and coherent
operation-specific nullability. It stores only the SHA-256 of the configured
control token. One ticket/run pair is single-flight; a concurrent duplicate is
rejected before the backend runs.

Responses bind the same operation and authority. Authoring must prove both
capability revocation and environment destruction and return content-addressed
transcript/patch references. Grading returns one to 100 task-evidence objects
and a destroyed environment. Recovery can expose only a bounded state and, for
a pending publication, the exact stage/request digest. Backend errors are
projected to generic codes and never include task, provider, lease, or secret
text.

The Python client sends the existing scorer control token only to
`VALIDATOR_DITTOBENCH_API_URL` over HTTPS, or over HTTP to loopback during
local development. It disables redirects per request, bounds response bytes,
requires `Cache-Control: no-store` and JSON, and independently validates the
operation, operation UUID, ticket, run, and typed outcome before returning
coordinator dataclasses.
It independently checks the grant ticket, case/profile, policy digest,
deadline, request budget, and prompt/completion budgets against the authoring
lease before forwarding the active exchange.

## Current boundary

`internal/codingphase` now implements the injected `PhaseRunner` and commits
the durable activation marker before candidate execution. The runner itself is
still unwired: no composition root constructs its dormant-harness or inference
adapters, injects it into `SessionBackend`, mounts `Handler`, or constructs the
Python `CodingSupervisorRuntime` worker. The session state remains deliberately
process-local; authoritative transcript, patch, and publication bytes belong
to the durable outbox and gateway journals.

The next review is a separate disabled-shadow wiring PR. Until that lands this
stack cannot claim work, call a live miner, publish Platform evidence, affect a
score, or set weights.

Validation:

```bash
cd services/dittobench-api
go test -race ./internal/codingsupervisor
go vet ./internal/codingsupervisor

cd ../..
uv run ruff check ditto/validator/coding_supervisor.py \
  ditto/tests/validator/test_coding_supervisor.py
uv run mypy ditto/validator/coding_supervisor.py \
  ditto/tests/validator/test_coding_supervisor.py
uv run pytest -q ditto/tests/validator/test_coding_supervisor.py
```
