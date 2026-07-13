# Pre-prod final modifications plan

Status: LANDED except P4 (2026-07-10).

- **P1 (write-then-read lifecycle cases)** — landed in `dittobench-datagen`
  (`gen/lifecycle.go`; known vector updated to `2c22245a…`).
- **P2 (per-agent seed, N1)** — landed: the platform derivation already bound
  the agent id; the job ticket now exposes the seed's block hash and the
  validator re-derives and refuses a mismatch
  (`ditto/validator/onchain_seed.py`, cross-repo pinned vector).
- **P3 (grader false-negative audit)** — landed: `cmd/graderaudit` in
  `dittobench-datagen`; first normalization fix (post-deletion decline
  phrases) already applied. Labeling passes remain a human process.
- **P4 (multi-seed champion confirmation)** — PARKED, not on main: full
  implementation (K-indexed CRN seeds, median dethrone fold, ledger
  confirmation composites from the audit log) lives on the
  `nick/p4-multi-seed-confirmation` branches of `ditto-subnet` and
  `ditto-platform`, per the decision to freeze validator-flow changes before
  launch. Rebase against main before merging (the branches also snapshot P2).
- **P5 (calibration column)** — landed: `calibration_brier` / `calibration_n`
  on the public leaderboard API, a detail-panel stat in the dashboard, and
  `confidence` documented in the starter kit's PROTOCOL.md.
