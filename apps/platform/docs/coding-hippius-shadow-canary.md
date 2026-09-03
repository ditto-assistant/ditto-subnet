# Hippius synthetic single-validator shadow canary

## Status

This layer defines the phase-6 canary orchestration contract. It is dormant by
construction: there is no HTTP route, scheduler, factory caller, deployment
flag, or worker activation. Ordinary tests inject fake retrieval, executor, and
evidence boundaries and make no Hippius, unwrap, KMS, or external execution
request.

A successful test run is not a provider canary. The first operational receipt
must come from this exact source after it is merged and deployed, using a fresh
matching provider-probe receipt, a separately published synthetic encrypted
release, the reviewed external unwrap service, the phase-separated trusted
executors, the Platform evidence runtime, and one explicitly selected
validator.

## Fixed authority

`HippiusShadowCanaryPlan` binds one nonzero canary UUID to:

- the exact 40-character repository source SHA; the runner separately accepts
  the independently observed deployed source SHA, which must match;
- one authoring ticket, claim generation `1`, validator hotkey, stable canary
  instance, and deadline shared by every phase;
- one encrypted private-input commitment, selected index, transport manifest,
  publication receipt, and complete expected plaintext-record digest; and
- a reserved `hippius-synthetic-canary-*` corpus release whose task remains
  `weight_eligible=false`.

The retriever's redacted provider-authority digest must equal the private-input
authority recorded by the phase-5 custody runtime. The evidence receipts must
reproduce the same probe-receipt digest. Authority drift fails before a ready
canary receipt can be written.

## Phase separation

The canary retrieves the same committed record twice through the ticket-bound
Hippius retriever:

1. authoring receives the issue, model-visible runtime policy, budgets, runner
   plan, and task-version digest; it does not receive the grader plan or
   protected resource profile;
2. the exact authoring transcript and frozen submission are sealed through the
   Platform evidence spool and mediator;
3. grading performs a new exact retrieval under a grading-phase authority and
   must observe the identical record digest; and
4. grading receives the grader plan, protected resource profile, frozen
   submission digest, and task-version digest; it does not receive the issue or
   other authoring material.

Authoring and grading are separate injected executor interfaces. Both must
report a resolved result, and grading must report pristine execution. The
outcomes must echo the exact execution-authority and task digests, while the
grading outcome must additionally echo the frozen-submission digest. The
contract publishes three immutable evidence classes in order:

- `authoring-transcript`;
- `frozen-submission`; and
- `terminal-publication-request`.

Any failed phase produces no ready canary receipt. Already prepared ciphertext
remains in the protected phase-5 spool for exact inspection or recovery; the
canary exposes no overwrite, deletion, bucket listing, or alternate-provider
operation.

## Redacted receipt

`write_hippius_shadow_canary_receipt` exclusively creates an absolute,
owner-controlled mode-`0600` file. It records only source and authority digests,
hashed canary/ticket/validator identities, input and execution digests, and the
three URL-free sealed-evidence receipts. It contains no endpoint, bucket,
credential, object key, presigned URL, raw UUID, validator hotkey, corpus name,
task text, grader material, transcript, patch, or terminal-evidence bytes.

The receipt is canonical, content-addressed, and self-validating. It always
states `synthetic_only=true`, `single_validator=true`, `worker_active=false`,
and `weight_eligible=false`. The exact confirmation is:

```text
RUN HIPPIUS CODING SHADOW CANARY
```

## Remaining activation boundary

The separately reviewed operator layer adds a default-off one-shot command and
fixed protected-process adapters for an external unwrap boundary and distinct
authoring/grading helpers. It still supplies no helper implementation,
synthetic release, credential value, probe/public-key file, route, scheduler,
deployment, or live invocation. See
[`coding-hippius-canary-operator.md`](coding-hippius-canary-operator.md).

Neither layer applies Terraform, converges Ansible, restarts Platform, enables
the existing Coding worker, registers a real private release, or changes
scoring, weights, or emissions.

Phase 7 remains blocked until an owner reviews a ready receipt produced by one
explicit validator from exact merged and deployed source. Phase 7 then requires
one settlement-bound synthetic artifact to agree across three independent
validators; it must not reuse this single-validator receipt as quorum evidence.
