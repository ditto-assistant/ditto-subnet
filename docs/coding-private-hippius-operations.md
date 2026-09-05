# Coding Hippius operational profiles

## Status

This companion operational profile refines
`docs/coding-private-hippius-data-plane.md`. It does not replace that provider
decision, provision a bucket, create a credential, issue a task, enable a
worker, or change `weight_eligible=false`.

Hippius remains the sole active remote object-data plane for Coding private
inputs and sealed evidence. PostgreSQL and signed Platform authorities remain
the authority for selection, leases, reservations, completion, and scoring.
An owner-controlled offline recovery source may retain encrypted release
material, but is never a runtime fallback, scheduler input, or alternate
object-data plane.

## Profile identity and expiry

Every confirmed provider operation must attach a private,
content-addressed `coding-hippius-provider-profile-v1` receipt. The receipt
contains only fingerprints of the following values:

- canonical HTTPS endpoint and `decentralized` region;
- reviewed S3 operation matrix and profile schema version;
- private-input and sealed-evidence bucket identities;
- scoped credential identities and permissions;
- source revision, probe time, and expiry time; and
- successful and rejected operation names.

Raw endpoints, bucket names, access IDs, secrets, presigned URLs, object keys,
or object bytes must not appear in a signed task, public receipt, model context,
error, or log.

A profile is invalidated by expiry, endpoint or TLS-identity drift, changed
bucket or credential policy, changed reviewed provider profile, a failed
scheduled probe, or an operation outside its recorded matrix. A later provider
documentation claim never changes the active Coding contract by itself.

## Credential profile

The owner holds management authority outside Platform, validator, executor,
miner, CI, and model environments. Coding applications use distinct,
non-human, bucket-scoped credentials:

| Identity | Bucket | Permitted application behavior |
| --- | --- | --- |
| private-input curator | private input | offline exact-object publication and verification |
| private-input reader | private input | Platform exact-object read and verification |
| evidence mediator | sealed evidence | reserved exact-object publication and full readback |

Provider read/write scope may be broader than the permitted application
behavior. Platform must still prohibit list, delete, overwrite, public ACL,
cross-bucket, and arbitrary-key operations. No miner, executor, coding
container, or model process receives an S3 credential or a reusable presigned
write URL.

## Revocation observation profile

`coding-hippius-revocation-observation-v1` is a confirmation-gated,
disposable-credential probe. An owner-only management identity creates and
revokes the probe credential; the probe process has only that credential.

The receipt records credential fingerprint, last observed success, first
observed rejection, clean-connection retry cadence, and any presigned URL
result. It does not claim a provider-specified revocation time is a verified
bound. Operations use a conservative bound derived from the observed receipt.

## Object Lock observation profile

Object Lock is not a v1 confidentiality, integrity, availability, or evidence
acceptance dependency. `coding-hippius-object-lock-observation-v1` may test a
reviewed deployment profile only in a disposable bucket with a short retention
period.

The version-aware probe must confirm:

1. versioning and Object Lock configuration before upload;
2. a retained object version and its observed retention metadata;
3. same-key upload creates a distinct version without mutating the retained
   version;
4. version-specific deletion is rejected while retention applies;
5. an unversioned delete marker, if accepted, does not remove the retained
   version; and
6. the retained version still downloads to the expected ciphertext digest and
   byte count.

The receipt proves only observed API-level behavior for the reviewed deployed
profile. It does not prove physical immutability or provider-administrator
behavior. A production bucket is never modified for this probe.

## Metadata confidentiality profile

Objects use contract-derived or opaque random keys; repository names, issue
identities, task IDs, condition labels, and human-semantic metadata are
forbidden. Client-side authenticated encryption occurs before every Hippius
request, with a fresh data-encryption key per object version and external
wrapping-key custody.

Exact plaintext size, upload timing, and access patterns are potential leakage
channels. Before a competitive private release, the operator must select
measured object-size classes and verify authenticated padding/framing. Padding
is not introduced until its storage, latency, and task-family effects are
calibrated. Access logs are redacted, minimized, and retained only under the
approved operations policy.

## Retention and recovery profile

Each private object class requires an owner-approved retention rule before its
first publication:

| Object class | Retention authority |
| --- | --- |
| encrypted private release | release/dispute policy |
| visible repository and memory bundle | referenced release lifetime |
| grader/resource bundle | release reproducibility policy |
| sealed authoring, patch, grading, and publication evidence | audit/dispute policy |
| temporary upload | explicit protected cleanup procedure |

Hippius remains the only active remote object-data plane. Owner-controlled
encrypted recovery material may be held outside the runtime path solely for a
declared disaster recovery procedure. It cannot be read automatically when
Hippius is unavailable and cannot alter an assignment, task, receipt, or score.

Recovery requires explicit owner authorization, a clean environment, canonical
release-manifest validation, ciphertext digest and byte-count verification, and
a recorded restore drill. Cryptographic erasure is valid only when every
object's unique data-encryption key, all wrapped-key copies, and all key backups
are within the approved destruction boundary.

## Activation gates

Before any private Coding shadow release, require all of:

1. a current provider-profile receipt;
2. scoped credentials from the protected owner boundary;
3. verified client-side encryption and external unwrap custody;
4. readback SHA-256 and byte-count verification;
5. accepted retention and recovery policy;
6. a completed synthetic single-validator canary; and
7. explicit owner authorization.

Object Lock and revocation observations improve the operating posture but do
not silently activate a worker, scoring, weights, or emissions.

The condition-specific v2 corpus uses the separate, deduplicated publication
profile documented in
`apps/platform/docs/coding-private-v2-publication.md`. The legacy v1
catalog-index publisher must not be used for a v2 transport manifest.