- **P6 (point-in-time questions)** — landed in `persona/questions.go`
  (`QTPointInTime`, dates share the renderer's exact time anchor).

Original plan follows. Scope: last changes to land before the first live
scoring run, plus two that can safely follow it. Speed/latency scoring is
explicitly out of scope: wall-clock measurements depend on the validator's
network conditions and hardware, so folding them into the composite would break
the "same transcript, same score, any machine" guarantee. Token counts remain
available as advisory telemetry only.

Ordering constraint that shapes everything below: `protocol.BenchVersion` stays
at 2 until the first live scoring run (`protocol/epoch.go`). Any change that
alters dataset bytes is nearly free before launch (update the pinned known
vector, no ledger to re-score) and expensive after (version bump plus a full
re-score sweep). So dataset-shape changes (P1, P6) go first, scoring-policy and
platform changes (P2–P5) have no byte impact and can trail.

---

## P1. Write-then-read lifecycle cases (dataset change, before launch)

**What.** Today `save_memory` / `memory_update` / `memory_delete` are graded as
routing decisions only. Add a case family that grades the write path end to
end: an instruction case tells the agent to store, update, or delete a fact,
and a later case asks a question that is only answerable if the write actually
landed in the harness's own store. No published memory benchmark grades this
loop.

**Design.** Three chain shapes, roughly 2 chains per full run (6 cases total,
drawn from the memory budget):

- Save chain: instruction case in wave 0 carries a per-seed coined value V
  (`coinToken(seed, "memwrite-save-"+ordinal, n)`, wrapped in a plausible
  sentence from the persona pools, e.g. a project codename). Read case with
  `RunAfterWave = 1` expects V as a `value` answer.
- Update chain: the haystack seeds value A in wave 0; the instruction case says
  the value changed to B; the read case expects B with A in
  `DistractorAnswers`. This is the supersession test applied to the write path.
- Delete chain: the haystack seeds a fact; the instruction case orders its
  removal; the read case is an `AnswerDecline` (the existing abstention
  machinery). Answering with the deleted value scores zero via
  `DistractorAnswers`.

Anti-hardcoding: each chain also coins a bait value that is never instructed;
it goes in `DistractorAnswers` on the read case, so a harness that
pattern-matches "write instruction" phrasing and echoes tokens gets caught.
Instruction phrasing goes through a grammar (`persona.Expand`, following
`datagen/grammars.go`) with the usual router-keyword leak-guard test.

**Where.**
- `dittobench-datagen`: new `gen/lifecycle.go` emitting `gen.StagedCase` pairs;
  wire into `GenerateMemorySuite` (`gen/memory_v2.go`) so chains draw from the
  `prof.Mem` budget and stratification stays intact. Instruction cases are
  `protocol.MemoryCase` with a new field group (e.g. `WriteSpec { Tool,
  RequiredArgs }`, validator-internal, never sent to the harness) graded on the
  transcript's `tool_calls`.
- `grade`: a write-instruction case scores on tool selection plus argument
  containment (the coined value must appear in the call args); the read case
  scores through the existing `grade.Memory` path unchanged. The read case is
  the real signal; weight the instruction case like any other memory case.
- `dittobench-api` runner: no structural change needed. The wave loop in
  `cmd/dittobench-api/main.go` (casesByWave, staged Tier-C ingestion) already
  executes cases strictly wave by wave, and memory cases already run with the
  tool catalog attached. Chains just require `Waves >= 2`, which the medium and
  full profiles already have; the small profile skips the family.
- Tests: determinism (`TestSameSeedSameBytes`), a new leak test asserting the
  coined value never appears in any haystack wave, grader unit tests for all
  three shapes including the hedge cases, and an updated pinned hash in
  `gen/publicvector_test.go`.

**Effort.** 2–4 days. Highest-value item in this plan; also the one that most
needs to land before launch, because it changes dataset bytes.

## P2. Per-agent dataset derivation, N1 (scoring policy, before first live run)

**What.** Close the answer-sharing channel: two colluding miners scored in the
same epoch currently see datasets from the same seed. Derive the initial-score
seed per agent instead.

**Design.** Mirror the CRN construction in `ditto/validator/crn.py`:

```
agent_seed = int63(sha256(epoch_seed_bytes || agent_id || bench_version))
```

The platform computes it at job issuance (`POST /validator/job`) and publishes
`epoch_seed` alongside; the validator worker independently re-derives the seed
before scoring and refuses a mismatch (same trust posture as the existing
tarball sha256 cross-check). Ledger entries already record the seed, so
reproducibility is unchanged: regenerate from the recorded seed as today.

Fairness note to document: per-category quotas are stratified
(`stratifiedCategoryOrder`, `weightedTypeQuota`), so per-seed difficulty
variance is bounded; and dethrone decisions already move to common CRN seeds
(P4), so no ranking flip ever rests on two agents having seen different data.
Run `cmd/gstudy` over a seed sweep and publish the seed-variance component as
evidence rather than assertion.

**Where.** Platform job issuance, `ditto/validator/worker.py` (derivation
check), `crn.py` (shared helper), MINER-FAQ and the anti-gaming addendum
(mark N1 landed). No datagen change, no byte change.

**Effort.** 1–2 days, mostly plumbing and docs.

## P3. Grader false-negative audit (tooling + process, before launch)

**What.** Deterministic grading's weak spot is a correct answer phrased outside
the normalization tables. Measure that rate and publish it, in the spirit of
the SWE-bench Verified / WebArena Verified audits.

**Design.**
- New public tool `cmd/graderaudit` in `dittobench-datagen`: input is an
  artifact JSON plus a transcript dump; output is every memory case scored
  zero at step 4 (the typed answer check — steps 1–3 disqualifications are
  excluded by design, those are the adversarial checks working) with question,
  expected answer, and response text, as a labeling sheet.
- Process: before launch, run the reference harness plus 2–3 model variants
  over ~20 seeds, label every step-4 zero (target ≥100 labels), fix the
  normalization gaps found (alias tables in `protocol.AnswerValue`, list-item
  paraphrase tolerance, duration phrasing), and re-run until the measured
  false-negative rate is published-worthy.
- Publish: per-answer-kind false-negative rate in the public docs, refreshed
  per bench version. Aggregate counts can ride in `RunDetails` (additive
  telemetry, outside the signature contract).

**Effort.** 1–2 days tooling, plus labeling time. Normalization fixes change
grading (not bytes), which is free before the first live run.

## P4. Multi-seed champion confirmation (scoring policy, can trail launch)

**What.** The composite stderr handles within-run noise; seed-to-seed variance
is the other component. Require a challenger to beat the champion on median
composite over K=3 common seeds before a dethrone.

**Design.**
- Extend `crn.py` to derive K seeds per pairing: fold an index into the hash,
  `crn_seed(agent_ids, version=v, k=i)`.
- Platform: when a fresh score clears the 5% margin against the incumbent,
  enqueue confirmation runs for both agents on the K shared seeds instead of
  flipping immediately. Confirmation entries land on the same public signed
  ledger, tagged with a pairing id.
- `weights.py`: `compute_weights` stays a pure fold; the dethrone comparison
  (`_beats`) uses the median composite over a pairing's confirmation entries
  when they exist, single entries otherwise. The existing stderr indifference
  band applies to the medians.

**Effort.** 2–3 days across platform and validator. No dataset change, so this
can land in the week after launch without a version bump, but the `_beats`
change should be specced now so the ledger schema (pairing id) ships correct.

## P5. Calibration column (trivial, before launch)

`CalibrationBrier` already exists in `RunDetails` (advisory, never folded).
Surface it as an unscored leaderboard column and document the optional
`confidence` field in the starter kit README. Zero gaming risk, and almost no
agent benchmark reports calibration at all. Under a day.

## P6. Point-in-time questions (dataset change, before launch if time allows)

New memory modality in `persona/questions.go`: "as of <date>, what was X?"
resolved against an update chain's timeline (timestamps already derive
backward from `DatasetEpoch`, so the dates are deterministic). Expected answer
is the chain value in force at that date; the other chain values are
distractors. Goes beyond LongMemEval's temporal categories (which ask about
order and change, not state-at-arbitrary-date). 1–2 days including grammar and
tests; changes bytes, so same before-launch preference as P1.

Deferred, explicitly: automated 2PL-driven quota rebalancing (run `cmd/gstudy`
manually before launch and hand-tune any saturated category; automate in v3),
and anything latency-based (see scope note above).

---

## Sequencing against the July 13 open-source date

| When | Items | Why |
|---|---|---|
| Before first live scoring run | P1, P6 | Change dataset bytes; free now, a version bump later |
| Before first live scoring run | P3 fixes, P2 | Grading/policy changes with no ledger to invalidate |
| Launch week | P5 | Trivial, good launch-day artifact |
| Week after launch | P4 | Pure policy layer over the ledger, no byte impact |

Every dataset-affecting change lands with: known-vector update, determinism
tests green, leak-guard tests for any new grammar, and a line in the public
README. If P1 or P6 slips past the first live run, it waits for bench_version 3
rather than shipping mid-ledger.
