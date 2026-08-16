---
name: longmem-confirmation-rollout
description: Drive Ditto LongMemEval confirmation from an observed production failure through bounded diagnosis, fail-closed implementation, independent subagent review, exact semantic release, managed-validator adoption, and one accepted signed Platform-verified canary. Use for LongMem confirmation bring-up, opaque dimension_execution failures, zero or mixed provider-lane evidence, harness isolation, confirmation release monitoring, fleet rollout, or requests to keep working until LongMem succeeds live.
---

# LongMem Confirmation Rollout

Treat live accepted evidence as the finish line. A green PR, successful release,
healthy validator, or completed provider call set is only an intermediate proof.

## Orient

Run the repository router with the user's task text, then read the benchmark and
release indexes it selects:

```bash
python3 .agents/skills/ditto-subnet-context/scripts/lookup-context.py \
  --max-topics 5 "$ARGUMENTS"
```

Use these companion skills when their boundary is active:

- `ditto-subnet-benchmark` for scorer, harness, evidence, relay, and frozen profile semantics.
- `ditto-subnet-platform` for policy, tickets, persisted evidence, API, or schema changes.
- `ditto-subnet-release-ops` for exact release identity and fleet activation.
- `ditto-subnet-worktree` and `github` for isolated implementation and PR publication.
- `wandb-ops` and `gcloud-ditto-platform-db-readonly` for live read-only diagnosis.

Read [the evidence ladder](references/evidence-ladder.md) before making a live
success claim. Read [the role prompts](references/independent-review-prompts.md)
before delegating implementation or review.

## Establish the exact state

Build one timestamped state table before changing code:

1. Source: current `origin/main`, active fix PR head/base, and whether its worktree is clean.
2. Review: exact-head CI plus independent semantic, security/accounting, contract/release, and test verdicts.
3. Delivery: merge commit, semantic tag, release commit, Platform deploy, immutable validator/scorer/stack digests.
4. Fleet: exact source/descriptor/scorer identity, protocol, health, availability, admission, scorer verification, and updater failure/rollback state.
5. Issuance: active policy revision, shadow/enforce mode, daily attempt and spend caps, outstanding tickets, and existing claims.
6. Evidence: bundle/ticket terminal state, dimension rows, provider request chronology, signed root, signature, verification, qualification, and public result.

Never infer one rung from another. In particular:

- A green PR is not merged or released.
- A successful release is not fleet adoption.
- A claimed ticket is not an accepted confirmation.
- Completed providers are not signed evidence.
- A dashboard card is not the authoritative database or signature record.

## Diagnose a failure read-only

Correlate the same bundle, ticket, validator, slot, release, and time window across:

- Platform policy, budget, bundle, ticket, dimension, and evidence rows.
- Provider request rows by lane, status, token counts, cost, and completion time.
- Validator heartbeat and updater identity.
- W&B console/history and safe structured failure class/status.
- Scorer or host logs only when authorized and necessary.

Localize the boundary before editing. Distinguish at least:

- seed versus first `/run` versus reader versus judge;
- received harness response versus transport/read/oversize/context failure;
- provider availability versus scorer evidence assembly;
- dimension execution versus report preparation versus signing/verification;
- zero, positive, partial, poisoned, or mixed provider-lane evidence.

Do not expose submitted bodies, URLs, case IDs, credentials, private artifacts,
or arbitrary exception strings. Low-cardinality allowlisted diagnostics must not
change score, retry ownership, or settlement.

## Implement the smallest fail-closed repair

Use `ditto-subnet-worktree` to create a fresh `origin/main` worktree for a new
repair, or import and resume the exact existing `gh stack` for an active PR.
Never edit the source checkout. Preserve user work and frozen LongMem
profile/install/launch bytes unless a deliberate versioned contract change is
required.

For every proposed repair:

1. State the exact producer authorization and the independently replayable wire shape.
2. Preserve transport, cancellation, provider failure, partial receipt, identity drift, fallback, and cap violations as fatal.
3. Keep canonical provider requests, receipts, tokens, and cost; never fabricate or relabel evidence.
4. Make positive-path evidence byte-stable when it is not intentionally changed.
5. Update Go producer/replay, shared Python models, and Platform verification together when evidence semantics change.
6. Prove old/new skew fails closed and release ordering deploys consumers before producers when required.
7. Add an integration test that reaches the next confirmation dimension or report boundary, not only a constructor unit test.

For untrusted harness lifecycle changes, require per-container authentication,
source and generation binding, revoked admission before stop, verified process
and network removal, admitted-handler drain, and no cross-case or cross-runtime
spend. A counter observed after dispatch is not automatically a canonical receipt.

### Preserve the known LongMem zero forms

Apply these decisions before inventing a new retry or evidence shape:

