# Native grading and terminal delivery

The native authoring worker can now continue from its retained, committed patch
through grading and sealed terminal publication. `Attempt.RunComplete` composes
authoring and grading; `Attempt.Grade` continues an already committed authoring
attempt and retries only captured terminal bytes after publication failures.

The private control service adds `grading`, `check_grading`, `grading_bundle`
and `terminal` operations. Public validator control returns a signed terminal
result only after encrypted terminal evidence is finalized. Runtime startup,
production profiles/images, key custody, release publication and a deployed
private canary remain unverified. No score, weight or emission path is activated.

## Approved grader profile

`GradingProfile` is a canonical, digest-bound template with the pinned image,
native grader contract, exact grader-bundle digest, reviewed test-manifest
commitment, resource envelope, build command, two required test groups and
execution timeout. Groups are sorted `hidden`, `visible`; execution is `visible`
then `hidden`. Counts and commands are explicit reviewed inputs, not inferred
from candidate stdout or fabricated from the corpus size.

The assignment approval commits its `grading_profile_sha256` before execution.
Platform and Go independently reconstruct the native per-attempt grader-plan
digest from that profile and the retained freeze. Go retains the existing
strict manifest validation and uses `PhaseFactory.HostedGrading`, whose production
executor requires the isolated, pinned runtime and refuses certification fixtures.

## Claim, replay and protected read

1. The worker submits the exact previously retained private freeze document and
   authoring-evidence commitment. Platform verifies their bytes and metadata
   against the authoring ledger and committed patch, with active assignment,
   worker, release, artifact, grading profile and phase checks.
2. Platform commits one immutable grading claim per evaluation before private
   grading reads. A repeated claim is refused, including after an ambiguous
   response. The claim binds the source, patch, profile and expected receipt
   identities; it cannot authorize a fresh candidate or grader rerun.
3. The initial private frame contains catalog, visible snapshot, runtime and
   resource objects plus the protected bundle's committed identity. It contains
   no hidden grader bytes. Both ends verify the registered task and hashes.
4. Go compiles the pristine snapshot, verifies its catalog/runtime trees and
   the approved plan, and invokes native frozen-patch replay. The original
   authoring workspace has already been closed.
5. Only the grader's post-replay protected opener requests `grading_bundle`.
   Platform rechecks the exact claim and active grading grant. Go verifies the
   full raw bundle digest, safe archive and catalog grader-tree commitment
   before materialization in the separate protected workspace.
6. A one-second authority monitor cancels grading on a failed check. Existing
   native grading verifies executor receipts, frozen-tree integrity, protected
   material integrity and cleanup. Missing or invalid evidence cannot become
   successful grading.

If the process loses a grading claim before it captures a result, automatic
re-execution is forbidden. This layer does not provide process-restart recovery
for that gap. Recovery after a captured result republishes those same bytes;
it does not reopen the grader. Losing the in-memory freeze on process restart
also requires a separate trusted recovery reader for retained evidence.

## Sealed terminal evidence

Platform checks the native result's source, frozen patch, grader profile,
command identities, ordered receipt hash chain, expected counts and outcome.
A successful result requires every expected receipt and matching integrity
commitments. Only bounded failure classes may lack full grading evidence.
Malformed known fields are rejected; private raw output never becomes a public
score claim. Late or no-longer-authorized new results are classified as
infrastructure failures rather than candidate failures or successes.

Terminal publication reuses the reviewed encrypted-blob primitive in a distinct
terminal namespace and binds the grading claim, authoring commitment, raw-result
digest and Platform outcome in a terminal identity. Exact ciphertext is spooled
before the append-only PostgreSQL reservation. Full Hippius readback precedes
finalization and closing the private task's object grants. Existing conflicting
bytes are never overwritten. A finalized identity can replay its historical
acknowledgement without another grader run or object write.

New publication is bounded to 24 hours after the assignment deadline and needs
a fresh matching provider probe. A local preparation that never reserved a
successful terminal identity before its assignment expired cannot later claim
timely success. Partial spools and unresolved claims remain fail-closed.

## Validator protocol

The existing signed control request and current validator authorization remain
required. Every response is `Cache-Control: no-store`.

| Response | Meaning |
| --- | --- |
| 202, `HostedCodingStatus` | Assignment is pending or started; terminal evidence is not finalized |
| 200, `HostedCodingResult` | Finalized terminal evidence, with a fresh signature bound to this exact request |
| 204 | The exact previously delivered result digest was acknowledged |

The result contains opaque evaluation/attempt identities, artifact and profile
digests, one permitted outcome and an evidence commitment. It includes no patch,
test counts, execution receipts, grader content, source paths or private score.
Existing validator verification checks canonical bytes, trusted signing key,
request binding, all assignment identities and the bounded signature lifetime.

Each signed delivery is retained in an append-only table. Acknowledgement uses
`hosted_message_digest(result)`, consumes a fresh signed request nonce and checks
the stored delivery's evaluation and validator ownership. Replaying the same
nonce is rejected. Several deliveries of one attempt are still one execution.

## Verification

The connected test runs the existing Go/Python authoring path, native pristine
grading with a scripted executor, encrypted terminal readback, public response
signature verification and acknowledgement. It observes actual fixture workspace
files but uses synthetic provider/storage and executor transport; it is not a
production private grader or live canary. Failure tests cover preflight denial
without any hidden read, malformed receipts, corrupt terminal readback and exact
publication recovery. Runtime deployment and benchmark-quality validation remain
separate acceptance requirements.
