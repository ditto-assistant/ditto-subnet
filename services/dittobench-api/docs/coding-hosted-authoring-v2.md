# Native hosted authoring and replay v2

Status: opt-in Go runtime core. No worker, listener, inference grant, storage
credential, private delivery endpoint, grader or score path is enabled.

`codingrunner.NewHostedSession` runs actual typed repository operations through
the existing workspace engine. It is a separate entry point from `NewSession`;
the old constructor, manifest validator, request parser and frozen replay remain
strictly v1. Existing v1 frozen-patch bytes and semantics are unchanged.

## Trusted input boundary

The Platform worker must first commit its durable hosted start, verify its active
private authoring grant and retrieve/decrypt the exact authorized inputs. It must
derive the complete manifest from the verified runtime policy, resource profile
and reviewed execution profile. A hash supplied to the core is not a credential
or independent proof of those upstream checks.

`HostedAuthority` carries the canonical evaluation UUID, attempt UUID and trusted
assignment digest. Internally `Manifest.TicketID` holds the evaluation UUID and
`Manifest.CaseID` holds the attempt UUID; neither is a v1 ticket lease or a private
task/group ID. The manifest must use version 2 and a deadline within one hour,
not exceeding the assignment deadline. The caller must still verify the exact
deadline against its database authority. The workspace capability identifier
must be independently minted, not a private object grant or catalog identifier.

The injected command executor retains its existing sandbox obligations: pinned
image, isolated rootless daemon, bounded resources, network denial and complete
process cleanup. This entry point does not attest an arbitrary injected executor.
No untrusted code may run in the Platform process itself.

## Snapshot conversion

`CompileHostedSnapshot` verifies the expected plaintext capsule digest, bounded
tar entries, canonical sanitized snapshot manifest, exact file set, modes,
hashes, byte counts and source/snapshot tree commitments. It rejects links,
forbidden source/cache/credential paths, duplicate entries and nonzero trailers.
The expected digest must come from the verified private payload authority.

Only the authorized `workspace/` files are written into a deterministic flat
runner bundle. The outer `manifest.json` is not forwarded. The existing safe
bundle inspector computes the flat bundle and runtime tree identities. These
are distinct from the original capsule and sanitized-manifest tree identities;
callers must bind the original capsule and the derived identities in private
execution evidence rather than substituting one digest for another.

The returned object owns private source bytes, refuses JSON diagnostic
serialization, and redacts string/structured logging. It must remain inside the
trusted Platform runtime. The source capsule is immutable; no private data is
written to Git or uploaded by this code.

## Native events, freeze and replay

The session accepts only v2 tool requests. Its event transcript records version 2
before hashing; it does not relabel v1 events after execution. The same path,
command-ID, call-count, replay, transcript, cancellation and protected-file
checks run for both explicitly selected versions.

Freeze emits `dittobench-coding-frozen-patch-v2`, binding the assignment digest,
evaluation UUID, attempt/case UUID, visible bundle and base tree, and exact
full-file transitions. Repeated freeze returns the same result and revokes tools.
`ReplayHostedFrozenSubmission` reconstructs a fresh workspace from the pristine
base and verifies these identities against the independently trusted authority.
The v1 validator/replayer rejects v2 submissions; changing only a version label
does not make a v1 patch valid v2 evidence.

These patch bytes are the bytes the Platform freeze ledger must hash and retain.
They are not a grade or a validator-facing result. The worker still must quiesce
the harness and revoke inference before database freeze, obtain a distinct
grading grant, run a trusted grader, seal evidence and finalize a signed terminal
record. Several validators reading one record are not independent executions.

## Remaining execution requirements found during integration review

The checked-in `Dockerfile.coding-supervisor` is a certification fixture. Its
fixture test driver reports the requested count and must not be used to grade
private tasks. Normal executor preflight already rejects fixture images.
Production needs reviewed, digest-pinned language/repository environments and
trusted test drivers that isolate candidate code and observe actual outcomes.

The private v2 runtime/resource artifacts also differ from the v1 execution-plan
schemas, and the private v2 grader does not implement the v1 five-group contract.
Do not fabricate v1 groups, silently reuse the public local grader, or invent
successful test counts to bridge these differences. Native input projection,
miner session/inference orchestration, grading and sealed terminal integration
remain required before a hosted private shadow canary can claim success.

## Validation

Tests run real typed edits, native freeze, pristine replay, version/assignment
rejection and synthetic capsule conversion. Commands use test executors unless
the separate sandbox integration is explicitly configured.

An owner-local opt-in test reads the external payload directory through
`DITTO_CODING_PRIVATE_PAYLOAD_TEST_DIR` and verifies 50 distinct snapshots across
250 arms. Without that variable CI skips only this private-data compatibility
test; synthetic tests remain active. Compatibility is not model, provider,
grading or scoring certification. All activation/reward boundaries stay off.
