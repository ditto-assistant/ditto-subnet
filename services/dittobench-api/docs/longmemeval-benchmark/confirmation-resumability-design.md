# Confirmation bundle resumability and durable checkpointing

Status: **design only — nothing in this document is implemented.** It exists so
the implementation is mechanical and so the three invariants that make naive
checkpointing unsafe are stated before anyone writes the migration.

## The loss this addresses

One v9 LongMemEval confirmation bundle is ~1,200 serial provider calls
(1,152 reader + 48 judge for a 48-case selection) issued under
`allow_fallbacks: false`. `Executor.Execute` emits no partial score: per its own
contract, "any dataset, harness, judge, accounting, fallback, or deadline
failure aborts the bundle."

So a single terminal failure at case 47 of 48 discards 47 already-decided,
already-paid-for case outcomes. The reservation settles in full regardless — a
failed attempt costs the same ~$14 as a successful one, including on a 200 ms
502. At a daily cap of 5 attempts that is ~$70/day of worst-case spend that can
produce zero scored bundles.

The receipt-free re-dispatch fix (merged, live since 2026-10-23) removes the
largest *cause* of those aborts: judge-lane failures that selected no endpoint
and issued no receipt are now retried in place. Measured on production right
after activation, the judge lane failed 7/146 (4.79%) and **every one** of those
failures was both `failed_without_provider` and `failed_without_cost` — exactly
the re-dispatchable signature.

That fix does not make the bundle resumable. A receipted failure, a reader
timeout (observed as `failed_max_duration_ms=90023`), or a host restart still
discards the whole run. Checkpointing is what converts those from
"lose 1,200 calls" into "lose the one case that failed".

## Why the obvious seam does not work

`Executor.OnCase(completed, total int)` already fires exactly where a checkpoint
would be written. It cannot be used:

```go
// OnCase is optional privacy-safe progress: completed cases out of the
// selected total. It must not observe question IDs, prompts, or answers.
OnCase func(completed, total int)
```

A resume ledger must name *which* cases were decided, and the executor
deliberately "keeps the raw selected IDs private". So checkpointing needs a new,
separately-scoped trusted seam — not a widening of `OnCase`. Any patch that
starts by adding a `questionID` parameter to `OnCase` is reintroducing a
privacy boundary that was put there on purpose.

## Three invariants a resume must not break

### 1. Provider sessions are zero-start, per session — not per bundle

`Execute` opens with `readBudgetSnapshot(ctx, profile, e.Meter, true)`, and
`requireZero` rejects any lane whose counters are non-zero:

```go
if requireZero && !zeroProviderCounters(value) {
    return nil, fmt.Errorf("provider lane %q did not start at zero", value.Lane)
}
```

A resumed bundle runs in a **new** `ProviderSession`, which also starts at zero.
Therefore the bundle's true accounting is the *sum across attempts*, while every
in-process ceiling check (`requireLaneHeadroom`, `validateBudgetSnapshot`) sees
only the current attempt.

Consequence: resumability without a bundle-level ledger lets a bundle exceed its
frozen per-bundle request/token/cost ceilings by running N attempts that are each
individually under the ceiling. **This is the one invariant change the feature
actually requires, and it must be explicit:** ceiling enforcement moves from
per-session to per-bundle, with the per-session check retained as a cheap
early abort.

### 2. Monotonicity is only meaningful within one session

`validateMonotonicSnapshot` compares consecutive snapshots from the same meter.
Across an attempt boundary the counters legitimately drop to zero. Resume logic
must never compare the last snapshot of attempt N with the first of attempt N+1;
it must compare per-attempt totals against a durable per-bundle accumulator.

### 3. Evidence must bind which attempt decided each case

`ExecutionResult.Digest(profile)` attests the run. If a resumed bundle merges
outcomes from an earlier attempt without binding them, the signed evidence
asserts work the current session did not perform and cannot prove.