- Unused-reader judged zero: when every selected case returned a judgeable harness response, `receivedFailures == 0`, reader is exact canonical zero, and judge has exactly one successful fully receipted request per selected case with frozen identity, no fallback, and all caps respected, the trusted executor must force every outcome incorrect before aggregation. Preserve the positive judge receipts, tokens, and cost and emit the dedicated reader-zero/judge-positive exact-zero form. Only the private producer authorization may mint it; ordinary evidence constructors must reject it.
- All-received-failures zero: when every selected case ended in a sealed received harness failure and both frozen provider lanes are canonical zero, use the separate dedicated both-lanes-zero authorization. Do not mix this provenance with the unused-reader form.
- Any received-failure mixture, missing/partial/extra judge receipt, reader-positive/judge-zero reversal, identity or fallback drift, cap violation, nonzero score, transport/read/oversize/context failure, or provider ambiguity remains fatal.

Both accepted zero forms must replay identically in Go, shared Python, and
Platform. Neither may erase real provider accounting or grant score credit.

## Use independent agents deliberately

When the user requests subagents or independent review, keep roles bounded and
non-overlapping. The implementation owner may edit one isolated worktree. Review
agents inspect a clean exact-head checkout and do not edit it.

Run these reviews in parallel after local focused gates:

- Semantic/retry: classification, cancellation, retry ownership, score behavior, lifecycle ordering.
- Security/accounting: untrusted replay, credentials, receipt completeness, spend amplification, privacy.
- Contract/release: Go/Python/Platform parity, frozen assets, affected components, rollout order, old/new skew.
- Tests/adversarial: exact production shape, malformed/partial variants, races, full affected suites.

Require each reviewer to report `APPROVE` or `BLOCKER` against the exact head SHA
with evidence and commands. A blocker owns the merge gate until fixed. After an
amendment, re-run affected tests and obtain exact amended-head verdicts; prior
approvals do not transfer automatically.

## Publish and deliver

Use noninteractive `gh stack` through the repository `github` skill. PR publication
does not authorize merge. Before calling a PR ready, record:

- exact head and base;
- clean worktree and diff check;
- focused, race, contract, and release-plan gates appropriate to the diff;
- every independent review verdict;
- GitHub check state, including whether an older failure is superseded.

After authorized merge, follow one verified ancestry chain:

```text
PR head -> merge commit -> released source (same commit or verified descendant)
        -> semantic release commit/tag -> immutable Platform artifacts
        -> immutable scorer/validator/stack artifacts -> exact managed validator
```

For evidence-wire or verifier-semantic changes, shared/Platform verification is
the consumer and scorer/validator evidence emission is the producer. The affected
plan must select both sides. Deploy and live-verify Platform first, then promote
the immutable scorer/validator stack. Old producer with new consumer is safe; new
producer with old consumer must fail closed. Block stack promotion when that
ordering or live consumer identity cannot be proved.

Do not canary a warning, stale, draining, resource-constrained, mismatched, or
rollback-marked validator. Require multiple fresh healthy/available/accepting
heartbeats, `fresh_verified` scorer support, exact descriptor/source, and an idle
updater with no failed candidate.

## Run exactly one bounded canary per exact repair

First look for an already-issued LongMem ticket. Never issue a duplicate merely
because the UI is slow. Respect live daily attempt/spend caps and shadow/enforce
mode; do not raise caps, requeue, cancel, or mutate policy without explicit
operator authority.

Observe one eligible claim through terminal state. Capture:

- exact bundle, ticket, attempt, validator, slot, subject, settings/profile revision;
- provider rows and cost by lane;
- terminal dimension and report state;
- evidence root and digest, reporter/scorer identity, signature, Platform verification, qualification, and public visibility.

If it fails, return to read-only localization. Do not reinterpret an
infrastructure failure as an incorrect answer and do not retry past the platform
cap. If an external condition such as a UTC-day cap or expired read-only cloud
authentication blocks proof, state the exact condition and natural next window,
continue safe public monitoring, and request only the missing authority or login.

After a failure is fixed, reviewed, merged, released, and adopted under a new
exact release/profile identity, one successor canary is allowed after repeating
the duplicate-ticket, eligibility, and cap checks. Never repeat the same failed
release merely to hope for a different result unless the failure was proven
transient and the platform's bounded retry policy owns that retry.

## Completion gate

Report LongMem working only when all are true for the same exact attempt:

1. The intended released scorer and validator executed it.
2. The ticket and bundle reached the accepted/completed terminal state.
3. Required dimension evidence exists and canonical provider accounting validates.
4. The evidence root/signature is present and Platform verification succeeded.
5. Qualification/result state reflects the accepted confirmation.
6. No hidden retry, rollback, duplicate issuance, or unresolved cleanup remains.

After reporting the accepted live result, amend this skill in a follow-up PR if
the successful attempt exposed a new reusable boundary. That documentation
follow-up must not delay or redefine the live success claim.
