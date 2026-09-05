# Coding private v2 Hippius publication

Status: implemented operator tooling, default-off and shadow-only. This path
does not run from Platform startup, CI, a validator, or a miner. It does not
register a release, issue a task, enable a worker, score a patch, or affect
weights or emissions.

The v2 publisher consumes the verified
`dittobench-coding-private-v2-transport-v1` directory. It is separate from the
older catalog-index v1 publisher because v2 stores deduplicated content
objects and binds `transport_sha256`, `payload_sha256`, `catalog_sha256`, and
`catalog_merkle_root`.

## Authority flow

```text
verified v2 transport
    + current successful provider-probe receipt
    + exact repository source SHA
    + curator Ed25519 public-key identity
        ↓
canonical external-signing message
        ↓ owner-controlled signing boundary
detached 64-byte Ed25519 signature
        ↓ explicit publication confirmation
curator exact-key PUT to private-input bucket
        ↓
Platform-reader complete GET and SHA-256/byte-count verification
        ↓
canonical redacted publication receipt
```

The remote key is derived only from the transport digest and the object's
ordinal:

```text
coding-private-inputs/v2/<transport-sha256>/objects/<ordinal>.bin
```

It does not include repository names, task IDs, condition labels, plaintext
digests, or other semantic metadata. Receipts contain only a hash of each
remote key. Provider metadata contains the object ordinal, ciphertext digest,
and transport digest.

## Safety properties

- The provider probe must be successful, no older than 24 hours, and bind the
  exact current private-input bucket and curator/reader identities.
- Each published ciphertext is capped at the existing reviewed provider
  profile's 2 MiB plaintext-sized object boundary. A larger offline v2
  transport fails before any provider request until a separate capability
  profile proves and reviews a larger limit.
- The source SHA, probe receipt, storage authority, transport identity,
  catalog/payload identities, wrapping key, and object count are covered by the
  curator signature.
- Publication never lists, deletes, or overwrites. Existing exact keys are
  accepted only when complete downloaded bytes match the expected ciphertext.
- Every newly uploaded object is completely downloaded through the distinct
  read-only Platform reader and verified before it enters the receipt.
- Redirects, ambient proxies, virtual-hosted addressing, provider errors, and
  unsafe output paths fail closed through the existing reviewed Hippius
  publication transport.
- No endpoint, bucket, access key, secret, presigned URL, raw object key,
  plaintext digest, or object byte is written to the receipt.
- The receipt is permanently `shadow_only=true` and
  `weight_eligible=false`.

## Operator boundary

Generate the exact message after the publication source revision and fresh
provider-probe receipt are fixed:

```bash
cd apps/platform
uv run python scripts/plan_private_v2_publication_signature.py \
  --transport /protected/private-v2-transport \
  --probe-receipt /protected/current-provider-probe.json \
  --curator-public-key /protected/curator-ed25519-public.pem \
  --output /protected/private-v2-signing-message.bin
```

The owner-controlled curator signs those exact bytes outside the Platform,
validator, executor, miner, CI, and model environments. The private signing
key is never passed to either repository script.

Publication requires a separately approved runtime with the existing
bucket-scoped curator and reader secrets injected from protected custody:

```bash
cd apps/platform
uv run python scripts/publish_private_v2_to_hippius.py \
  --transport /protected/private-v2-transport \
  --probe-receipt /protected/current-provider-probe.json \
  --curator-public-key /protected/curator-ed25519-public.pem \
  --curator-signature /protected/private-v2-signature.bin \
  --receipt-output /protected/private-v2-publication-receipt.json \
  --confirm 'PUBLISH HIPPIUS CODING PRIVATE V2 PAYLOAD'
```

These commands document the contract; merging this implementation does not
authorize running them against live private data. A live publication still
requires a production wrapping public key, a freshly regenerated transport,
a current provider probe, temporary runtime secret access, and explicit owner
approval.
