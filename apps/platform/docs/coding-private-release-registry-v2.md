# Coding private release registry v2

Status: implemented shadow-only Platform registry. The registry does not enable
private-object access, task selection, a worker, scoring, weights, or emissions.

PostgreSQL is the authority for a private Coding release. Hippius stores only
the encrypted object bytes named by the registry.

One registered release binds:

```text
opaque release ID and release manifest SHA-256
catalog commitment and encrypted transport-manifest SHA-256
audited group-manifest SHA-256 values
opaque repository stratum IDs
wrapping-key and publication-receipt digests
release status, retention policy, and shadow-only policy revision
weight_eligible = false
```

Registration is append-only and never makes a release selectable. A later,
separately reviewed activation layer could reference only a registration whose
audited package, exact-read publication receipt, provider profile, and operator
approval all agree. A release may be quarantined or retired but its historical
registry evidence is never rewritten.

The registry does not store raw private source, memory, hidden grader, reference
patch, wrapping private key, Hippius credential, or miner-visible object URL.

## Persisted authorities

`coding_private_v2_releases` stores one immutable initial registration per
opaque corpus release ID. It binds the registration, private release, catalog,
Merkle root, payload, transport, wrapping key, provider probe, private-input
authority, curator signing key, publication source, publication receipt, and
verified object-count identities. The canonical registration authority is
digest-only JSON; the full publication receipt and its per-object evidence are
validated at the API boundary but are not stored in PostgreSQL.

The registry profile accepts at most 10,000 publication objects. The current
release has 652; this bound leaves room for future v2 curation while preventing
an administrative request from expanding toward the transport format's much
larger theoretical ceiling.

Every row is constrained to:

```text
coding_contract_version = 2
shadow_only = true
selectable = false
weight_eligible = false
```

`coding_private_v2_release_events` stores at most one quarantine and one
retirement event per release. Events carry the exact expected registration
digest, a canonical event digest, actor, reason, and the same permanent safety
flags. A retirement is terminal; a quarantine cannot be appended afterward.
Database triggers reject every update and delete on both tables.

## Operator API

The admin-only, `Cache-Control: no-store` surface is:

```text
GET  /api/v1/admin/coding-private-v2-releases
POST /api/v1/admin/coding-private-v2-releases/register
POST /api/v1/admin/coding-private-v2-releases/quarantine
POST /api/v1/admin/coding-private-v2-releases/retire
```

Registration requires the canonical registration authority, complete canonical
publication receipt, curator Ed25519 public key, substantive actor/reason, and
an exact confirmation phrase. Before inserting, Platform verifies:

- registration and receipt self-digests;
- catalog, Merkle, payload, transport, wrapping-key, and receipt linkage;
- the curator public-key fingerprint;
- the curator signature over the exact v2 publication signing authority; and
- that the publication timestamp is not in the future.

The confirmation includes the opaque release ID, registration digest, and
curator key digest so the operator must approve the exact authority. Lifecycle
confirmations bind the release ID and expected registration digest. Idempotent
replays return the original row; identity or audit drift fails closed.

The response omits the publication receipt, per-object evidence, curator public
key, signature, provider coordinates, and storage keys. It reports only safe
registration digests, lifecycle status, and audit metadata.
