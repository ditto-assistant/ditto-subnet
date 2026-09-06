# Native v2 inference evidence

`HostedInferenceEvidencePublisher` implements the native relay's
`retain_evidence` callback. Model output is released only after this callback
finishes. It retains the verified provider response, normalized model response,
settlement, policy and canonical budget profile inside the trusted Platform.

This is an unconstructed runtime primitive. It adds no route, factory wiring,
worker, credential provisioning or activation setting. It does not seal frozen
patches, grading evidence or the final validator result. Coding remains
`weight_eligible=false`.

## Publication and recovery

1. Verify the settled request in PostgreSQL against the worker, grant,
   evaluation, attempt, assignment and policy. Revalidate provider evidence and
   billing against the committed runtime profile at the request's start time.
2. Encrypt with a fresh AES-256-GCM key and nonce, wrapping the data key with
   the external custody layer's RSA-OAEP-SHA256 public-key wrapper. The envelope
   binds the native authority, plaintext digest, storage domain and wrapping key.
3. Durably store the exact framed ciphertext and canonical identity in a
   pre-provisioned, owner-only Platform spool outside Git. No plaintext is
   written there. Files are sealed mode `0400` and directories are mode `0700`.
4. Commit an append-only PostgreSQL reservation before any Hippius operation.
5. Read the derived object key, upload only if absent, then download the complete
   object and verify its byte count, SHA-256 and envelope commitment. Existing
   conflicting bytes are never overwritten.
6. Append finalization only after readback and renewed authority checks.

The two native ledger tables bind evidence to native inference requests, not
legacy v1 tickets. Insert triggers validate the source identity and publication
window; update and delete triggers reject ledger rewrites. The schema mirror
under `services/model-relay/db/schema.sql` follows the same Alembic chain.

The sealed blob contains a format marker, nonce, wrapped-key length, wrapped key
and authenticated ciphertext. PostgreSQL retains the digest-bound manifest
needed to interpret the object, without raw evidence, storage URLs or secrets.

`resume(request_id)` reopens exactly the stored bytes. It never reruns inference,
re-encrypts, unwraps or replaces the stored identity. Interrupted or ambiguous
uploads preserve the reservation and spool. Key rotation leaves prepared bytes
bound to their original wrapping key; credential rotation requires a fresh
matching provider probe and cannot redirect an object to another storage domain.
Old wrapping private keys must remain in approved custody for later decryption.

A provider probe must be less than 24 hours old. Publication must finish within
24 hours of settlement, with checks between storage operations and before
finalization. An expired publication remains incomplete; it does not authorize
new inference. Historical finalizations can still contribute to the evidence
set after that window.

## Worker integration contract

Construct the publisher only inside a trusted Platform worker, using its native
worker identity, committed runtime profile, protected spool, wrapping public key,
dedicated Hippius evidence credential and fresh probe. Pass it to
`HostedRelayBridge(retain_evidence=publisher)`.

After revoking and draining inference, call `require_complete(grant_id)` to
obtain an ordered commitment to the finalized evidence identities. Pending,
uncertain or unfinalized requests reject completion. This commitment is only
an inference-evidence prerequisite: the worker must independently prove
container shutdown, frozen patch identity, pristine grading and terminal result
publication. It is not a grading result or physical-drain proof.

The spool enforces process exclusion, file ownership, modes, link counts,
ancestor safety and bounded capacity. It has no garbage collector. A partial
entry blocks further use and requires operator recovery; do not delete or
rebuild ambiguous evidence automatically. Provision and monitor capacity before
admitting work. Cancellation during remote publication retains exact replay
bytes but does not release model output or authorize a new candidate attempt.

The shared Hippius SDK transport checks the exact HTTPS origin and object path
before each send, including SDK retries. Tests exercise synthetic provider
responses and storage with real PostgreSQL; passing them does not establish
live Hippius capability, deployed worker readiness or competitive activation.
