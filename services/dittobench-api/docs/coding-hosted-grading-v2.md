# Native hosted grading v2

Status: implemented grader and concrete sandbox adapter, opt-in and unwired.
No listener, worker, private release, provider key or scoring path is activated.

`codinggrader.GradeHosted` combines native v2 pristine replay with the existing
attested sandbox, fixed commands, trusted result receipts, integrity checks and
cleanup. It does not relabel a v1 grade. The old grade/manifest/resource/plan
APIs and their five-group contracts retain their original byte identities.

## Authority and result

The caller supplies `HostedGradingAuthority` from the committed Platform
assignment and freeze ledger, independently of the input plan and submission:

- evaluation/attempt UUIDs and assignment digest;
- exact frozen-patch digest;
- exact grading-plan digest; and
- assignment deadline, which must match the plan's runtime deadline.

Wrong authorities are rejected before sandbox preflight or private input reads.
The visible bundle must replay to the frozen final tree before the protected
grader opener is invoked. That opener must still verify the active grading grant
and retrieve/decrypt only its exact Hippius object. A digest is not a credential.

The plan, resource envelope, contract and execution receipts use v2 hash/schema
domains. Two required groups, sorted `hidden` and `visible`, execute in the order
`visible` then `hidden`. Both must be complete and successful. Test counts must
be positive and bounded; missing groups cannot be padded with synthetic success.
Build, candidate-workspace and protected-grader integrity checks remain required
according to their declared policy. Failed setup/execution/cleanup cannot become
a resolved task merely because some test receipts passed.

`HostedResult` is private, unsigned grading evidence with a v2 schema,
`shadow_only=true` and `weight_eligible=false`. It is not the bounded signed
validator terminal response. Its nested evidence is deliberately rejected by
the v1 evidence validator. A future Platform terminal path must seal and verify
the evidence before publishing only an approved result projection.

## Concrete executor

`codingexecutor.NewHostedGrading` and `PhaseFactory.HostedGrading` select the
native plan explicitly. The legacy constructor cannot infer v2 from a manifest.
Native grading refuses authoring mode and certification-fixture opt-in. Preflight
still requires a pinned non-fixture image, rootless isolated daemon, declared
resources and observed read-only/hidden mounts and network denial.

The adapter refuses a mismatched preflight plan and any build/test command or
expected count outside its pinned plan, before creating a container. It cannot
serve as an authoring executor. Every test uses the trusted test-driver entry
point and the existing nonce-bound, root-owned report protocol; candidate stdout
is never treated as an authoritative test count. The internal supervisor ABI
remains v1; that stable process-isolation ABI is separate from grading semantics.

## Remaining live prerequisites

The public supervisor image's fixture driver is not a production test driver.
This change does not turn it into one. Private language/repository driver images
must run actual tests, isolate candidate execution and report observed outcomes.
They require reviewed source/build provenance and exact image digests.

The Platform worker must resolve verified v2 runtime/resource/grader artifacts
into these complete plans, commit start, quiesce authoring and revoke inference,
commit the patch freeze, obtain the grading grant, and seal/finalize evidence.
The compact private resource file alone is not the complete sandbox envelope.
No public local-practice score or fixture report can substitute for that flow.

Tests cover native authoring-to-grading replay, group order, immutable authorities,
tampered counts/receipts, failures, source integrity, driver restrictions and the
concrete adapter with injected Docker observations. These tests do not prove live
driver deployment, Hippius/KMS availability or a completed private evaluation.
