# Hippius sealed Coding evidence mediator

## Status

This is a default-off Platform primitive. It adds no HTTP route, lifespan
construction, worker caller, secret-manager/KMS implementation, deployment,
score, weight, or emission path. Coding contract v1 remains permanently
`weight_eligible=false`.

Hippius is the only remote object store. Validators, executors, candidates,
models, CI, and developers receive no Hippius credential or reusable signed
upload URL. The trusted Platform mediator alone may perform exact evidence
`GET` and `PUT` operations with the dedicated evidence identity.

## Durable authority

`coding_sealed_evidence_reservations` fixes one evidence kind for an exact
ticket and claim generation before provider I/O. It binds:

- validator and worker-instance authority plus the ticket deadline;
- plaintext SHA-256 and byte count;
- ciphertext SHA-256 and byte count;
- opaque remote-key digest;
- wrapping-key, AES-GCM AAD, and envelope digests; and
- the canonical identity digest.

The table stores no bucket, endpoint, raw object key, object URL, credential,
plaintext, ciphertext, nonce, or wrapped data key. One
`(ticket_id, claim_generation, evidence_kind)` can reserve only one immutable
identity. Exact replay is idempotent; drift conflicts.

`coding_sealed_evidence_finalizations` is a separate append-only row. It can be
created only after the mediator downloads the entire stored ciphertext and
recomputes its size and SHA-256. Both tables reject update and delete through
database triggers.

## Preparation and recovery

`prepare_hippius_sealed_evidence` creates a fresh AES-256-GCM key and 96-bit
nonce for one object and sends only that data key plus the AAD digest to an
external wrapping boundary. The canonical AAD binds the ticket, claim,
validator, instance, deadline, evidence kind, reservation, plaintext identity,
and wrapping-key identity.

The returned `HippiusSealedEvidencePreparedObject` contains the exact
ciphertext, nonce, and wrapped key required for replay. The caller must commit
that prepared object to the existing protected evidence outbox before calling
the mediator. A retry must reopen the same prepared bytes; it must never rerun
candidate work or create a new encryption identity after a reservation exists.
This PR deliberately does not add the outbox serialization or KMS adapter.

## Provider mediation

The remote key is derived only from the reservation UUID, evidence kind, and
ciphertext SHA-256. The mediator:

1. appends or replays the exact PostgreSQL reservation;
2. reads the derived key with the evidence identity;
3. reuses only byte-identical ciphertext or uploads when absent;
4. refuses conflicting existing bytes without overwriting;
5. downloads the complete stored object again and verifies size and SHA-256;
6. rechecks the ticket deadline; and
7. appends or replays the finalization.

The provider adapter exposes no list or delete method, disables ambient proxy
configuration, performs no bucket mutation, and redacts provider identity from
errors. An ambiguous upload leaves the PostgreSQL reservation intact. Recovery
with the same prepared bytes observes and verifies the stored object before
finalization.

## Activation boundary

Construction requires a fresh successful Hippius capability-probe receipt
whose sealed-evidence authority fingerprint matches the endpoint, bucket, and
mediator access ID. Ordinary tests use fake transports, ledgers, and wrappers
and never contact Hippius.

The custody layer adds an owner-only Platform ciphertext spool, public-key-only
wrapping, dedicated Secret Manager containers/Ansible boundaries, default-off
factory construction, rotation-safe replay, and redacted readiness. No current
endpoint or worker calls the resulting runtime; live operation still requires a
separately reviewed synthetic canary.
