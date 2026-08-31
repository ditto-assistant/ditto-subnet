# Coding capability-certification shadow core

`internal/codingcertifier` turns `/coding/health` advertisement into an active,
artifact-bound capability result. It is shadow-only: it has no scorer route,
does not persist Platform eligibility, does not change the existing tool/memory
composite, and always emits `weight_eligible=false`.

## Compatibility rule

An agent without `/coding/health`, coding contract v1, or all required
capabilities is `unsupported`. It remains eligible for the existing core
tool-and-memory pipeline. Unsupported is not converted into a zero coding score
or folded into the current composite.

Health is necessary but not sufficient. A harness is certified only after the
trusted caller proves this exact sequence for the exact screened artifact:

```text
sandbox artifact attestation
  -> GET /coding/health
  -> POST /coding/seed
  -> repeat the identical seed and observe an idempotent replay
  -> publish a source-bound coding-runner capability
  -> POST /coding/run for one content-addressed public canary
  -> revoke the capability
  -> freeze the validator-owned workspace
  -> durably publish and verify the canonical tool transcript
  -> durably store the replayable frozen submission
  -> finalize validator-observed inference-relay evidence
  -> replay the frozen patch into the pristine grader
  -> require a resolved executable result
```

The harness attestation is owned by the trusted sandbox controller, not by the
miner. It binds the harness instance to the exact agent artifact SHA-256 and
requires a read-only root filesystem, enforced capability-only egress, no host
Docker socket, and no injected credentials.

## Content and evidence binding

`CanaryManifestSHA256` is derived from the complete non-ephemeral canary input:

- ticket, case, and profile capability identities;
- memory bundle digest and repository epoch;
- issue and constraints;
- visible bundle and base-tree digests;
- editable, creatable, and deletable path policies;
- exact visible test/build commands and runner limits;
- model/tool/wall-time budgets;
- grader plan, resource profile, image digest, and platform.

Ephemeral capability URLs and deadlines are excluded. Their authority is
separately enforced by the capability publisher, sandbox attestation, and
runtime contexts.

The sealed result binds the advertised coding versions/capabilities, artifact
and harness identities, canary manifest, frozen patch, changed-path root, final
tree, protected-path result, authoring event/transcript roots and size, pristine
grader receipt root, locked model/provider evidence, issuance time, and expiry.
The transcript proof requires successful `repo.read_file`, `repo.apply_patch`,
`tests.run`, and `git.diff` events in that order. Its states are:

- `unsupported`: coding is not advertised; keep the miner core-only;
- `failed`: the miner advertised coding but failed a candidate-attributable
  health, seed, run, freeze, or grade check;
- `certified`: the exact artifact completed the canary and pristine grade.

Wire consumers use `ParseReceipt()` so duplicate or missing known fields fail
while future unknown fields remain compatible. In-memory consumers validate the
known-field digest with `Receipt.Validate()`. Both must call
`Receipt.ValidateAt(now)` before treating a persisted receipt as active.

`certification_sha256` is a content-integrity address, not an identity
signature. The later validator/Platform adapter must bind the canonical receipt
digest to the validator ticket and signing identity before persistence.

The public canary is intentionally rehearsable and may be hard-coded by a
miner. Certification therefore proves protocol and sandbox integration only;
it is not a coding-quality score and contributes no leaderboard or emissions
weight. Private post-commit tasks measure coding quality later.

Validator infrastructure, invalid task material, and control-plane integrity
failures return an error instead of de-certifying the miner. They require an
operator retry or repair. A harness run is never retried after authoritative
workspace activity; the capability is revoked and the workspace is frozen.
A transport failure before the first authoritative event is infrastructure. A
post-event disconnect cannot earn a clean retry: the durable transcript,
trusted relay evidence, frozen patch, and pristine grade decide the result.

## Remaining integration boundary

The package consumes interfaces for the already-started harness, source-bound
capability publisher, immutable bundle store, and one trusted executor that
implements both visible authoring commands and pristine grading. It also
requires validator-local durable outboxes for the transcript and frozen
submission plus a trusted inference-evidence source; test fixtures cannot be
used by production adapters.

`internal/codingcanary` is the default-off host adapter. It loads the pinned
`certification/v1` pack, acquires a lease-shaped screened harness, activates
the existing source-bound inference gateway from an exchanged claimed-lease
grant, runs this certifier, and always revokes the gateway, workspace route,
and harness. The lease pack hash is the public-canary identity; the receipt
`canary_manifest_sha256` is this package's execution digest. They are not
required to be equal. Certified receipts still require observed inference
evidence; unused inference still yields a persistable failed receipt. Public
canary dispatches use the lease UUID as dispatch `ticket_id` and settle on
`coding_certification_inference_requests`, not the private-ticket ledger. The
host constructs the adapter only when `DITTOBENCH_CODING_CANARY_ENABLED` is set
and the certification pack root is present. Flags stay false until a separately
reviewed activation.

Coding contract v1 remains permanently shadow-only. A separately reviewed
contract v2, calibration result, and owner-approved emissions policy are
required before coding can receive weight.

## Validation

```bash
cd services/dittobench-api
go test -race ./internal/codingcertifier ./internal/codingexecutor \
  ./internal/codingrunner ./internal/codinggrader ./internal/codingcontract
go vet ./internal/codingcertifier ./internal/codingexecutor \
  ./internal/codingrunner ./internal/codinggrader ./internal/codingcontract
go test ./...
bash ../../scripts/test-coding-starter-practice-e2e.sh
```
