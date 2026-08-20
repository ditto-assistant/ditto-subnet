# Coding pristine-grader shadow core

`internal/codinggrader` implements step 3 of the DittoBench Coding
[private execution protocol](../../../research/dittobench-coding-datagen/docs/PRIVATE-EXECUTION-PROTOCOL.md).
It is a shadow-only deterministic core: no scorer route calls it, no private
catalog bytes ship in the repository, and its result cannot affect a benchmark
score or validator weight.

## Trust boundary

The grader accepts only:

- one validated grader manifest;
- one authoritative `codingrunner.FrozenSubmission`;
- a new stream of the exact visible capsule;
- a separate stream of the digest-bound grader bundle;
- an injected trusted `Executor` supplied by the later sandbox layer.

It never accepts the authoring workspace or a miner-provided diff. The runner
reconstructs the visible capsule into a new mode-0700 directory, recomputes the
base tree, rebuilds the canonical structured patch, verifies every transition,
applies exact full-file bytes, and requires the resulting tree and changed-path
root to match the frozen submission.

Grader material is materialized only after replay, in a second protected
directory. It is never copied into the candidate repository. The executor
receives candidate and protected paths separately and must mount the candidate
tree read-only, keep the protected tree inaccessible to candidate processes,
deny network, kill complete process groups, and derive completion/counts from a
trusted parent-owned channel rather than candidate stdout.

The package deliberately provides no default executor. An incomplete
integration therefore cannot run candidate code on the scorer host or expose
hidden tests in the candidate mount.

## Deterministic evaluation

The manifest contains one fixed build command and exactly these sorted groups:

```text
adversarial
fail_to_pass
hidden
integrity
pass_to_pass
```

The sorted order is the canonical evidence representation. Execution is
fail-fast in semantic order: build, `fail_to_pass`, `pass_to_pass`, `hidden`,
`adversarial`, then `integrity`. Each group binds an opaque command ID, argv,
timeout, and exact expected count. A canonical plan digest commits those
fields, the selected case/variant, visible capsule, base tree, test manifest,
images, execution timeout, resource profile, and order. A separate
resource digest commits both materialization limits and the combined candidate,
protected, staging, scratch, memory, PID, and CPU ceilings.

Before reading either artifact, the injected executor must attest its concrete
instance, image, `linux/amd64` platform, plan, resource profile, network denial, read-only candidate
mount, hidden protected mount, and process-group isolation. Every build/test
receipt echoes the exact command digest and executor instance. Receipts run in
execution order and form a domain-separated hash chain; the final root is part
of canonical cross-language grader evidence, together with the exact receipt
count required for a resolved result. Output, prose, patch similarity, and
self-reported test claims do not score.

Setup runs under the validator lease deadline. Only after pristine replay and
protected materialization does the separate plan-bound execution timeout begin.
Parent or lease expiry is validator infrastructure; only expiry of that
execution timeout is a candidate repair failure.

Before execution the grader commits a domain-separated integrity root over the
candidate tree and protected grader tree. It recomputes both afterward.
Candidate-tree mutation is attributable integrity failure; protected-tree
mutation is control-plane failure unless later sandbox evidence proves a
candidate escape.

The result carries canonical `codingcontract.GraderEvidence` and exactly one
terminal domain:

- `resolved`: required build and every test pass, exact counts agree, and both
  trees remain intact; score `1_000_000` micros;
- `repair_failure`: build/test failure or trusted candidate-timeout receipt;
- `validator_infrastructure`: the injected executor or trusted snapshot layer
  fails, including setup/deadline failure before candidate execution becomes
  authoritative;
- `control_plane_integrity`: manifests, capsules, frozen identities, expected
  counts, evidence, or validator-controlled protected grader bytes disagree;
- `candidate_integrity`: the candidate repository tree mutates during grading.

The package does not invent `task_invalid` from an ambiguous candidate run.
That domain belongs to the upstream calibration/quarantine layer, where a
reproducible base, gold, environment, or curator defect can be established
without letting a candidate-caused collection failure escape scoring.

No LLM decides correctness and no secondary diagnostic can rescue a failed
repair. Destruction of both temporary roots uses bounded retries and marks the
result as validator infrastructure if cleanup still fails. A production
integration must additionally run an external host sweeper for permanently
failed temporary roots before it can retain selected grader bytes safely.

## Validation

```bash
cd services/dittobench-api
go test -race ./internal/codingrunner ./internal/codinggrader ./internal/codingcontract
go vet ./internal/codingrunner ./internal/codinggrader ./internal/codingcontract
go test ./...
```

The synthetic tests prove fresh replay, delayed protected-bundle
materialization, candidate inability to see the protected path, exact group
order/counts, fail-fast behavior, resolved evidence, preflight and receipt
binding, build/test failures, phase-aware deadlines, executor failure, count
drift, candidate/grader mutation, bundle and patch tampering, resource and plan
drift, and fixed identity roots. They contain no production task or
grader bytes.
