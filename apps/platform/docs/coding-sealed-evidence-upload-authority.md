# Sealed coding evidence upload authority

This layer records the Platform-side authority for sealed coding evidence. It
is intentionally not an S3 integration: it creates no API route, bucket,
object-store client, presigned URL, credential, executor behavior, assignment,
score, or weight path.

## Durable records

`coding_sealed_evidence_uploads` reserves exactly one evidence kind for a
`(ticket_id, claim_generation)`. Its generated `upload_id`, lowercase SHA-256,
positive exact size, fixed `application/octet-stream` type, and permanent
`weight_eligible=false` are immutable. The table contains no object key, URL,
bucket, ETag, or credential.

`coding_sealed_evidence_finalizations` is a separate append-only row keyed by
that `upload_id`. Its composite foreign key repeats the full reservation
identity, so finalization cannot name a different ticket, claim generation,
kind, digest, or size.

Both tables have database append-only guards. Exact replays are idempotent;
changing the identity for an already-reserved kind is a conflict.

## Claim boundary

Reservation and finalization require the same validator hotkey, stable worker
instance, ticket, and current claim generation as a live **started** ticket
claim. The lease and claim must still be unexpired, and the ticket may not
already have a terminal result for five of the six evidence kinds. The sole
exception is `terminal-publication-acknowledgement`: those bytes exist only
after Platform accepts the terminal result, so that kind requires the exact
ticket's terminal result to exist while the same started claim, generation,
instance, and deadline are still live. No other evidence kind may cross that
terminal boundary. A stale worker cannot reserve or finalize a different
generation's evidence.

One recovery exception grants no upload or claim authority: after an exact
`terminal-publication-acknowledgement` finalization already exists, the signing
validator may replay that immutable receipt by its ticket, generation, upload
ID, kind, digest, and size even after claim cleanup. Platform reads the existing
row without minting a URL, checking storage again, extending a deadline, or
creating state. A missing or changed receipt still follows the live-claim path
and fails closed when that claim is gone.

The worker must complete that acknowledgement upload before polling for its
next ticket. The ordinary claim-next transition clears a claim as soon as it
observes the terminal result; the exception does not resurrect or extend a
cleared or expired claim.

The ledger does not treat an S3 PUT as final. The signed finalization route
first locks the live claim and reservation, then checks the dedicated evidence
store's exact object, content type, bounded metadata, byte size, and full
streamed SHA-256. It finally re-locks the claim and reservation before appending
the finalization. Storage I/O never holds the database transaction open, and a
claim that expires or changes during verification fails closed. Only then may
later freeze/result work bind to finalized records. This preserves PostgreSQL
and signed Platform records as authority; object storage remains immutable byte
transport.

## Store isolation

`CodingSealedEvidenceStorageConfig` is optional. Without it, capability and
finalization requests return unavailable without creating authority.
When supplied, it must use a bucket and credentials distinct from both the
miner-upload store and the private-catalog store. The Platform role may create
an internal capability minter, but no worker calls it in this layer. It derives
the fixed `coding-evidence/v1/<kind>/sha256/<digest>` key, exact size/type and
bounded metadata from a reservation, then validates the short-lived PUT URL
without retaining that bearer URL in the database.

This layer still does not make a freeze/result scoreable. The trusted uploader
durably reads the exact local outbox object, uses the capability, and calls
finalization. Authoring-freeze and terminal-result acceptance now lock and bind
the phase-required finalization rows in the same transaction as their
append-only publication.

The corresponding transport restrictions are in
[`docs/coding-private-s3-data-plane.md`](../../../docs/coding-private-s3-data-plane.md).
