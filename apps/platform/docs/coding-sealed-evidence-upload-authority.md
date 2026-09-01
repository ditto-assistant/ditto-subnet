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
already have a terminal result. A stale worker cannot reserve or finalize a
different generation's evidence.

The ledger does not treat an S3 PUT as final. A later route must verify the
dedicated evidence store's exact object, bounded metadata, byte size, and full
SHA-256 before calling finalization. Only then may later freeze/result work
bind to finalized records. This preserves PostgreSQL and signed Platform
records as authority; object storage remains immutable byte transport.

The corresponding transport restrictions are in
[`docs/coding-private-s3-data-plane.md`](../../../docs/coding-private-s3-data-plane.md).
