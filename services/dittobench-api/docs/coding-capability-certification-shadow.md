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
grader receipt root, issuance time, and expiry. Its states are:

- `unsupported`: coding is not advertised; keep the miner core-only;
- `failed`: the miner advertised coding but failed a candidate-attributable
  health, seed, run, freeze, or grade check;
- `certified`: the exact artifact completed the canary and pristine grade.

Consumers validate the known-field digest with `Receipt.Validate()` and must
also call `Receipt.ValidateAt(now)` before treating a persisted receipt as
active.

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

## Remaining integration boundary

The package consumes interfaces for the already-started harness, source-bound
capability publisher, immutable bundle store, and one trusted executor that
implements both visible authoring commands and pristine grading. This PR
intentionally does not add the Platform persistence/screening adapter. That
later layer must tie the receipt to the screened image digest,
expire it when the artifact changes, and keep core qualification separate from
coding qualification.

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
```
