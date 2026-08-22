# Durable Luna relay journal

`internal/codingrelayjournal` is the validator-local filesystem implementation
of the `codingrelay.Journal` port. One store owns one private relay root and one
immutable ticket/grant/budget binding. It is an unwired shadow component: no
current validator, worker, scorer, or model relay constructs it.

## Durable state machine

The store persists three kinds of authority:

1. `state.json` fixes the complete relay binding and durable revocation bit;
2. `entries/NNNNNNNN.json` begins as the exact pre-provider dispatch marker;
3. completion atomically replaces only that marker with the settlement,
   receipt, private response projection, and miner-safe replay response.

Every record has a versioned schema, state-specific generation, strict
known-field shape, and checksum. Dispatch sequence is contiguous, retry
attempts retain the same locked request identity, and each new logical request
must advance exactly once. Only the final entry may be incomplete. Exact
`Begin`, `Complete`, and `Revoke` retries are idempotent;
binding or body drift is a typed conflict. A persisted incomplete dispatch is
returned to the relay core and therefore becomes `ErrAmbiguousDispatch`, never
a clean provider retry.

The dispatch marker is durable before the upstream receives a request. The
completion record is durable before the relay returns success. A successful
rename followed by a directory-sync error is retained as an ambiguous durable
commit in memory; the next operation re-syncs every journal directory before
serving an idempotent result. Process restart validates the installed record
from disk.

## Filesystem and capacity boundary

The configured root must be an absolute, canonical, non-root path that already
exists with euid ownership and exact mode `0700`. The store descriptor-walks
every ancestor with `O_NOFOLLOW`, retains the root and parent directory
capabilities, takes a nonblocking exclusive `flock`, and revalidates inode,
device, ownership, mode, and child-directory identity before mutations.

Records are descriptor-relative, mode `0600`, single-link regular files.
Reads use `O_NOFOLLOW|O_NONBLOCK`, bounded sizes, before/after inode checks, and
pathname identity checks. Writes use random exclusive staging files, file
`fsync`, atomic rename, destination-directory `fsync`, and staging-directory
`fsync`. Symlinks, hardlinks, FIFOs, devices, unsafe modes, unexpected names,
duplicate JSON fields, unknown fields, checksum drift, and sequence gaps fail
closed. A restart removes only validated private staging files left by the
previous exclusive owner.

`MaxEntries` cannot exceed the locked policy's request-plus-retry bound.
`MaxTotalBytes` accounts for installed record lengths and transient staging
record lengths. A new dispatch reserves enough configured headroom for the
maximum terminal entry while both the dispatch marker and replacement staging
file coexist. This
prevents an admitted provider result from becoming unjournalable because an
earlier request consumed the remaining local capacity.

The journal is not a retention scheduler. The future gateway must create one
private root per attempt, keep it through terminal evidence publication, and
delete it only under the same durable release/retention authority as the coding
evidence outbox. Host-volume encryption and backup policy remain deployment
responsibilities.

## Integration boundary

This package stores no provider credential, bearer token, live Platform URL,
hidden grader, score, or weight. The next integration layers remain:

- the trusted model-relay handler and settlement producer consumed by the
  unwired `internal/codingplatform` client;
- a validator-local capability gateway that constructs the relay and journal;
- shadow attempt orchestration and terminal publication;
- calibration and a separately reviewed versioned activation proposal.

Validation:

```bash
cd services/dittobench-api
go test -race ./internal/codingrelayjournal ./internal/codingrelay ./internal/codingcertifier
go vet ./internal/codingrelayjournal ./internal/codingrelay ./internal/codingcertifier
```
