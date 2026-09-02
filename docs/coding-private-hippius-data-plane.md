# Hippius-only private Coding data plane

## Status

This document fixes the object-storage provider for DittoBench Coding private
inputs and sealed evidence to Hippius. It is a design contract only. It creates
no bucket, credential, object, endpoint, deployment, worker, score, weight, or
emission path.

Coding contract v1 remains shadow-only and permanently
`weight_eligible=false`. PostgreSQL remains the durable control-plane
authority. A secret manager or KMS may retain credentials and wrapping keys;
those systems are not an alternate object-data plane.

This decision supersedes the unmerged GCS coding-storage proposal represented
by PRs #1472-#1476 and #1478-#1480. Those branches must not be merged or applied
as the Coding data-plane implementation. Existing non-Coding Hippius consumers,
including avatar and inference-trace storage, are outside this decision.

## Provider decision

Hippius is the only remote object store for:

- encrypted private catalog records and task bundles;
- visible repository bundles and scoped memory bundles;
- protected grader bundles and resource profiles; and
- encrypted sealed authoring, patch, grading, and publication evidence.

Private inputs and sealed evidence use distinct private buckets, credentials,
object namespaces, and application adapters. Neither may reuse miner-upload,
avatar, inference-trace, developer, or another Coding authority.

There is no GCS, miner-upload, avatar, trace, or public-bucket fallback. A
Hippius outage, timeout, or incomplete publication is trusted infrastructure
failure: retry the same durable authority when safe, and never penalize the
candidate or substitute bytes from another store.

The reviewed transport target is HTTPS S3-compatible Hippius with region
`decentralized`. Endpoint, region, bucket, and credential values remain runtime
configuration and never enter a signed task, evidence identity, public API,
error, log, or model context.

## Authority split

Hippius stores opaque bytes. It never decides which task runs, whether an
upload is complete, or whether evidence is accepted.

| Concern | Authority |
| --- | --- |
| Catalog release and selected task | signed Platform commitment and PostgreSQL |
| Task identity | canonical plaintext task and artifact digests |
| Stored-object identity | exact ciphertext SHA-256, byte count, and contract-derived or opaque key |
| Assignment and ticket | Platform PostgreSQL lease state |
| Encryption and key release | trusted curator plus external KMS/secret boundary |
| Candidate-visible workspace | trusted phase-separated executor |
| Evidence completion | one-time PostgreSQL reservation and finalization |
| Retry classification | signed Platform and validator evidence |

Bucket listing, object-store events, provider metadata, ETags, blockchain
publication, cache state, and successful HTTP status are never scheduling,
completion, or scoring authority.

## Reviewed provider gaps

The Hippius S3 staging surface reviewed on 2026-09-02 at
`1fa2066a366a0b839e83be60f8ab643153a772f6` supports SigV4, expiring presigned
URLs, private buckets, scoped subtokens, versioning, metadata, range requests,
audit logging, and transparent envelope encryption. The same reviewed surface
does not yet provide every control assumed by the abandoned GCS design:

- `object_read` permits object reads and bucket listing;
- arbitrary IAM-style bucket policies and Public Access Block are absent;
- conditional create-only `PUT` is absent;
- lifecycle configuration is accepted but not enforced;
- Object Lock configuration persists without WORM enforcement, and retention
  and legal-hold operations remain unavailable; and
- native SHA-256 object checksums are not an acceptance authority.