The resume ledger holds question IDs, which are private. So:

- the **private** ledger (Platform-side) holds
  `(bundle_id, attempt_id, question_id, correct, receipt_set_sha256)`;
- the **public** evidence gains only `resume_ledger_sha256` plus per-attempt
  decided-case counts.

A verifier can then confirm that the attempt counts sum to the selection size
and that the ledger digest matches, without ever seeing a question ID.

## Proposed shape

### Durable ledger (Platform, Alembic)

```sql
CREATE TABLE confirmation_bundle_case_outcomes (
    bundle_id           uuid        NOT NULL REFERENCES confirmation_bundles(id),
    question_id         text        NOT NULL,
    correct             boolean     NOT NULL,
    attempt_id          uuid        NOT NULL,
    receipt_set_sha256  text        NOT NULL,
    reader_requests     bigint      NOT NULL,
    judge_requests      bigint      NOT NULL,
    cost_usd_micros     bigint      NOT NULL,
    recorded_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (bundle_id, question_id)
);
```

Append-only. `PRIMARY KEY (bundle_id, question_id)` is the idempotency guard: a
case decided twice is a bug, and the insert must conflict rather than overwrite,
because the second decision would be unpaid-for or double-counted.

### Executor seam

```go
// OnDecided is trusted-side durable checkpointing. Unlike OnCase it DOES
// observe the question ID, so it is only ever wired to Platform-side storage,
// never to public evidence, logs, or heartbeats. A non-nil error aborts the
// bundle: a case we cannot durably record must not be treated as decided.
OnDecided func(ctx context.Context, outcome Outcome, acct CaseAccounting) error

// Resume lists cases already decided by an earlier attempt of this bundle.
// Listed cases are skipped entirely: no seed, no run, no judge, no spend.
Resume map[string]Outcome
```

Ordering requirement: `OnDecided` must commit **before** `Execute` continues to
the next case. Writing it asynchronously reintroduces the loss this feature
removes, because the crash that discards the outcome is exactly the crash that
discards the un-flushed write.

### Accounting across attempts

`Validate` gains a bundle-level accumulator supplied by the caller:

```
bundle_total(lane) = carried_forward(lane) + this_session(lane)
```

and the frozen ceiling is checked against `bundle_total`, not `this_session`.
`carried_forward` comes from summing the ledger's per-case accounting columns,
which is why those columns are in the table rather than being derivable.

## What must be tested before this ships

Each of these is a test that fails on the unfixed implementation, not a
description of intent:

1. **Ceiling cannot be evaded by splitting.** A bundle resumed N times where
   each attempt is individually under the frozen request ceiling, but whose sum
   exceeds it, must be rejected. This is the invariant-1 regression and it is
   the one a naive implementation will ship broken.
2. **A case is never decided twice.** Resume with a ledger that already contains
   case X; assert the harness receives no seed and no run for X, and that the
   provider meter shows zero delta attributable to it.
3. **An unrecordable outcome aborts.** `OnDecided` returning an error must abort
   the bundle rather than continue, and the already-written ledger rows must
   remain valid for the next attempt.
4. **Evidence binds the ledger.** Two bundles with identical outcomes but
   different attempt partitions must produce different digests.
5. **No question ID escapes.** Assert that `OnCase`, the heartbeat
   `confirmation_progress` JSONB, and the public evidence contain no question
   ID after a resumed run.

## Explicitly out of scope

- Resuming *within* a case. A case is the atomic unit; a partially-seeded case
  is re-seeded from scratch under a fresh isolated user ID.
- Changing the per-case judge or reader protocol.
- Relaxing `allow_fallbacks: false`.

## Relationship to judge substitution

Independent. Judge-model substitution (see `judge-substitution-study.md`)
changes *which* model issues the 48 judge calls; resumability changes *what
survives* when one of the ~1,200 calls fails. Both reduce the cost of a bundle,
but only resumability changes the failure mode.
