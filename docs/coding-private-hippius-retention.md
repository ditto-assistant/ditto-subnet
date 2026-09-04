# Coding private Hippius metadata, retention, and recovery profile

Status: normative for private Coding object handling; default-off; not a
provider activation, deployment instruction, or weighting change.

This document is a companion to `coding-private-hippius-data-plane.md`. Hippius
remains the sole remote Coding object data plane. PostgreSQL, signed Platform
authorities, and external key custody remain authoritative for task assignment,
object acceptance, and scoring.

## Metadata confidentiality

Client-side encryption protects contents, not necessarily object size, timing,
or access frequency. Every protected object therefore uses a random opaque key.
Names, metadata, and HTTP headers MUST NOT contain a repository, issue, miner,
condition, task-group, memory, or evaluator identifier.

The Platform MUST:

* use coarse padded ciphertext classes (16 MiB, 64 MiB, 256 MiB, then 1 GiB
  increments) where task confidentiality warrants it;
* keep endpoint, bucket, credential, and raw object identity out of miner
  inputs, model context, public receipts, and logs;
* bound access-log retention and redact storage request identifiers before any
  operator-facing evidence projection;
* batch or prefetch authorized objects when doing so does not weaken lease or
  replay protections; and
* treat size and timing correlation as residual risk, not as a property solved
  by encryption alone.

Padding does not authorize padding an object beyond resource limits or changing
the canonical plaintext digest. The acceptance record binds the original
plaintext digest, ciphertext digest, byte count, and padding class separately.

## Retention classes

| Class | Minimum retention | Destruction authority |
| --- | --- | --- |
| private task release and repository bundle | scoreability plus dispute window | release authority and key custodian |
| scoped memory and grader resources | active release plus dispute window | release authority and key custodian |
| authoring/grading/frozen-patch evidence | reproducibility and dispute window | evidence authority and key custodian |
| redacted debug output | minimum useful diagnostic period | operations authority |
| failed temporary upload | short offline-sweeper period | operations authority |
| Object Lock canary bucket | provider retention expiry plus audit record | dedicated canary owner |

The concrete durations are release-policy inputs, not hardcoded provider
lifecycle rules. Provider lifecycle behavior is not authoritative for Ditto
retention. Object Lock may be enabled only after its separate canary profile is
current; it does not replace the application acceptance record or key custody.

## Recovery and cryptographic erasure

Hippius erasure coding and provider repair are availability characteristics, not
an owner-controlled backup. The owner maintains encrypted recovery copies of
private release packages, canonical manifests, wrapped-key metadata, and sealed
evidence needed for disputes. Those copies are not an active second object data
plane and never become an unstated GCS fallback.

Each private release has explicit recovery objectives:

```text
RPO: release-policy maximum loss of accepted private objects
RTO: release-policy maximum time to reconstruct a clean release
```

At least once per release cycle, a clean-environment restore drill MUST rebuild
a selected release from owner-controlled encrypted recovery material and verify
the canonical manifest digests. A restore result is an audit artifact; it does
not activate Coding or change weights.

When physical deletion cannot be demonstrated, unique external unwrap-key
destruction may provide cryptographic erasure. It is valid only if ciphertext
copies, wrapped-key backups, and key replicas follow the same lifecycle. Key
destruction is a separately authorized, audited operation; it is never an
automatic cleanup fallback.

## Activation gates

Before competitive private Coding use, the owner must approve a current
provider profile, retention-class policy, recovery drill, metadata review, and
credential-revocation observation. Missing, expired, or contradictory evidence
keeps the relevant release default-off and non-weight-eligible.
