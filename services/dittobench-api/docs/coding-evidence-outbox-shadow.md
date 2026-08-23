# Durable shadow coding evidence outbox

`internal/codingoutbox` is the validator-local persistence core for canonical
authoring transcripts, replayable frozen patches, and the exact signed shadow
publication request/acknowledgement bytes. It is deliberately unwired: no
endpoint, worker, Platform client, Compose volume, score, or weight path imports
it.

The private record schema is now `dittobench-coding-evidence-outbox-v2` so the
reserved publication capacity and acknowledgement commitments cannot be
misread as the earlier artifact-only shape. No production composition root has
activated either schema; there is no live spool migration in this PR.

## Authority and lifecycle

The caller reserves the signed maximum transcript and patch bytes plus
record/staging overhead before it publishes a workspace capability. Private
shadow attempts additionally reserve two bounded publication requests and two
bounded acknowledgements; certification remains evidence-only. Reservation is
keyed by purpose, execution, ticket, case, and profile identity; an exact retry
returns the same attempt and any artifact, harness, authority, deadline, or
limits drift conflicts. Certification and private shadow attempts use distinct
purpose domains.

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

Shadow publication adds a checksummed sub-state without weakening that evidence
state machine:

```text
ready -> authoring request -> authoring acknowledgement
      -> terminal request  -> terminal acknowledgement -> released

terminal_without_patch
  -> terminal request -> terminal acknowledgement -> released
```

The authoring acknowledgement persists the exact freeze ID but is only a
midpoint. A gradeable patch cannot prepare its terminal request before that
acknowledgement, and a shadow record cannot transition to `released` until the
terminal acknowledgement and exact terminal evidence digest are durable.

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

Evidence and publication bytes are written to a random mode-`0600` staging
inode, bounded and hashed, sealed mode `0400`, synced, and installed with
`RENAME_NOREPLACE`. Existing
objects are accepted only after their type, owner, mode, link count, byte count,
and digest are reverified. Object directories and staging are synced before a
checksummed mode-`0600` record is atomically replaced and its directory synced.

The frozen object key stores exactly the canonical patch bytes. Publication
objects store the exact signed JSON request and exact verified response bytes;
records retain only their content-addressed identities and binding-critical
authority. The store revalidates required ticket, run, artifact, evidence,
freeze, acceptance, and permanent `weight_eligible=false` fields on every
restart. Shadow publication additionally requires the run-manifest digest to
equal the reservation's `authority_sha256`; authoring and terminal stages must
share all run authority except their stage-specific evidence digest. Full
Pydantic projection, evidence-digest construction, and validator
signature creation remain responsibilities of the trusted Python adapter
before the bytes enter this store. Binding-scoped
metadata lives in the record because identical patch bytes can legitimately
belong to different transcript, base-tree, and final-tree identities. Loading
reconstructs the complete submission and reruns
`codingrunner.ValidateFrozenSubmission` under the reserved signed limits.

No record contains memory bundles, grader bytes, artifact URLs, credentials,
provider secrets, workspace paths, or miner-controlled storage names.

## Retention

Capacity is reservation-based, includes bounded record/staging and publication
overhead, and reconciles orphan bytes on restart; it cannot evict unpublished
evidence.
Directory scans have cardinality and batch limits. `Pending` exposes sealed
evidence; `PendingPublications` exposes only the next exact replayable request
per attempt. `Release` enforces a durable terminal acknowledgement for shadow
attempts; an authoring-freeze midpoint is insufficient. Changed requests,
acknowledgements, authority, or terminal evidence conflict. Released and
expired records have separate bounded retention windows. An object is removed
only after no retained record references it and its orphan grace period has elapsed.
Clock rollback fails closed.

The future transport must send the exact bytes returned by `OpenPublication`,
not rebuild them through an HTTP client's `json=` convenience path. When it
stores the verified response it must also supply the request object's SHA-256;
the journal commits that correlation so a response from another signed request
cannot be attached by ticket/run identity alone.
Endpoint paths remain fixed by the Platform client; the terminal agent path is
derived only from the journal's canonical `agent_id`, never from stored URL
input.

Any error after a publication object starts committing marks physical capacity
unknown and blocks further allocations in that open store. The owner must close
and reopen the private spool so checksummed record recovery and physical
reconciliation decide whether the transition committed; it must not guess or
send a newly rebuilt request.

## Integration boundary

A future local gateway owns reservation, transcript/frozen persistence,
terminal sealing, constructing and signing publication bytes, invoking the
Platform client, host sweeping, and the production volume location.
Certification integration must reserve only after health and seed succeed;
pre-reserving for unsupported miners would leak capacity until expiry.

This store now provides self-contained byte replay for prepared shadow
publication requests and verified acknowledgements. It does not call Platform,
create signatures, request grading leases, classify terminal failures, or
schedule attempts. `Release` retains the terminal evidence digest supplied by
the trusted terminal builder and cross-checks it against the acknowledged
terminal request.

Coding contract v1 remains permanently `weight_eligible=false`.

## Validation

```bash
cd services/dittobench-api
go test -race ./internal/codingoutbox
go vet ./internal/codingoutbox
go test ./...
```
