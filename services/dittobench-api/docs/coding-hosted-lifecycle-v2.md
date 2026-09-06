# Hosted v2 screened-harness lifecycle

Status: native lifecycle and source-router adapters with local regression tests.
The separate PostgreSQL start-store and local process handoff are documented in
[coding-hosted-start-worker.md](../../../apps/platform/docs/coding-hosted-start-worker.md).
No production worker, inference issuer, deployment, private dataset execution or
scoring path is enabled.

## Native start boundary

`codingharness.NewHosted` requires an explicit trusted `HostedStartStore`, the
screened-image runtime and a source registry. It rejects an ambient proxy
transport before loading an image. `HostedSandboxRuntime` uses the existing
rootless, isolated-daemon adapter with `RunRetainingFailedHandle`; the legacy
best-effort `SandboxRuntime` is explicitly rejected. A failed network/container
creation or address discovery preserves the exact generated cleanup identity
instead of discarding an unverified removal. Arbitrary injected runtime and store
implementations are trusted integration dependencies, not evidence of isolation.

`Acquire` validates evaluation, attempt, worker, assignment, artifact, image and
deadline authority. It verifies/loads the screened image through the existing
runtime without starting candidate code. Image capabilities are discarded after
loading. Active reservations reject duplicate evaluation/attempt/instance IDs.

`Activate` serializes concurrent calls, invokes `CommitStart` once and starts the
container only after a successful fresh-commit acknowledgement. The store must
reconstruct and compare the exact Platform assignment and commit its irreversible
start before returning true. Replay, denial, lost acknowledgement, cancellation
and late success never grant another launch. This interface does not itself
implement PostgreSQL transactions; `HostedStartCommand` supplies the concrete
Platform helper adapter when explicitly configured by the native worker.
There is no v1 ticket, legacy certification lease or numeric bench-version shim.

Once active, only native v2 health/seed/run methods are available. Seed/run must
match the evaluation, attempt and profile. Calls have the remaining assignment
deadline and are cancelled when the handle closes. Candidate HTTP reports remain
advisory, never a patch, grade or signed terminal result.
The handle permits only one valid run dispatch, including after transport failure
or response loss; retries cannot execute the candidate again.

## Source isolation and cleanup

`RegisterHosted` stores an independently typed binding including worker and
assignment digest. Native `/v2/coding/workspace/` and `/v2/coding/inference/`
capabilities require that exact binding and the Docker-observed direct source
address. Legacy publishers cannot resolve hosted registrations. Instance/address
uniqueness is shared across both versions, and old routes cannot follow a reused
address or textual instance identity after its registration closes. Forwarded
headers grant nothing. Inference publication mounts a trusted handler; it does
not issue a grant, select a provider, or authorize a model request.

Destroy denies new HTTP calls, cancels admitted calls, removes source authority,
and stops the exact container. Failed or partial starts retain any returned
container handle. Stop failure retains the handle and reservation for cleanup
retry, but can never authorize restart. Assignment expiry also triggers bounded
best-effort cleanup. Its timer is not durable recovery: the worker must retain
container identity and reconcile process/host failure without clearing start.

The caller MUST revoke and drain published workspace and inference handlers,
revoke the native upstream inference grant, and verify successful container stop
before committing freeze or admitting grading. Closing source admission cannot
cancel already-running tool/provider work by itself. The router serializes
source admission with route close so a concurrent close cannot race its private
source pointer. No cleanup failure may be reported as successful quiescence.

## Verification and next integration

Tests cover native seeding, concurrent activation, duplicate acquisition, durable
start replay/denial/lost acknowledgement, cancellation during commit, partial
start plus failed stop, cleanup retry, actual deadline cleanup, in-flight HTTP
cancellation, cross-attempt/worker/assignment denial, source spoofing, legacy
separation, address reuse and redacted diagnostics. These use synthetic images,
fake runtime/store adapters and local HTTP, not production Docker/Hippius/KMS.

Next: assemble the approved screened-image authority with the concrete start
handoff, connect verified private projections and scoped native inference to the
worker, then commit freeze, run pristine grading and seal terminal evidence.
The ten-task public pack, private corpus, embedding behavior, scoring and reward
configuration are unchanged.
