# Private coding-catalog record loader

Platform can optionally resolve one selected DittoBench Coding record from a
separately credentialed S3-compatible store. The loader is internal and
shadow-only: it exposes no route, enumerates no catalog, issues no ticket, and
never changes score or emissions.

## Storage boundary

The loader is disabled unless every `DITTO_CODING_CATALOG_STORAGE_*` credential
is configured. It never falls back to the miner-upload `STORAGE_*` bucket or
credentials. Production endpoints must use HTTPS; plaintext is accepted only
for loopback development. Credentials, endpoints, bucket names, and object keys
are omitted from the configuration representation and from typed loader errors.

One record is addressed only by the registered commitment digest and selected
zero-based index:

```text
coding-catalog/v1/<commitment-sha256>/records/<six-digit-index>.json
```

The caller cannot supply a URL, prefix, bucket, or arbitrary object key. The
source has no list operation and reads at most one bounded object per selector
probe. Catalog bytes are held in memory only; this layer creates no plaintext
disk cache.

## Record contract

Each object uses `dittobench-coding-private-catalog-record-v1` and contains:

```json
{
  "schema": "dittobench-coding-private-catalog-record-v1",
  "catalog_commitment_sha256": "...",
  "task_version": {},
  "membership_proof": {}
}
```

Input is byte- and depth-bounded. Duplicate fields and non-finite JSON values
are rejected. Unknown fields are ignored for rolling compatibility and never
become digest authority. The existing task and proof models revalidate their
known fields and canonical digests.

Before returning a record, the loader independently checks the exact registered
commitment digest, contract version, release, catalog index, Merkle root, task
count, task commitment, proof digest, and position-bound Merkle membership. The
selector repeats the authority and membership checks before constructing a run.

Missing objects, object-store failures, and timeouts are retryable unavailable
errors. Oversized, malformed, digest-drifted, or non-member records are
non-retryable integrity errors. Task cancellation is preserved rather than
translated into either domain.

## Activation boundary

The API lifespan constructs this source only when its separate configuration is
present. No production caller is added here. Automatic scheduling, task
delivery, Luna inference grants, validator tickets, scoring, deployment, and
emissions require later reviewed layers. Coding contract v1 remains permanently
`weight_eligible=false`.
