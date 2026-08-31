# Private coding S3 data plane

This document freezes the byte-transport boundary for a future shadow coding
executor. It does not create a bucket, grant a credential, mount a worker,
change a score, or make coding contract v1 weight eligible.

The existing Platform lease, claim, authoring-freeze, settlement, and signed
result ledgers remain the authority for all state transitions. S3-compatible
storage holds immutable bytes only.

## Authority boundary

```text
PostgreSQL + signed Platform records
  assignment -> ticket -> claim -> freeze -> result -> score
                    |                         ^
                    | short-lived capabilities |
                    v                         |
             trusted executor ------ sealed evidence
                    |                         |
                    +------ private S3 -------+
```

The following are never authority for a lease, freeze, result, or score:

- an object URL, bucket name, caller-supplied key, ETag, or object-listing
  result;
- an S3 event, object creation time, or storage-provider clock;
- a successful upload before Platform verifies and finalizes its expected
  identity; or
- storage metadata in place of a full consumer SHA-256 check.

Platform derives every storage key from an already verified record. A validator,
executor, miner, or model may not select a bucket, prefix, key, artifact kind,
or object version.

## Logical storage authorities

Production has two non-interchangeable S3 authorities. They should use separate
buckets. A single physical bucket is permitted only when two distinct IAM
identities and non-overlapping prefix policies provide the same isolation.

| Authority | Contents | Long-lived access |
| --- | --- | --- |
| private inputs | catalog records and immutable visible, memory, resource, and grader artifacts | curator/release writer and Platform read/presign service |
| sealed evidence | transcripts, frozen submissions, and exact publication request/acknowledgement bytes | Platform evidence finalizer and retention/audit operator |

Neither authority may be the miner-upload, public-download, avatar, trace, or
general application bucket. Neither validator, dedicated executor, candidate
container, or model process receives an S3 credential. A trusted executor may
receive only one short-lived capability URL for an already authorized object.

`ListBucket`, public read, public write, delete, and cross-authority reads are
not required by the runtime and must be denied. Bucket policy must require TLS,
default encryption, audit data events, and retention appropriate to the coding
evidence policy.

## Object names and identities

Existing private inputs retain their fixed namespaces:

```text
coding-catalog/v1/<catalog-commitment>/records/<six-digit-index>.json
coding-artifacts/v1/<artifact-kind>/sha256/<digest>
```

Sealed evidence uses a separate server-derived namespace:

```text
coding-evidence/v1/<evidence-kind>/sha256/<digest>
```

Valid evidence kinds are `authoring-transcript`, `frozen-submission`,
`authoring-publication-request`, `authoring-publication-acknowledgement`,
`terminal-publication-request`, and `terminal-publication-acknowledgement`.
The outbox's existing logical key, `sha256/<digest>`, remains the signed
reference. Its physical S3 prefix is an implementation detail and never enters
a miner/model response or canonical signed message.

Every authoritative reference contains at least its kind, lowercase SHA-256,
and exact positive byte size. The storage object must carry matching bounded
metadata for `sha256` and `artifact-kind`/`evidence-kind`, but that metadata is
only a preflight check. Consumers stream the entire object under the declared
size bound and recompute its SHA-256 before use.

Writes are immutable: an existing content-addressed object is accepted only
when its complete identity is reverified; a different body or metadata for the
same logical identity fails closed. S3 versioning alone does not satisfy this
rule because it permits a new current version under the same key. A deployment
must either reject overwrite atomically or persist and authorize the exact
version ID in a later versioned contract. Multipart ETags are never a substitute
for the whole-object SHA-256.

## Capability rules

Platform mints a capability only after verifying the ticket, claim generation,
phase, artifact/evidence identity, and remaining deadline. A URL expiry is the
smaller of the requested bound, five minutes, and the remaining ticket lifetime.
URLs are bearer credentials: they are redacted from logs, excluded from object
representations, and never persisted in an outbox record or signed envelope.

Input GET capabilities preserve the current phase split:

```text
authoring: visible-bundle, memory-bundle, resource-profile
grading:   visible-bundle, resource-profile, grader-bundle
```

The grader capability is not minted or projected until Platform has accepted
the exact authoring freeze. No input capability enters the candidate harness or
model context; trusted materializers and protected graders consume them.

Evidence PUT capabilities are different from input GET capabilities. Platform
derives their key and binds the method, kind, content type, exact byte size,
checksum, metadata, ticket, and deadline. A client cannot reuse a transcript
capability for a frozen submission or a different ticket. An expired URL may be
refreshed only for the same still-live authority; refreshing a URL never starts
a new attempt.

## Evidence publication order

The local durable outbox remains the first commit point. S3 does not replace its
non-rerunnable state machine.

```text
reserve local attempt
  -> commit collecting marker before candidate activation
  -> seal transcript/frozen/publication bytes locally
  -> request one ticket-bound evidence PUT capability
  -> upload exact bytes
  -> Platform HEAD/full-check finalizes expected identity
  -> signed freeze or terminal result references only finalized identities
```

Platform must reject a freeze or terminal result whose required evidence object
is missing, exceeds its declared size, has mismatched metadata, or fails a full
digest verification. A successful PUT alone is insufficient. The current
prepare -> publish -> acknowledge ordering remains unchanged: exact signed
request and verified acknowledgement bytes are committed locally before their
state transition is released.

## Failure and recovery rules

| Condition | Classification | Required behavior |
| --- | --- | --- |
| object store unavailable, timeout, or expired capability before finalization | infrastructure | retry the same authority while its claim and deadline remain valid |
| missing expected object after a sealed local record | infrastructure | recover only the exact sealed bytes; never rerun candidate authoring or grading |
| wrong key, size, kind, metadata, version, or SHA-256 | integrity | fail closed; do not issue a clean retry |
| candidate failure after the collecting marker | candidate terminal evidence | seal and publish the authoritative failure; do not erase or rerun it |
| Platform response loss after publication | transport ambiguity | replay only the exact durable request bytes |

S3 events, inventory jobs, or object listing may support operator audit but may
not schedule work, infer completion, or repair a claim. PostgreSQL
reconciliation enumerates expected authoritative records and performs bounded
verification of those exact objects.

## Deployment boundary

This document does not select AWS S3 versus another compatible provider, create
Terraform resources, or introduce a storage endpoint/environment variable. A
deployment implementation must prove the chosen provider supports the immutable
write, presigned capability, metadata, timeout, and full-download verification
rules above. It must also keep input and evidence identities distinct from the
existing miner-upload and public artifact stores.

The next implementation layer may add Platform evidence capabilities and
finalization, then an executor outbox exporter. It must preserve the current
authoring/grading split, durable local recovery, locked inference settlement,
and permanent `weight_eligible=false` posture. Executor ingress, private catalog
registration, worker activation, scoring, and emissions remain separate later
reviews.
