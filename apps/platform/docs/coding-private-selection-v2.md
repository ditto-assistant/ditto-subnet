# Coding private selection v2

Status: proposed shadow-only selection contract. This document does not issue
leases or select a live private release.

For one frozen artifact, Platform derives a post-commit selection seed from the
immutable release commitment, artifact SHA-256, selection-policy revision, and
designated future finalized block. It selects six complete groups from six
distinct opaque repository strata, then schedules all five V0-V4 arms.

```text
6 groups x 5 arms = 30 isolated sessions
6 sessions per V0-V4 condition
```

The signed private authority binds artifact, release, six group commitments,
repository epochs, condition commitments, replicas, quorum policy, and the
selection derivation. Miners receive only one blinded arm at a time. Condition,
group, release, source, and unselected-task identities remain private.

The selector must use a rolling balanced-incomplete-block schedule so each
repository stratum receives comparable long-run exposure. It must reject a
release with fewer than six eligible strata, duplicate groups, incomplete
V0-V4 arms, expired audit evidence, or an unavailable finalized-block anchor.

Selection is deterministic for audit, unpredictable before the artifact is
frozen, and never rerolled by a miner or validator.
