# Verified hosted authoring inputs

Status: Platform assembler and native Go consumer, with encrypted-object and
PostgreSQL integration tests. No production worker, inference grant, private
evaluation, score, weight or deployment is activated.

## Platform assembly

`HostedAuthoringInputAssembler` fixes its worker identity in trusted runtime
configuration. It loads the committed assignment and selected private task from
PostgreSQL, requiring the exact attempt, assignment digest, worker, active release,
deadline and unfrozen authoring phase. No caller supplies a catalog index, grant,
bucket, object key or role set. Reads use the existing release/agent/assignment/task
lock order; no provider I/O occurs while those locks are held.

The retriever's private authoring descriptor recomputes the catalog Merkle root
from the signed payload's ordered task commitments. It exposes only the six
authoring roles: catalog record, issue, visible snapshot, memory, runtime policy
and resource profile. Hidden grader objects are absent. Existing ciphertext,
AES-GCM/AAD, unwrap-request and full plaintext hash/size checks run for each read.
The assembler also verifies catalog membership, cross-object digests and a fresh
database/grant view after collection. Wrong-task, expired, retired or frozen
authority fails without returning a successful bundle.

## Private handoff

The explicit worker frame is a four-byte big-endian header length, canonical JSON
header (at most 16 KiB), six bounded objects in fixed role order, then the literal
completion marker `DITTO-AUTHORING-READY-V2\n` and EOF. The producer rechecks the
database before writing and between objects; the completion marker is written
only after the final check. It is a stream-completion marker, not a signature,
new grant, durable acknowledgement or proof of ongoing authorization.

This plaintext frame is exclusively a trusted Platform process-to-process handoff.
It is not a miner/validator API, signed public receipt or Git artifact. The caller
must provide a bounded, cancellable private transport and close it on failure.
The Go reader takes ownership of its stream, limits sizes, closes it on deadline
or cancellation, verifies EOF/marker and hashes, and matches the header against
independently supplied assignment/payload expectations. Never derive those trusted
expectations from the received frame itself.

## Native preparation

The Go `codinghostedinput` consumer binds an explicit authoring profile to the
assignment's approved execution-profile digest. `ProfileDigest` defines its
canonical identity. The profile includes a pinned executor image, full resource
policy and model/tool budgets; none is fabricated from the smaller dataset policy.
The operator must review/provision that profile before approving an assignment.
CPU, RAM, scratch and PID values must match the dataset resource object.

Snapshot identities are deliberately different:

| Identity | What it commits |
| --- | --- |
| Capsule SHA-256 | Complete downloaded tar bytes |
| Catalog snapshot tree | Outer archive file paths, sizes and hashes |
| Runner base tree | Flattened workspace, including file kinds and modes |

The existing sanitized snapshot compiler verifies the archive and projects only
workspace files. The consumer binds its outer tree to the catalog and uses the
derived flat-bundle/base-tree identities for the native v2 runner. It constructs
the raw-memory seed and issue/allowed-command projection without condition labels,
private catalog IDs, grader metadata, provider credentials or storage details.
Commands require empty task-supplied environments and retain runner validation.

`Prepared.Begin` creates the concrete authoring executor and workspace once.
`SeedRequest` and `RunRequest` provide native v2 harness inputs. Close the workspace
and prepared inputs separately; dropping prepared plaintext does not revoke grants
or erase copies already delivered to trusted processes or the candidate.

## Verification and remaining integration

Tests build a synthetic 50-group/250-arm payload, encrypt it with AES-GCM and
RSA-OAEP, sign its publication receipt, register it in real migrated PostgreSQL,
assemble the selected six objects and pass the resulting frame to Go. Go creates
the native workspace and reads a projected file through typed tools. Tests also
reject corrupted/truncated frames, wrong task/profile, expiry, freeze during
download and grader injection. No model or Docker build/test command runs in this
test; the object reader is in-memory and the keys/data are synthetic.

Next: wire the approved execution profile and assembler into the live worker with
native inference issuance/revocation, ongoing lifecycle checks, freeze, pristine
grading and sealed evidence. Grant checks cannot retract previously issued bytes;
the worker must stop on retirement/expiry and must revoke/drain before freeze.
This change does not prove a live Hippius/KMS canary or complete the private bench.
