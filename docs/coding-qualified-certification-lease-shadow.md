# Qualified coding-capability certification lease (shadow contract)

## Status

This is the planned control-plane contract for a shadow-only coding capability
certification. It defines the missing bridge between a core-qualified normal
submission and the existing validator-owned coding canary.

It does not enable a worker, issue a private coding task, modify the active
Tool + Memory score, alter validator weights, or make coding contract v1
weight-eligible.

## Why a separate lease is required

`/coding/health` is only an advertisement. The stronger coding certifier must
also verify a scoped seed, validator-owned workspace calls, revoked capabilities,
frozen workspace evidence, locked inference evidence, and pristine grading.

Normal screening occurs before Tool + Memory scores exist. It must therefore
not create a coding certification, infer coding quality, or issue a coding
task. Likewise, a normal canonical scoring ticket must not silently double as a
coding-canary authorization: the two workloads have different artifacts,
budgets, capability routes, evidence, and failure semantics.

The Platform must instead issue one explicit certification lease only after it
has verified current normal core qualification for the same immutable artifact.

## Preconditions for issuance

Within one Platform transaction, an issuer must require all of the following:

1. A configured, shadow-only core-qualification policy for the requested
   benchmark version.
2. The latest complete qualification observation is `qualified` and binds the
   same agent ID, source artifact SHA-256, screened-image SHA-256, benchmark
   version, and current policy revision.
3. The screened image remains complete, content-addressed, and available to
   the validator through its existing short-lived image capability.
4. No active or terminal certification lease already exists for the same
   `(agent, artifact, screened image, benchmark, coding contract)` identity.
5. A current public canary manifest and public-only resource profile are
   available for the requested coding contract.

An absent, stale, partial-wave, unqualified, expired-image, or policy-revision
mismatch is not an error against the normal submission. It simply makes the
agent ineligible for a certification lease at that time.

## Lease authority

The issued lease must be validator-specific and bind at least:

```text
lease ID and deadline
validator hotkey
agent ID
agent artifact SHA-256
screened image SHA-256, config digest, reference, and upload ID
normal benchmark version
coding contract version
core-qualification observation ID and policy checksum
public canary manifest SHA-256
runner-plan, grader-plan, resource-profile, and locked-inference-policy digests
weight_eligible = false
```

The lease may grant only the capabilities needed for the public canary:

- a short-lived screened-image capability;
- a scoped memory bundle and visible workspace bundle;
- a validator-owned coding-runner route;
- the locked, ticket-scoped inference route; and
- the phase-specific protected grader capability after authoring is frozen.

It must not contain private catalog membership, a private repository, hidden
tests, a production coding assignment, a provider credential, or a reward
weight.

## One-way lifecycle

```text
qualified artifact
  -> issued
  -> claimed by the named validator
  -> certification terminal evidence persisted
  -> lease resolved | terminal failure | expired
```

Claiming is exclusive. Once validator-owned authoring activity begins, neither
an HTTP timeout nor a process restart may create a clean rerun. The existing
coding outbox, workspace freeze, and terminal-evidence rules determine the
result. A validator may replay only byte-identical completed publication, never
repeat a new authoring attempt under the same lease.

`unsupported` is a valid terminal certification result when the image returns
`404` for `/coding/health`. It means normal-only support and has no effect on
normal scores. A malformed coding advertisement, candidate-attributable canary
failure, or integrity failure is a terminal certification result for that
artifact only. Control-plane or sandbox infrastructure failure leaves a typed
retry/recovery record and never de-certifies the normal submission.

## Certification result

The validator submits the existing signed, append-only coding certification
receipt. Platform accepts a new receipt only if its lease identity and all
artifact, qualification, canary, and deadline bindings match exactly. Exact
receipt replay is idempotent even if the qualification later changes; a new
lease always re-evaluates current qualification.

A `certified` receipt is protocol-and-sandbox evidence only. It enables a
future default-off shadow coding admission check for the same artifact; it is
not a coding-quality score and cannot change emissions.

## Implementation order

The implementation must be split after this contract:

1. Platform persistence and authenticated issue/claim/abort endpoints for the
   qualified certification lease, including migration-order and stale-binding
   tests.
2. Validator integration that claims one lease, launches the screened image in
   the existing hardened sandbox, runs `codingcertifier`, revokes every route,
   and publishes the exact terminal receipt.
3. Platform receipt acceptance that verifies the lease binding and exposes
   operator-only certification state.
4. A separate default-off coding-assignment integration. It may admit only a
   currently core-qualified artifact with a current certified receipt.

Every stage remains shadow-only. Any reward activation requires a separately
reviewed contract version, calibration evidence, and owner-approved emissions
policy.
