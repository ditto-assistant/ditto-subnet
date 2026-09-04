# Coding private authoring v2

Status: proposed shadow-only curation contract. This document does not create a
private release, upload an object, issue a lease, or alter Coding weights,
emissions, or deployment.

## Version control and delivery

Private task authoring MAY use an owner-controlled private Git repository or
another private version-history system. Reproducible provenance is required.
The delivered task snapshot MUST NOT contain `.git`, remotes, hooks, source
credentials, authoring notes, local caches, host paths, or curator-only
metadata.

```text
private source history
    -> reviewed authoring epoch
    -> sanitized immutable snapshot
    -> V0-V4 task group
    -> encryption and private release publication
```

"No Git in the dataset" means no Git metadata in miner, validator, executor,
or public release material. It does not mean discarding owner provenance.

## Curator-only provenance

Each private base-task group retains an owner-only provenance record containing:

```text
private group ID
source authority and licence review
authoring epoch identity
transformation recipe or mutation operator
curator and independent reviewer identities
reference solution identity
hidden grader identity
leakage and public-overlap audit results
retention and deletion policy
```

This record is not a miner-facing task object, public receipt, model input, or
validator scoring projection. It is encrypted owner/audit material.

## Delivered private group

One complete private group contains separate encrypted objects for:

```text
visible sanitized workspace snapshot
visible issue and runtime policy
V0 raw memory bundle
V1 raw memory bundle
V2 raw memory bundle
V3 raw memory bundle
V4 raw memory bundle
hidden grader and adversarial tests
resource profile and object commitments
```

The reference patch remains curator-only unless a future dispute process
explicitly authorizes its disclosure. A validator receives hidden grader
material only after authoring is frozen.

## Private task quality gates

Before release registration, every group MUST pass:

1. Source ownership, redistribution, and execution authorization review.
2. Secret, personal-data, credential, and proprietary-data scanning.
3. Snapshot normalization and no-Git verification.
4. Pre-fix build and deterministic fail-to-pass/pass-to-pass checks.
5. Independent hidden grader review.
6. Public benchmark overlap and source-recognition audit.
7. Gold-patch, issue-text, memory-text, and hidden-test leakage audit.
8. V0-V4 semantic consistency, validity/supersession consistency, and balanced
   visible bundle volume.
9. Calibration against weak, memory-ignoring, stale-following, and
   context-stuffing baselines.

Failure of any gate quarantines the group. Quarantined groups are absent from
selection and cannot improve a candidate score by reducing a denominator.

Release admission requires a canonical calibration authority compiled from two
base and two reference observations under one immutable runner profile. Both
base runs must build and pass every visible regression test while failing at
least one hidden test. Both reference runs must build and pass all visible and
hidden tests. Test counts and outcomes must be stable across replicates. The
receipt binds observation digests; it does not make private task material
public or make the task weight-eligible.

The private input audit opens all five canonical memory-bundle files, verifies
their manifest-bound SHA-256 values, requires an empty V0 bundle and non-empty
V1-V4 bundles, rejects credential-like content, and binds the complete memory
bundle-set digest. Each canonical bundle must fit within its declared balanced
`seeded_memory_bytes` envelope; padding and observed runtime accounting remain
validator-owned rather than being represented as fake memory records.

## Publication boundary

Only after the gates pass does the offline curator package client-encrypted
objects for the Hippius private-input bucket. PostgreSQL remains the authority
for release registration, selection, object identity, leases, and scoring.
Hippius remains encrypted object transport and persistence only.

Private task bytes, raw source identifiers, private Git history, reference
patches, hidden tests, wrapping keys, and Hippius credentials remain outside
public Git and ordinary build contexts.