The source of truth for that review is Hippius's pinned
[compatibility matrix](https://github.com/thenervelab/hippius-s3/blob/1fa2066a366a0b839e83be60f8ab643153a772f6/docs/s3-compatibility.md),
[Object Lock specification](https://github.com/thenervelab/hippius-s3/blob/1fa2066a366a0b839e83be60f8ab643153a772f6/specs/s3-object-lock.md),
and
[subtoken scope tests](https://github.com/thenervelab/hippius-s3/blob/1fa2066a366a0b839e83be60f8ab643153a772f6/tests/unit/gateway/test_sub_token_scope.py).
Provider evolution must be re-probed against the deployed endpoint before
activation; a later feature claim does not silently weaken this contract.

These gaps are activation blockers unless the trusted application boundaries
below compensate for them. S3 compatibility alone is not evidence that the
required security semantics hold.

## Private-input publication

The offline curator performs publication. It must:

1. validate the canonical private record, task, runner, grader, resource, and
   membership preimages before storage;
2. generate a fresh authenticated-encryption data key for each object version;
3. encrypt bytes before any Hippius request and wrap the data key outside
   Hippius;
4. derive the contract-defined catalog or artifact key without a repository,
   issue, case, or other human-semantic name;
5. upload with a curator-only credential scoped to the private-input bucket;
6. download the complete stored ciphertext through a separate verification
   path and recompute its SHA-256 and byte count; and
7. sign a private transport manifest that binds the catalog commitment to the
   logical key, plaintext identity, ciphertext identity, envelope identity, and
   byte count.

No deterministic ciphertext, repository name, issue text, or case ID may
appear in the object key. An exact verified replay may reuse an existing
byte-identical object. Changed bytes require a new transport identity and, when
the plaintext identity changes, a new catalog commitment; they never overwrite
an accepted version.

Hippius's transparent encryption is defense in depth, not the secrecy
boundary. The provider must never possess the client-side plaintext data key or
external wrapping authority.

The offline implementation begins with
`apps/platform/scripts/prepare_hippius_private_inputs.py`. It reuses the
canonical curator publication plan, encrypts each record with a fresh
AES-256-GCM data key and 96-bit nonce, and wraps that key with
RSA-OAEP-SHA256 to an RSA public key between 3072 and 8192 bits. Canonical AAD
binds the catalog,
publication, logical object, plaintext, task, and wrapping-public-key
identities. The private wrapping key is structurally absent from this layer.

The tool creates one new mode-`0700` local directory containing mode-`0600`
ciphertext objects and a manifest written last. A partial directory without
`manifest.json` is deliberately unpublishable and is retained for operator
inspection; reruns require a new output path. The manifest is digest-bound but
not yet curator-signed.

```bash
cd apps/platform
uv run python scripts/prepare_hippius_private_inputs.py \
  --commitment /protected/catalog/commitment.json \
  --records-dir /protected/catalog/records \
  --wrapping-public-key /protected/keys/coding-input-wrap-public.pem \
  --output-dir /protected/catalog/encrypted-transport-new \
  --confirm "ENCRYPT HIPPIUS CODING PRIVATE INPUTS"
```

`scripts/plan_hippius_private_input_signature.py` combines that manifest with
a successful canonical probe receipt and an Ed25519 curator public key. It
writes the exact mode-`0600` message for an external KMS, hardware signer, or
other protected curator signer; no private signing key enters this repository
tooling. The detached raw 64-byte signature is then consumed by
`scripts/publish_hippius_private_inputs.py`.

Publication requires the same private-input endpoint, bucket, curator access
ID, and reader access ID fingerprint observed in a probe receipt no more than
24 hours old. It checks for the manifest-derived remote key using only the
reader, reuses exact bytes idempotently, refuses conflicting bytes, uploads a
missing object with only the curator, and performs a complete reader download
and SHA-256 verification afterward. The exact confirmation is
`PUBLISH HIPPIUS CODING PRIVATE INPUTS`.

```bash
cd apps/platform
uv run python scripts/plan_hippius_private_input_signature.py \
  --transport-dir /protected/catalog/encrypted-transport \
  --probe-receipt /protected/receipts/hippius-probe.json \
  --curator-public-key /protected/keys/curator-signing-public.pem \
  --output /protected/catalog/private-input-signing-message.bin

# Sign the exact message bytes outside this repository process, then:
uv run python scripts/publish_hippius_private_inputs.py \
  --transport-dir /protected/catalog/encrypted-transport \
  --probe-receipt /protected/receipts/hippius-probe.json \
  --curator-public-key /protected/keys/curator-signing-public.pem \
  --curator-signature /protected/catalog/curator-signature.bin \
  --receipt-output /protected/receipts/private-input-publication-new.json \
  --confirm "PUBLISH HIPPIUS CODING PRIVATE INPUTS"
```

The redacted mode-`0600` publication receipt retains the detached signature,
curator public-key digest, probe-receipt digest, manifest identity, remote-key
digest, ciphertext identity, and uploaded/reused outcome. It contains no
endpoint, bucket, raw object key, access ID, secret, plaintext, URL, or object
bytes. Publication still does not register or activate a catalog. Unwrap
service custody, registered-release wiring, and phase delivery remain later
reviews.

## Private-input retrieval

Only Platform holds the private-input reader credential. The current Hippius
scope may technically permit listing, but production code must never issue a
list request. Validators, executors, miners, model containers, CI, and human
developers receive neither that credential nor a derived catalog-wide token.

Platform derives one exact object from a registered commitment and selected
index. It fetches or signs only that fixed object. Before any plaintext is
trusted, the consumer must:

- reject redirects, ambient proxies, plaintext transport, and unexpected host
  or path forms;
- enforce the signed expiration, positive byte limit, ticket, audience, and
  phase;
- stream and verify the complete ciphertext SHA-256 and byte count;
- obtain the ticket-bound unwrap capability only after assignment commitment;
- authenticate and decrypt in a trusted process without a plaintext disk
  cache; and
- recompute every canonical plaintext digest before materialization.

The candidate receives only the phase-appropriate materialized workspace. No
Hippius URL, credential, wrapping key, hidden grader bundle, or catalog identity
enters the miner harness or model context.

The default-off Platform primitive begins with
`apps/platform/ditto/api_server/coding_hippius_retrieval.py`. It loads a
canonical encrypted manifest and canonical ready publication receipt, verifies
the external curator signature, and requires the runtime reader identity to
reproduce the published authority fingerprint. One post-assignment authority
binds the ticket, validator, run, assignment, run manifest, deadline, delivery
phase, registered catalog, selected index, manifest, and publication receipt.

The reader can derive only the corresponding manifest-addressed key. Its live
adapter creates a 60-second exact-GET signature locally, rejects a changed
origin or path, disables ambient proxies and redirects, streams within the
manifest byte bound, and verifies the complete ciphertext digest before asking
an external unwrap boundary for a data key. The unwrap request and response are
digest-bound to the ticket and cannot outlive it. AES-GCM authentication,
plaintext size/SHA-256, canonical JSON, catalog membership, task identity, and
all record digests are rechecked before the in-memory record is returned.

This primitive has no API route, lifespan construction, secret-manager or KMS
implementation, registered-release loader, plaintext cache, worker caller, or
activation flag. Those boundaries remain separate reviews; merging it cannot
make the existing plaintext-compatible catalog source use Hippius ciphertext.

## Sealed-evidence publication

Hippius currently cannot enforce the create-only, single-use, WORM write
authority required for direct executor uploads. Therefore no validator,
executor, candidate, or model process receives a Hippius write credential or a
reusable presigned `PUT` URL.

A trusted Platform evidence mediator owns the evidence-bucket credential and
implements one-time publication:

1. reserve the exact ticket, claim generation, evidence kind, opaque key,
   ciphertext SHA-256, plaintext evidence digest, and byte count in PostgreSQL;
2. accept one authenticated, bounded byte stream for that reservation;
3. reject a missing, expired, finalized, replayed, or identity-drifted
   reservation before contacting Hippius;
4. encrypt before upload and use a new opaque key that no prior reservation has
   used;
5. upload once, then download the complete stored object and verify ciphertext
   SHA-256 and byte count;
6. append the immutable finalization only after full verification; and
7. return a URL-free, credential-free, digest-bound receipt.

An HTTP success, `HEAD`, ETag, provider version ID, or object listing is not
finalization. Failed or ambiguous publication retains the same reservation and
exact local outbox bytes; recovery never reruns candidate authoring or grading
and never invents a new evidence identity.

The mediator must not expose overwrite or delete operations. Administrative
provider authority may be broader than the application contract, so credential
custody, request audit, exact-operation tests, and the PostgreSQL append-only
ledger are mandatory compensating controls.

## Credential boundary

At minimum, provision three non-human, bucket-scoped Hippius credentials:

| Credential | Permitted application use | Forbidden recipients |
| --- | --- | --- |
| Private-input curator | offline publish and verification | Platform runtime, validators, executors, miners |
| Private-input reader | exact Platform read or signing only | validators, executors, miners, CI, developers |
| Evidence mediator | exact reserved upload plus full verification | validators, executors, miners, CI, developers |

Provider scopes must be the narrowest Hippius exposes and limited to one named
bucket. Application code must further prohibit list, delete, overwrite,
cross-bucket access, bucket mutation, public ACLs, and arbitrary keys.

Credential values and client-side wrapping keys live only in a reviewed secret
manager or KMS. Terraform may create secret containers and access bindings but
must not place secret payloads in state. Personal access, including a standing
grant for a maintainer, is not part of this architecture. Rotation must support
overlap for reads while immediately preventing new publication with the retired
credential.

## Required provider canary

No real private release may be registered until a confirmation-gated canary
proves all of the following against the deployed Hippius endpoint:

- anonymous `GET`, `HEAD`, and list are denied for both buckets;
- each credential is confined to its intended bucket;
- validators, executors, and candidates possess no long-lived credential;
- an exact short-lived GET succeeds and wrong bucket, key, method, expiry,
  signature, and redirect attempts fail;
- the application never performs a catalog list;
- ciphertext round-trips byte-for-byte under a full local SHA-256 check;
- malformed, truncated, oversized, stale, and digest-drifted objects fail
  closed;
- evidence overwrite, replay, delete, key reuse, and cross-kind publication are
  rejected by the mediator even if provider administration is broader;
- credential revocation prevents new operations within the documented bound;
- logs and receipts contain no URL, key, secret, object name, plaintext task,
  grader content, or decrypted bytes; and
- timeout, outage, and ambiguous-response recovery preserves exact identity and
  is classified as infrastructure rather than candidate failure.

Canary receipts are mode-`0600`, content-addressed, redacted, and reviewed
before any activation change. A provider status page, unit test, green CI run,
or successful local upload is not a substitute.

The first provider probe is
`apps/platform/scripts/probe_hippius_coding_storage.py`. Ordinary CI exercises
its transport protocol against synthetic fakes and never contacts Hippius. A
live run requires three distinct non-human credentials, two pre-existing
distinct private buckets, an absolute new receipt path, and the exact
confirmation `PROBE HIPPIUS CODING STORAGE`.

The probe writes and retains two random 4 KiB objects so the receipt describes
real stored bytes without deleting or overwriting prior state. It may retain up
to three additional small synthetic objects only when it detects an unexpected
input-reader or cross-bucket write, in which case `ready=false`. It never
creates or deletes a bucket, reads a private task, lists outside its fresh
random prefix, or returns an endpoint, bucket, key, access ID, secret, URL, or
object byte. The operator reviews the redacted receipt before any later
implementation layer treats the observed provider behavior as evidence.

A live operator must inject these variables from a protected secret boundary
without printing or persisting their values. This layer does not add a secret
retrieval wrapper:

```text
DITTO_CODING_HIPPIUS_ENDPOINT_URL
DITTO_CODING_HIPPIUS_REGION
DITTO_CODING_HIPPIUS_PRIVATE_INPUT_BUCKET
DITTO_CODING_HIPPIUS_SEALED_EVIDENCE_BUCKET
DITTO_CODING_HIPPIUS_PRIVATE_INPUT_CURATOR_ACCESS_KEY
DITTO_CODING_HIPPIUS_PRIVATE_INPUT_CURATOR_SECRET_KEY
DITTO_CODING_HIPPIUS_PRIVATE_INPUT_READER_ACCESS_KEY
DITTO_CODING_HIPPIUS_PRIVATE_INPUT_READER_SECRET_KEY
DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_ACCESS_KEY
DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_SECRET_KEY
```

Do not pass credentials as command-line arguments or store them in a repository
environment file. From `apps/platform`, the confirmed live entry point is:

```bash
uv run python scripts/probe_hippius_coding_storage.py \
  --confirm "PROBE HIPPIUS CODING STORAGE" \
  --output /protected/new-hippius-probe-receipt.json
```

## Activation order

Implementation must remain in independently reviewable, default-off layers:

1. Hippius provider contract and pinned capability probe;
2. client-side encryption and offline curator publication;
3. Platform exact-object retrieval and ticket-bound unwrap;
4. one-time evidence mediator and full-byte finalization;
5. secret custody, rotation, and redacted control-plane verification;
6. synthetic single-validator shadow canary;
7. one settlement-bound artifact across three independent validators; and
8. owner-reviewed real private release registration.

Every worker, scorer, catalog, evidence, readiness, weight, and emission gate
remains false through the contract and infrastructure layers. A later
activation requires exact merged source, deployed identity, canary receipts,
an active private catalog, three certified validators, and explicit operator
authorization. Coding rewards require a new contract version and separate
policy; they never arise from this storage decision.
