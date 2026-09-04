# Coding private release registry v2

Status: proposed shadow-only Platform registry contract. No database migration,
release registration, or private-object access is enabled by this document.

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

Registration is append-only. A release becomes selectable only after its
audited package, exact-read publication receipt, current provider profile, and
operator approval all agree. A release may be quarantined or retired but its
historical registry evidence is never rewritten.

The registry does not store raw private source, memory, hidden grader, reference
patch, wrapping private key, Hippius credential, or miner-visible object URL.
