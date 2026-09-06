# Native Platform authoring coordinator

For the subsequent native grading and executable worker composition, see
[the one-attempt launcher](coding-hosted-runtime-v2.md). The scope below describes
the original authoring coordinator, not the entire current hosted execution path.

`internal/codinghostedworker` joins the native harness lifecycle, verified input
consumer, resource-bound workspace, source router and inference relay into one
authoring attempt. Its public constructor uses those concrete components.
Platform-owned `Control` adapters provide the private retrieval, inference
bridge, lifecycle checks, evidence retention and database freeze operations.

This layer ends at a committed patch-freeze acknowledgement. It does not
implement those Platform control adapters, a worker process entry point, grading
input conversion, terminal evidence publication or a signed validator result.
There is no deployment or startup caller, no private-data distribution, and no
scoring or reward activation. Contract v1 remains separate and shadow-only.

## One attempt

1. Acquire the exact screened image, verify the source binding and activate the
   native harness. Its start-store adapter commits the irreversible start before
   the container runs. Same-attempt replay cannot launch another candidate.
2. Check native health and retrieve verified authoring inputs through Platform.
   Compare their evaluation, attempt, worker, assignment and deadline against
   the harness binding before creating the resource-bound workspace.
3. Seed memory, then publish the workspace and native inference routes from the
   observed container source. Partial route/bridge handles remain owned even if
   publication returns an error. Provider keys stay inside the Python bridge.
4. Run once, bounded by both the assignment deadline and the projected run's
   wall-time budget. Check current authoring authority between stages and at
   one-second intervals during authoring. A failed check cancels candidate HTTP
   work. The control adapter must honor its five-second polling deadline.
5. Revoke and drain workspace and relay routes, revoke the private bridge and
   stop the exact container. Every revocation/stop is attempted even when another
   fails. Successful cleanup boundaries are remembered for retry. Keep the bridge
   command socket until these operations succeed, then dispose it.
6. Freeze the native workspace and retain its exact patch, transcript and run
   classification through the Platform evidence adapter. That adapter must verify
   complete inference evidence before acknowledging a successful authoring run.
7. Commit only the retained patch and verify the returned evaluation, attempt,
   assignment and patch digest against independently reconstructed PostgreSQL
   authority. Only then close the local workspace and return the replay authority.

The returned authority is a grading prerequisite, not proof of grading, signed
result publication or success on a private task. The `Control` implementation is
trusted: accepting a self-reported digest or an in-memory evidence collector in
production would violate this contract.

## Adapter requirements

| Control operation | Required Platform binding |
| --- | --- |
| `Authoring` | Existing `HostedAuthoringInputAssembler` and private frame verification; no caller-selected task or object |
| `Inference` | Native grant, policy-bound budget estimator, source-bound `HostedRelayBridge`, durable inference-evidence publisher |
| `CheckAuthoring` | Fresh assignment, worker, active release/artifact, authoring phase and deadline checks |
| `Retain` | Durable encrypted patch/transcript/failure evidence and complete inference-evidence commitment; exact replay |
| `CommitFreeze` | `freeze_hosted_private_patch` in a committed transaction, bound to retained bytes; independently verified acknowledgement |
| `Abort` | `close_hosted_private_task`, including after expiry or release retirement |

`Bridge.Revoke` must cancel/await active provider work and revoke/drain its grant
while preserving the local command socket. `Bridge.Close` releases that socket
only after the relay and other physical cleanup complete. Both must be
idempotent and retain handles on ambiguous failure. Each cleanup operation has
its own 30-second context so a failed operation cannot consume every later
operation's deadline. A caller must keep the `Attempt` until cleanup is verified.

## Failure and replay

`Run` is single-use, including after failures or cancellation. `Cleanup` cancels
an active run, retries pending cleanup and removes private-object access. A
cleanup failure never permits a committed freeze. A successful cleanup before
the first run permanently closes that attempt as well.

Once candidate execution and physical cleanup succeed, ambiguous retention or
freeze acknowledgement can use `RetryFinalization`. It reuses the original
captured bytes and never seeds, runs, issues a new model request or grades. The
evidence adapter must make repeated retention idempotent. A failed or revoked run
cannot become gradeable through this path. Explicit cleanup aborts the remaining
object grants and ends finalization recovery.

Before durable evidence retention, process loss is non-rerunnable and may lose
ephemeral workspace bytes. This coordinator does not claim process-restart
recovery. Once retention succeeds, a later Platform adapter can recover from its
durable encrypted records. Failed retention keeps the frozen workspace available
to the still-running process; do not automatically delete ambiguous evidence.

## Validation

Tests exercise native workspace edits and pristine replay, partial publication,
all cleanup failures, cancellation, lifecycle revocation, shorter run budgets,
wrong acknowledgements and exact-byte finalization retries. A composition test
uses the concrete harness lifecycle, source router and native relay with a local
synthetic bridge and approved synthetic harness transport. Input frames have a
separate Go/Python producer integration. These tests do not execute a private
grader, contact Hippius, call a live provider or start a real candidate container.

The next layer must implement the Platform control adapters and native
patch/transcript retention, then connect protected grading and signed terminal
results. One deployed private shadow run remains the operational acceptance test.
