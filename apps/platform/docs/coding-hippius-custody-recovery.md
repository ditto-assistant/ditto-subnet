# Hippius Coding custody and recovery

## Status

This layer is default-off. It composes the already reviewed evidence mediator
only when `DITTO_CODING_HIPPIUS_EVIDENCE_ENABLED=true` and every dedicated
credential, protected authority file, and spool boundary validates. It adds no
HTTP route, worker caller, catalog activation, score, weight, or emission path.

Hippius remains the only object-data plane. Google Secret Manager is used only
as credential custody: Terraform creates empty, prevent-destroy containers and
never manages provider secret values. Operators add and rotate versions out of
band. No Terraform apply or secret version is part of this change.

## Protected Platform spool

Client-side encryption occurs inside trusted Platform custody. Returning the
prepared ciphertext, nonce, wrapped data key, or raw Hippius key to the
validator would create a second secret-bearing transport and enlarge the
validator trust boundary. The existing Go evidence outbox therefore remains
the authority for the exact signed source bytes, while Platform owns the exact
post-encryption replay bytes.

`HippiusEvidenceSpool` requires an absolute, owner-controlled mode-`0700`
directory. Each canonical identity gets one mode-`0700` child containing:

- mode-`0600` `ciphertext.bin`; and
- mode-`0600` canonical `manifest.json`, written last.

The manifest binds the complete sealed-evidence identity, raw derived object
key, nonce, wrapped data key, and ciphertext identity. A partial directory
without a manifest is retained for inspection and cannot publish. Exact replay
is idempotent; different bytes under an existing identity fail closed. No
delete operation is exposed.

## Wrapping-key rotation

New evidence uses the configured RSA-3072 through RSA-8192 public key with
RSA-OAEP-SHA256. The private key is structurally absent from Platform. The
wrapping public-key digest is part of the envelope and PostgreSQL reservation.

Rotation installs a new public key for new preparations. Already-spooled
objects retain their old wrapping-key identity and publish byte-for-byte
without needing the old public key. The external unwrap/KMS boundary must keep
old private-key versions readable until all associated evidence retires, while
preventing new wraps with a retired version.

Credential rotation may overlap old and new reader access during a rolling
restart, but a changed mediator access ID changes the authority fingerprint.
New publication then fails boot until a fresh provider probe binds that exact
identity; an old receipt cannot silently authorize the rotated credential.

## Runtime and process isolation

The Platform process may construct `HippiusEvidenceRuntime`; no endpoint or
worker calls it in this layer. The shared model-relay PM2 environment explicitly
clears every evidence credential, bucket, path, and enable flag. Validators,
executors, candidates, CI, and developers receive none of them.

Ansible refuses enablement unless:

- the exact Hippius endpoint and `decentralized` region are selected;
- the evidence bucket differs from upload, avatar/trace, and catalog buckets;
- dedicated Secret Manager IDs and loaded values differ from every other
  storage authority;
- the probe receipt and wrapping public key are owner-controlled mode-`0400` or
  mode-`0600` files; and
- the protected spool is mode `0700`.

Readiness is a redacted digest/status projection. It contains no endpoint,
bucket, path, access ID, secret, object key, ciphertext, nonce, wrapped key,
ticket, task, or evidence content.

## Remaining activation boundary

This layer does not install probe/public-key file contents, add Secret Manager
versions, apply Terraform, converge Ansible, register a private release, or
enable a worker. The phase-6 source contract composes this runtime with a
ticket-bound retriever and separate authoring/grading executor interfaces. It
still has no route, scheduler, deployed adapter, or invocation. A real phase-6
result requires a separately authorized synthetic single-validator run using
exact merged and deployed source and a reviewed redacted receipt.
