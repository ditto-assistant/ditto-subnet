# Durable shadow coding evidence outbox

`internal/codingoutbox` is the validator-local persistence core for canonical
authoring transcripts and replayable frozen patches. It is deliberately
unwired: no endpoint, worker, Platform client, Compose volume, score, or weight
path imports it.

## Authority and lifecycle

The caller reserves the signed maximum transcript and patch bytes plus bounded
record/staging overhead before it publishes a workspace capability. Reservation
is keyed by purpose, execution, ticket, case, and profile identity; an exact
retry returns the same attempt and any artifact, harness, authority, deadline,
or limits drift conflicts. Certification and private shadow attempts use
distinct purpose domains.

One attempt moves through:

```text
reserved -> collecting -> ready -> released
                      \-> terminal_without_patch -> released
reserved past deadline -> expired
collecting at deadline + bounded finalization grace -> expired
```

`terminal_without_patch` is intentional. A valid freeze failure can have an
authoritative transcript but no replayable patch, so restart recovery must not
misclassify every transcript-only record as an incomplete write. `StoreFrozen`
atomically seals a successful submission; `Seal` persists failures and verifies
exact success retries. No success has a crash window between patch storage and
the ready record.

The outbox guarantees recovery only after an object and its checksummed record
have committed. It cannot resume an active workspace or transcript writer:
those still belong to the ephemeral runner session and `/tmp`. The future
gateway must `Reserve`, durably enter `collecting` with `BeginTranscript`, and
only then publish the workspace capability or call the harness. After freeze it
streams into that writer, commits the transcript, stores the patch, destroys the
authoring session, and combines the sealed artifacts with its separately
durable model/grader evidence and signed Platform envelope. Restart recovery
turns an abandoned partial collection into non-rerunnable `expired`, never back
into `reserved`.

An already-started writer may finish through `FinalizationGrace`, allowing
cancellation-resistant freeze cleanup without an unbounded lease extension.
Commit, frozen storage, sealing, live sweeping, and restart recovery use the
same exclusive cutoff; exact already-durable retries remain idempotent after it.
This grace protects local cleanup only; it does not extend Platform ticket or
remote-submission authority.

## Filesystem boundary

The root is a pre-provisioned absolute mode-`0700` directory owned by the
scorer user. The implementation walks every ancestor with `openat` and
`O_NOFOLLOW`, retains directory descriptors, validates root device/inode before
commits, and takes an exclusive process lock. A second live opener fails.

```text
root/
├── .staging/
├── objects/sha256/aa/<remaining-digest>
└── records/<binding-digest>.json
```

Evidence is written to a random mode-`0600` staging inode, bounded and hashed,
sealed mode `0400`, synced, and installed with `RENAME_NOREPLACE`. Existing
objects are accepted only after their type, owner, mode, link count, byte count,
and digest are reverified. Object directories and staging are synced before a
checksummed mode-`0600` record is atomically replaced and its directory synced.

The frozen object key stores exactly the canonical patch bytes. Binding-scoped
metadata lives in the record because identical patch bytes can legitimately
belong to different transcript, base-tree, and final-tree identities. Loading
reconstructs the complete submission and reruns
`codingrunner.ValidateFrozenSubmission` under the reserved signed limits.

No record contains memory bundles, grader bytes, artifact URLs, credentials,
provider secrets, workspace paths, or miner-controlled storage names.

## Retention

Capacity is reservation-based, includes bounded record/staging overhead and
reconciles orphan bytes on restart; it cannot evict unpublished evidence.
Directory scans have cardinality and batch limits. Pending evidence records are
deterministic and bounded. `Release` is an idempotent local transition that the
future gateway may call only after complete terminal evidence is durably
accepted; an authoring-freeze midpoint is not sufficient. Changed terminal
evidence conflicts. Released and expired records have separate bounded
retention windows. An object is removed only
after no retained record references it and its orphan grace period has elapsed.
Clock rollback fails closed.

## Integration boundary

A future local gateway owns reservation, transcript/frozen persistence,
terminal sealing, publication-envelope durability, acknowledgement, host
sweeping, and the production volume location. Certification integration must
reserve only after health and seed succeed; pre-reserving for unsupported
miners would leak capacity until expiry.

`Pending` exposes sealed evidence records for reconciliation, not a complete
remote request. Model/grader evidence and the exact signed Platform envelope do
not exist in this store yet, so this PR does not claim self-contained remote
publication replay after restart. `Release` retains only the terminal evidence
digest supplied by that later durable layer.

Coding contract v1 remains permanently `weight_eligible=false`.

## Validation

```bash
cd services/dittobench-api
go test -race ./internal/codingoutbox
go vet ./internal/codingoutbox
go test ./...
```
