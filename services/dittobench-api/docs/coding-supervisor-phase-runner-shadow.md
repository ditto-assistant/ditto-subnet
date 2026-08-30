# Durable shadow coding supervisor phase runner

`internal/codingphase` implements the previously injected
`codingsupervisor.PhaseRunner`. It composes phase-separated lease parsing,
validator-owned authoring, scoped memory delivery, the ticket-bound Luna
gateway, durable evidence storage, and pristine grading. Nothing constructs it
in a production composition root yet.

## Authoring order

One successful authoring call follows this order:

1. Parse the authoring lease, run manifest, runner plan, model-visible issue,
   runtime policy, budgets, and exactly three authoring capabilities.
2. Acquire a dormant harness handle. `Acquire` is forbidden from executing
   candidate code; the handle has a separate `Activate` transition.
3. Reserve the evidence outbox and commit `BeginTranscript`. This is the
   durable, non-rerunnable `collecting` marker.
4. Materialize the validator-owned runner session from the visible capsule.
   The authoring spec contains only candidate limits. The full resource
   artifact is verified internally, but protected grader limits are absent
   from the authoring lease and model context.
5. Activate the harness, verify coding-v1 health, and deliver the exact scoped
   memory projection.
6. Validate the exchanged grant against the locked Luna policy, task identity,
   effective lease budgets, broker keypair, expiry, and exact Platform proxy
   route. The relay binding's issue time is the trusted local activation time;
   its deadline is the active grant expiry.
7. Activate inference only after its authorizer observes the durable outbox
   marker, then publish the source-bound workspace route.
8. Run the harness with only the issue, runtime policy, budgets, memory already
   seeded, and the two opaque capability URLs.
9. Revoke the workspace route and inference gateway before freezing. Freeze,
   transcript export, transcript commit, frozen-patch storage, evidence
   finalization, local close, and harness destruction run even after candidate
   failure or caller cancellation. An ambiguous revocation is retried once
   through the required idempotent handles before evidence can be returned.

The authoring result is returned only when every revocation, persistence, and
destruction step succeeds. A failed harness call can still leave authoritative
transcript and patch evidence, but it never becomes a successful supervisor
outcome and cannot receive a clean authoring retry.

`changed_bytes` is the sum of the exact post-change content byte lengths in the
frozen patch. A deletion contributes zero because the frozen submission does
not carry candidate-controlled preimage bytes.

## Grading and restart

Outbox reservations are unique by `(purpose, execution_id)`. The phase runner
uses the ticket UUID as the shadow execution identity, so grading and recovery
can resolve the durable record without trusting a caller-provided object key or
remembering a process-local record ID.

Grading independently parses the freeze-gated grading lease, protected grader
plan, resource profile, authoring outcome, and run manifest. It reopens the
frozen patch from the outbox, cross-checks transcript, patch, tree, path root,
changed count/bytes, and model-grant identity, then invokes the existing
pristine `codingattempt.Runtime.Grade`. The resulting task evidence is emitted
through the authority-validating canonical serializer.

Recovery never republishes a workspace or inference capability and never
reruns candidate code. It reports exact pending publication records when they
already exist; otherwise durable collecting/reserved/ready states are
conservatively `ambiguous`, released is `released`, and expired is `expired`.
The next disabled wiring review must persist and publish the complete
authoring/terminal Platform envelopes before it can claim end-to-end
publication recovery.

## Current boundary

This package supplies interfaces for a dormant harness controller and an
inference activator. No listener, command, scheduler, worker, or validator
composition root constructs them. It does not claim work, call a live miner,
publish Platform evidence, set a score, or change a weight. Coding contract v1
remains permanently `weight_eligible=false`.

Validation:

```bash
cd services/dittobench-api
go test -race ./internal/codingphase ./internal/codingsupervisor \
  ./internal/codingattempt ./internal/codingoutbox ./internal/codinggateway \
  ./internal/codingcontract
go vet ./internal/codingphase ./internal/codingsupervisor \
  ./internal/codingattempt ./internal/codingoutbox ./internal/codinggateway \
  ./internal/codingcontract
```
