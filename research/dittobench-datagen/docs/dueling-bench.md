# Dueling bench (bench_version 16): generator cases in the canonical agent run

Status: **implementation in progress, OFF.** Nothing activates without an
operator rollout. This is Rollout step 3 of
[generator-competition.md](generator-competition.md): "a new bench version
draws its memory cases from the generators".

Operator decision: SN118 becomes a dueling subnet. Each agent's canonical run is
DittoBench datagen cases **plus** cases from the current top generators, scored
as one composite through the existing scoring and consensus path. Generators
also earn their own track (10% share) from rounds. Round columns are 70 cases
(`cases_per_seed = 70`).

**The version number is 16** (retargeted 2026-11-03). This design was first
written as v15, but v15 shipped and activated first as the production-scale
tool catalog and scored decision lane
([benchmark-v15-rollout.md](../../../docs/benchmark-v15-rollout.md), #3318). The
dueling bench therefore ships as `protocol.BenchVersionV16`, and its datagen half
**inherits the v15 contract** (catalog, decision lane, grader floor and gates),
with the two v15 lessons in section 8 applied. Everywhere below, "v15" in the
2026-10-27 amendments reads as **v16**, "the inherited contract" is v15, and
"accepts columns declaring 14 or 15" becomes "15 or 16" (the two-version pack
window at 16).

Rounds moved from 1 to 4 epochs on 2026-10-31 (generator settings revision
10), so one round is already about 4.8 h. A season is now
`rounds_per_season` rounds with a **default of 1** (one 4-epoch round, about
4.8 h, the same wall clock as the original "4 one-epoch rounds"). The minimum
safe season is still 2 epochs, so Backroom refuses
`rounds_per_season x epochs_per_round < 2`.

## Accepted amendments (2026-10-27)

The operator accepted sections 1 to 4 of the dueling list in
[tandem-upgrades.md](tandem-upgrades.md) ("v16: generator cases inside agent
runs across a version boundary", written as v15 at the time) as amendments to
this design:

1. Slices are graded at each column's declared pack policy
   (`grading_bench_version`), never at a fixed 13 or the run's bench version
   (section 4 below).
2. The season freezes each column's declared policy; the 32-seed battery must
   agree, and a slice that declares another policy fails like a digest
   mismatch.
3. A season belongs to one bench version and never spans an activation.
4. While a v16 rollout is open, Platform runs non-authoritative shadow seasons
   at 16 for the cohort's desired-version runs.

With the two-version window (also decided 2026-10-27), a v16 season accepts
columns declaring 15 or 16 only.

## Summary of the proposal

| Question | Proposal |
|---|---|
| Run size | 350 cases total, unchanged: 175 datagen (50 tool + 125 memory) + 175 generator (5 columns x 35). |
| Weight | `composite = gate_d x (0.5 x datagen + 0.5 x generator)`. Pinned in the v16 contract. |
| Which generators | The top 5 **eligible** generators by ledger score, one per owner, frozen per **season** (one 4-epoch round by default, about 4.8 h). A newly qualified generator can enter canonical runs about 4.8-9.6 h after qualifying. The public default generator fills empty slots. |
| Who generates | Platform's trusted `challenge-scorer serve`, through a new `slice` work kind. Validators never get generator source. |
| Seeds | `slice_seed = H(season secret, season anchor block, run seed, column)`. The season secret is committed before its anchor block exists, like round seeds. |
| Delivery | A new `dueling-slice-v1` mode on `/v2/score` carries the slice bytes and their SHA-256. The validator regenerates the datagen half from the public seed, as it does today. |
| Neutrality | Each agent's generator mean excludes its owner's columns (self-mask). The paired dethrone test excludes columns owned by **either** agent's owner (pair-mask). |
| Solvability | A column is eligible only after a scored round showed it is solvable. Each column is capped at 10% of the composite. Generator cases never feed a zeroing gate. |
| Comparability | Every agent in a season faces the same generator set (same binaries) on its own seeds. Crown and rank comparisons use only same-season scores. |
| Generator pay | Still rounds only. Canonical per-column statistics feed Backroom monitoring, not the generator ledger. |
| Audit | Slices are reproducible after a delay. Each pinned generator's exact source is published once the embargo ends, so anyone can regenerate a slice from the revealed season secret and the public run seed. |

## 1. Composition

### Proportion and run size

Today a full v13 run has 350 cases: 100 tool and 250 memory
(`profilesV13` in `gen/gen.go`, the slot table in `gen/v13_envelope.go`
and `gen/memory_v2.go`).
Composite is `0.5 x tool_mean + 0.5 x memory_mean` times the composite gate
(`services/dittobench-api/internal/scorer/scorer.go`, the `composite :=` block).

Generator packs are memory-only (`challenge/pack.go`: tool cases are a v1
non-goal). The v16 contract therefore splits the run into two halves:

| Half | Cases | Inside the half |
|---|---:|---|
| Datagen | 175 | 50 tool + about 125 memory, from the inherited contract (v15, plus the section 8 lessons) at a new "dueling" profile. Graded and gated exactly as that contract. |
| Generator slice | 175 | 5 columns x 35 cases, each graded at its pack's declared policy (`grading_bench_version`, pinned per season; see [tandem-upgrades.md](tandem-upgrades.md)). |

```
w         = 0.1                                      (column weight = 0.5 / 5, a v16 contract constant)
datagen   = 0.5 x tool_mean + 0.5 x memory_mean      (datagen cases only, before gates)
col_c     = mean case score on column c              (a failed /run is a 0, as in datagen)
U(A, M)   = columns c not in the mask M              (M = owners masked for this comparison)
composite(A, M) = gate_datagen x ((1 - w x |U|) x datagen + w x sum_{c in U} col_c)
composite(A)    = composite(A, {owner(A)})           (self-mask, the reported composite)
```

Columns get equal weight whatever their case count, so a column that loses
duplicates is not diluted. With no column masked the shares are 25% tool, 25%
datagen memory and 50% generator memory. **A masked column's 10% returns to the
datagen half** instead of being spread over the other columns (as a
`G_self` mean would), so no column ever carries more than 10% of the composite,
masked or not. This is the implementation of "each column is capped at 10%"
(decided 2026-11-03 with the v16 retarget; equal to the original formula when
nothing is masked). The pair-masked crown test (section 3) uses the same
formula with `M = {owner(A), owner(B)}` for both agents.

**Why 350 and not 700.** Rounds keep about 70 cases per column for
measurement precision. The canonical slice uses 35 per column so the run stays
inside today's ticket TTL and the 6600 s wall clock
(`.agents/skills/ditto-subnet-benchmark/references/bench-runtime.md`). A
700-case run would double validator slot-hours for every canonical,
retest and confirmation run.

**Governance.** The 0.5 weight, the 175/175 split and 5 x 35 are **contract
constants** (`protocol` and `gen.ProfileForVersion` at `>= BenchVersionV16`).
Changing them is a new bench version, like every other scoring rule. Which
generators fill the columns is operator policy, set through the generator
competition settings and the season pin (section 3). Backroom reads and writes
it with revision and confirmation guards.

### Which generators contribute

At season open, Platform freezes the season's **set**: up to 5 columns. It uses
the same ordering as `select_columns` (`apps/platform/ditto/api_server/generator_rounds.py`),
with stricter eligibility:

1. The generator is `active`, qualified, and held a **counted or challenger
   column in a scored round** inside `window_days`. A challenger column never
   moves an agent's round score, but its column statistics count here.
2. That round column has `column score > 0` and an unsolved fraction
   `u / (n - dup) <= 0.20` (at least 80% of its cases were solved by two or more
   independent owners).
3. It passes a **season qualification battery**: 32 fresh secret seeds at 35
   cases, every pack valid (`challenge.ValidateGenerated`), plus a determinism
   re-run. One failure excludes it from the season. This makes a mid-season
   generation failure very unlikely.
4. One column per owner (`owner_key` from `emission_owner`,
   `apps/platform/ditto/db/queries/scores.py`), ordered by ledger score.

The pin is the exact `submission_id` and `tree_sha256`, not the hotkey. A new
upload during a season replaces the old one in the next season only.

### New generator path

Rounds are 4 epochs (about 4.8 h) and a season is one round by default, so a
strong new generator does not wait a day:

1. **Qualify.** The upload passes probe qualification (generator-competition.md
   "Qualification").
2. **Next round.** It takes the next round's challenger column
   (`challenger_columns`, which goes to qualified generators that never held a
   column), or a counted column if one is free. `select_columns` already does
   this. The round opens at the next epoch boundary and is scored when it
   closes.
3. **Next season boundary.** If that scored column meets eligibility rules 2-3
   and its ledger score places it in the top 5 eligible owners, it is pinned at
   the next season open. The season qualification battery runs during the
   round, so it adds no wait.

Best case is one round's run (about 4.8 h, qualifying just before a round
opens). Worst case adds most of a round's wait (about 9.6 h). With
`rounds_per_season = 1` every round boundary is a season boundary, so the
season adds no further wait. A new upload from an already pinned owner
follows the same path, and its old pin keeps serving until the new one is
eligible. The Backroom early-rotation control (section 3) can bring a strong
newcomer in sooner, with the same audited confirmation as any early rotation.

### The public default generator

The default generator is owned by nobody and is never masked. It plays four
roles:

- It **fills** every slot that no eligible miner generator takes, each on its own
  column seed. A default-only slice is the first shadow configuration.
- It is the **fallback** when a pinned generator fails on one run's seed. The
  run uses a default column instead and records the substitution in the report.
  The generator is then excluded from the next season and must re-qualify.
- It is the **drift anchor**. Its column mean, across agents, separates "the
  generators got harder" from "the agents got worse" in Backroom.
- It keeps the column count fixed, so the composite always averages exactly 5 columns.

## 2. Seeding and secrecy

### Today's canonical seeds

The datagen seed is `derive_validator_seed(block_hash, agent_id, validator)`
(`apps/platform/ditto/api_server/onchain_seed.py`). It is bound to the
finalized block at job-ready, so it is unpredictable at submission and
verifiable afterwards. It is also **public**, which is fine for datagen: v13's
promise is "public seed and generator revision reproduce the dataset".
Confirmation seeds are the champion-anchored CRN family
(`apps/platform/ditto/api_server/crn.py`), block-bound from v13.

### Slice seeds

Generator source is private, and generator output is the generator miner's
asset. Two things must hold. Nobody can know a run's generator cases before the
run, and Platform cannot grind them. Reusing the round commit/reveal
(`challenge.RoundCommitment`, `RoundSeed`, `RoundColumnSeed` in `challenge/round.go`):

```
season_commitment = sha256("dittobench-dueling-season-commit/v1" || 0 || u64be(s) || secret_s)
season_seed       = H(secret_s, anchor_block_hash_s)       # anchor = season's first epoch block
slice_seed(run,c) = H("dittobench-dueling-slice/v1" || season_seed || u64be(run_seed) || column_id_c)
```

- **No precomputation.** `run_seed` is the existing datagen or CRN seed. The
  season secret stays private until reveal, so nobody, including a generator's
  owner, can compute a run's slice early. A precomputed slice would not help
  anyway: the agent is already fixed and sandboxed, with only the relay for
  network.
- **No grinding.** The commitment is published on Platform (and on chain as
  `dds1:{s}:{commitment}` when `onchain_commitment` is set) before the anchor
  block exists. The run seed is chain-derived. Platform chooses neither.
- **Reveal.** `privacy_window_rounds` (6 rounds, about 7.2 h) after the season
  closes, `secret_s`, the season seed and every slice digest become public.

### Delayed reproducibility (decided)

v13 promises that anyone holding the public seed can reproduce the dataset.
v16 delays that promise but keeps it: every slice becomes reproducible once its
generator's source is published. Publishing the source is a required part of
the contract, not an option. For generators that hold a v16 column, it replaces
the "source is never public" rule in generator-competition.md "Privacy".

- **What is published.** The exact archive pinned by `tree_sha256` for every
  generator that held a column in any v16 season, plus the challenge-scorer
  revision that built it. The public default generator is already public.
- **When.** `release_at = max(reveal of the last season that pinned it, close of
  the first season that pinned it + embargo_hours)`. `embargo_hours` comes from
  the existing source release policy (`apps/platform/ditto/api_models/artifact_release_settings.py`,
  read with `get_source_release_policy`). The reveal comes from
  `privacy_window_rounds`. So a generator that holds a column for a long time
  still becomes auditable once the embargo runs out. That is deliberate: it
  pushes generator miners to keep submitting new work, which is the point of
  the duel.
- **Activation guard.** v16 activation, and every season open, refuse to run
  while the source release policy has `disclosure = never`. A generator
  uploaded under v16 rules accepts this disclosure at upload time, through the
  same CLA and consent receipt as agent source.
- **How to reproduce.** Published source, the revealed `secret_s` and anchor
  block, and the run's public seed give the slice byte for byte. Checking that
  against the signed `slice_sha256` on the score row re-grades the run. The
  doc and miner kit ship a `challenge-scorer reproduce-slice` command for this.

Until a generator's release date, these checks protect the run:

- Validators re-run `challenge.ValidateGenerated` on every pack before scoring,
  so a pack that is malformed or ungrounded is refused.
- Validators check that each pack echoes its `slice_seed` and the pinned
  generator hotkey.
- The scorer signs the slice manifest (column, submission, tree digest, seed,
  pack digest).
- Platform keeps the slice bytes, so operators can reproduce any run in Backroom
  (artifact scope) before public release.

### Who generates, latency, cost

Validators cannot run generators, because that would distribute private source.
Platform's trusted scorer (`services/dittobench-api/cmd/challenge-scorer`,
`serve`) already builds and runs generators for qualification and rounds.
v16 adds a third work kind beside `generate` and `score` in `round.go`:

- **`slice`**: inputs are the season pin and `(run_seed, columns)`; the output is
  a canonical slice dataset (`challenge.EncodeSliceDataset`: the
  `EncodeRoundDataset` shape plus a manifest), posted with its SHA-256.
- Binaries are built **once per season** and cached by `tree_sha256`. A slice
  is then 5 sandboxed runs of milliseconds to seconds, each under the 60 s limit:
  about 5-30 s of CPU and no inference. At a few hundred canonical, retest and
  confirmation runs a day, that is well under one CPU-hour.
- **Lease-time preparation** follows the private-dataset pattern
  (`apps/platform/docs/private-benchmark-preparation.md`,
  `db/queries/private_benchmark_preparations.py`). The datagen seed needs the
  validator hotkey, so the slice cannot be built before the lease. A missing
  slice reserves a preparation row in a separate transaction, the lease rolls
  back, and Platform returns 503 with a retry interval. Concurrent requests
  reuse the row, and existing pinned bytes win. The cache key is
  `(season, run_seed)`, so quorum re-leases and retries reuse the same bytes.
- **Retention.** Slice bytes (up to `challenge.MaxRoundDatasetBytes`, usually
  1-3 MiB) are kept in Platform-owned private storage until every contributing
  generator's source is released and the score-dispute window has passed. After
  that, only digests are kept, because anyone can regenerate the bytes.
  Platform prunes on the season tick, 7 days after the later of the reveal
  and the last pinned source release.

### Transport

The existing private transport (`services/dittobench-api/cmd/dittobench-api/private_dataset.go`,
PROTOCOL.md "Private V13 control-plane transport" and "Generator-round
datasets") already carries Platform-pinned bytes with a canonical digest. v16
adds one mode:

- `datasetFeatures()` advertises `dueling-slice-v1` when screened-image mode is
  on. `validatePrivateDatasetRequest` admits it only for
  `bench_version >= 16`, requiring slice bytes plus `expected_slice_sha256`.
- The **datagen half is not sent**. The scorer regenerates it from the public
  seed, as for v13. The ticket keeps `dataset_sha256` for the datagen half and
  gains `slice_sha256`. That keeps the public reproducibility of the datagen
  half separate from the private slice.
- Execution reuses `internal/challengerun`. It gets an opaque ID projection
  with a nonce derived from `slice_seed` (not random, so reruns are
  byte-identical), and each column gets its own opaque users and memory graph.
  Slice waves are seeded after the datagen waves, on the same harness and the
  same ticket inference session.
- `generator-round-v1` stays a separate mode. Its reports never enter the
  ledger.

## 3. Fairness and collusion

### Owner neutrality

Rounds are pairwise-neutral: A and B are compared only on columns owned by
neither of them. A scalar composite cannot reproduce that exactly. Self-masking
alone still leaks: an owner whose generator is the hardest column skips it,
while their rivals face it. At 10% of the composite per column, a column that
top agents score 0.4 on against 0.85 elsewhere is worth about 0.045 composite
to that owner's agent, which is six times `KOTH_MARGIN = 0.007`. So v16 uses
two masks.

- **Self-mask (every composite).** `G_self(A)` drops columns owned by A's
  owner. The agent still runs them (one shared slice per season keeps the
  pipeline simple), but they score nothing for it. No generator can lift its
  owner's agent.
- **Pair-mask (crown and ordering).** The signed report carries per-column
  aggregates: `{column_id, owner_key, n, sum, sum_sq}`. The validator fold
  (`ditto/validator/weights.py`: `_paired_dethrone`, `_entry_seed_composites`)
  recomputes each shared CRN seed's composite for **both** agents with columns
  owned by either owner removed, then applies the existing paired z-test. Top-5
  ordering uses the same pairwise-masked comparison between adjacent ranks.
  Everything stays deterministic: columns are sorted by id and the arithmetic
  is explicit floats.

The public default generator and reference-owned columns are never masked.
Owners are identified by `owner_key` (coldkey, via `emission_owner`). Signed
owner attestations (`get_owner_attestations`) are non-transitive evidence for
copy review; they do not extend a mask (open question 5).

### Solvability

An unsolvable column must not drag down everyone. v16 guards against that in
layers:

1. **Per case**: `challenge.ValidateGenerated` checks grounding, that the gold
   answer passes, and that dumping, trivial and distractor answers fail. It runs
   at generation and again on the validator.
2. **Per generator**: eligibility needs round evidence that at least 80% of its
   cases were solved by two or more independent owners, with reference agents
   counted as independent solvers.
3. **Per run**: each column is capped at `0.5 / 5 = 10%` of the composite.
   Generator cases are excluded from every multiplicative gate (canary,
   metamorphic, efficiency, v13 shadow gates), so a generator case can lower a
   score but never zero it. This follows the honest-agent invariant in
   `docs/bench-versions.md`.
4. **Per season**: Backroom tracks each column's mean across in-season agents.
   If a column's top-5 mean falls below 0.15, or its default-relative gap is a
   5-sigma outlier, the operator can **rotate the season early** (confirmation
   phrase, audited). Columns are never swapped mid-season for some agents only.

A uniform hit affects all agents in the season about equally, so ranks barely
move. The cap bounds the non-uniform part.

### Copies and duplicates

Exact-source and behavioral-copy detection stays at upload and qualification
(generator-competition.md "Copies"). Season eligibility adds one column per
owner. Inside one slice, a fingerprint that repeats an earlier column's case is
dropped from the later column. Column means keep weights stable. A generator
that copies datagen families earns nothing in rounds (top agents find those
cases trivial), so it never becomes eligible.

### Churn and comparability

Agents scored against different generator sets are not comparable, so the
**season** is part of the score's identity:

- A season is pinned for `rounds_per_season` rounds (default 1; rounds are
  `epochs_per_round = 4`, so about 4.8 h), aligned with `SubnetEpochIndex` round
  boundaries. Every validator folds the same season id. Uploads, supersessions
  and new round scores during a season affect the **next** season only.
- Score rows record `season_id`. Crown, dethrone and rank comparisons only use
  scores from the same season.
- **Rotation is a mini-rollout.** At season open, Platform queues the champion
  first, then `retest_cohort_size` agents by rank, for continual retests on the
  new set. It re-anchors the confirmation seed anchor per
  `(champion, bench_version, season)`
  (`db/queries/confirmation_seed_anchors.py`). Previous-season scores stay
  visible as provisional for one season, then expire from the authoritative
  ledger. The crown carries over until a same-season challenger beats it.
  While a benchmark rollout is open, a rotation re-score that cannot add a
  quorum score waits on any validator that could instead lease a missing score
  for an activation-blocking priority member. Re-scores that restore an expired
  quorum row for such a member go first (`db/queries/score_retests.py`).
- A ban or copy ruling against a pinned generator mid-season triggers an early
  rotation, not an in-place swap.
- **Idle seasons extend instead of rotating** (`skip_unchanged_rounds`, on by
  default). At a due boundary the open season is extended by
  `rounds_per_season` when all of these hold:
  1. the waiting season would pin the same generators (submission, source
     digest, grading policy and default flag);
  2. the rotation cohort is the one the season re-scored when it opened;
  3. no agent created since the season opened is scored or live;
  4. no rotation was requested.

  No season opens, so the two-season window does not advance: no score
  expires and nobody is re-tested. The skip is all or nothing for the cohort,
  and the fold reads exactly the rows it read before. The waiting season is
  voided and recommitted at the new boundary, because its commitment binds its
  index. `detail.idle_extensions` records every extension
  (`get_dueling_season`). The privacy window counts from the extended close,
  so the secret is never revealed early. An agent uploaded after the first
  extension owes no season duel seeds in that season, because those seeds
  have been public since the anchor. It joins at the next rotation, which its
  upload triggers.

### Rotation cost at 4.8 h, and the minimum safe season

Inputs: v13 full runs take about 10-11 min (use 10.5). The fleet has about 20
scoring slots after the per-validator caps (8/6/4/2) and the reserved round
slots (`get_validator_slot_settings`). That gives about 114 runs per hour,
about 137 runs (24 slot-hours) per epoch, and about 548 runs per 4-epoch
season. A comparable same-season score needs `SCORING_QUORUM = 3` runs
(`db/queries/scores.py`). Full statistical-band depth needs
`TOP5_MIN_CONFIRMATION_SEEDS = 8` CRN seeds (`api_server/koth.py`).

| Rescore at each rotation | Runs | Slot-hours | Share of one epoch | Share of a 4.8 h season | Per day (5 seasons) |
|---|---:|---:|---:|---:|---:|
| Quorum only, cohort 5 (champion + 4) | 15 | 2.6 | 11% | 3% | 13 slot-h (3%) |
| 8 seeds, cohort 5 (default) | 40 | 7.0 | 29% | 7% | 35 slot-h (7%) |
| 8 seeds, cohort 10 | 80 | 14.0 | 58% | 15% | 70 slot-h (15%) |
| 8 seeds, cohort 25 (max) | 200 | 35.0 | 146% | 36% | 175 slot-h (36%) |

With the default cohort, a 4.8 h season costs about 35 slot-hours a day out of
about 480. A 29 h season would cost about 6. The extra 29 slot-hours a day is
the cost of letting new generators in quickly. Continual retests already run
on the cohort, so rotation mostly restarts their confirmation depth rather than
adding new work. If v16 runs take longer (the generator half is all memory
cases), the table scales linearly: at 13 min every number goes up 24%.

**Minimum safe season: 2 epochs (about 2.4 h) at cohort <= 10.** With
`ledger_pin_mode = epoch`, validators fold the ledger once per epoch. A
1-epoch season would put every fold mid-rotation, so the crown would always be
decided on thin or mixed-season data. Two epochs give one epoch to rebuild
depth (40-80 runs fit in 72 min even with only half the fleet free) and at
least one fold on complete same-season data. **4 epochs is the default**: one
epoch to rebuild, then three folds (75% of the season) on complete data. A
cohort above 10 needs at least 4 epochs. At 25 it needs a longer season or
`retest_eligibility_mode = statistical` to shrink the cohort. Backroom refuses
`rounds_per_season x epochs_per_round < 2`.

## 4. Scoring

- **Grading.** Slice cases are graded by `scorer.GradeMemory` at the pack's
  declared `grading_bench_version` (15 or 16 in a v16 season), which the season pins per
  column ([tandem-upgrades.md](tandem-upgrades.md)). That is the grader rounds
  use, so a column's measured round solvability carries over to canonical
  runs. Datagen cases are
  graded by the inherited contract, unchanged.
- **Composite.** As in section 1. `composite_stderr` treats columns as strata:
  `SE_G^2 = (1/k^2) x sum_c(s_c^2 / n_c)` over the k unmasked columns, and
  `SE^2 = 0.25 x SE_datagen^2 + 0.25 x SE_G^2`. Both are then scaled by the
  datagen gate, as today.
- **KOTH and crown.** The dethrone test is pair-masked (section 3). Generator
  cases are built to be discriminating, so composites will drop and spread
  out. `KOTH_MARGIN`, `KOTH_BAND_DECAY_START_COMPOSITE` and the statistical band
  cap (`apps/platform/ditto/api_server/koth.py`, mirrored in
  `ditto/validator/config.py`) get v16 values from shadow calibration, gated
  on `bench_version >= 16` and never retyped per version.
- **Continual retests.** Unchanged mechanically. Every retest of an agent in a
  season uses the season's set. Rotation is what drives retest demand.
- **Confirmation seeds.** Each CRN seed's slice derives from that seed and the
  season seed, so champion and challenger face identical generator cases and
  the pairing holds. `TOP5_MIN_CONFIRMATION_SEEDS` (8) is rechecked in
  calibration, because 35-case columns are noisier per seed.
- **LongMem confirmation.** An instrument, not a per-epoch contract. The
  existing profile confirms v16 unchanged (bench-version-bump skill, section 1b).
  **Decided 2026-11-03: the v9 confirmation-bundle lane carries no dueling
  slice.** A bundle is one LongMem and ablation execution per artifact,
  profile and generation, with no run seed and no datagen half, so there is
  nothing to slice. A v16 bundle needs no dueling fields; the lane admits 16
  once the shared evidence set (`V9EvidenceBenchVersion`) includes it.
- **Priced inference cost.** The one input that reads the whole run. The
  scorer prices the run's chat, embedding and decision-lane tokens at locked
  public rates and multiplies the composite by a factor floored at 0.6. The
  factor rides inside `details.dueling.datagen_gate`, so it scales both halves
  and leaves the column statistics raw. A generator's column means never move
  with the agent's spend, and no generator case feeds a zeroing gate. See
  `services/dittobench-api/PROTOCOL.md`, "Bench v16 priced inference cost
  factor".
- **Efficiency, outlier escalation, deferred source review, ATH anomaly.**
  Efficiency and gates use the datagen half only in v16.0. Outlier and anomaly
  thresholds are recalibrated on the v16 shadow distribution before
  activation.

## 5. Bench-version bump checklist (v16)

Everything is additive and gated `>= BenchVersionV16`. The v2-v15 vectors must
not move.

| Layer | Change |
|---|---|
| Datagen contract | `protocol/epoch.go`: `BenchVersionV16`, `datasetEpochV16`, `supportedBenchVersions`. `gen/v16_envelope.go`: dueling profile (50 tool + ~125 memory slot table, `mixaudit` gate). `gen.ProfileForVersion`. `challenge/`: `SeasonCommitment`, `SeasonSeed`, `SliceSeed`, `EncodeSliceDataset`/`DecodeSliceDataset`, `testdata/slice_vectors.json`. `grade/`: none. `docs/bench-versions.md`: the one v16 row and section. Known vectors: new v16 datagen-half vector. |
| Scorer | `internal/scoregates`: `SupportedBenchVersion` upper bound. `cmd/dittobench-api`: `supportedBenchVersions`, `private_dataset.go` (`dueling-slice-v1`), datagen+slice runner, report `generator_columns` aggregates and `season_id`. `internal/scorer`: v16 composite and stratified SE. `internal/efficiency`: `ProductionReadyForVersion` (datagen half). `internal/longmemeval/profile.go`: floor check. `relay_preflight.go`, `confirmation*.go`, `v9base` (datagen half). `PROTOCOL.md`. |
| Challenge scorer | `cmd/challenge-scorer/round.go`: `slice` work kind, per-season build cache, season qualification battery, `reproduce-slice` (published source + revealed season secret + run seed). |
| Shared protocol | `packages/ditto-screening-protocol/bench_v9.py`: add 16 to the `V9EvidenceBenchVersion` Literal (derived sets follow). New `dueling.py`: season manifest and column aggregate models used by both validator and Platform. |
| Platform | `db/models.py` + one Alembic migration: `dueling_seasons` (commitment, secret, anchor, pinned columns, reveal), `dueling_slice_preparations`, `slice_sha256` on tickets, `season_id` and column aggregates on scores, every `bench_version` CHECK reviewed as a floor. `db/queries/`: `tickets.py` (slice pin on lease, released like the round pin), `score_ranking.py` (season-scoped), `queue_order.py` and `benchmark_rollout.py` (rotation cohort), `confirmation_seed_anchors.py` (season key). `api_server/`: `validator.py` lease payload, `generator_rounds.py` (season open after a scored round), `crn.py` and `koth.py` v16 constants. Backroom tools: `get_dueling_season`, `list_dueling_slices`, `rotate_dueling_season` (confirmation phrase), `rounds_per_season` setting (default 1; a season is at least 2 epochs). Generator source release: `generator_source_releases` keyed to `privacy_window_rounds` and `embargo_hours`, published like agent source releases; season open refuses `disclosure = never`. |
| Validator | `SUPPORTED_BENCH_VERSIONS` (from shared). `dittobench.py`: forward slice bytes and SHA. `weights.py`: same-season comparisons, pair-masked `_paired_dethrone`, aggregate-aware `_entry_seed_composites`. Signed envelope carries `season_id` and aggregates. Regenerate `validator_contract.json` from Platform. |
| Screener | Screening stays datagen-only. Review version-named policy fingerprints. |
| Generated + mirrors | Regenerate `apps/backroom/src/generated/platform-api.ts` and `services/model-relay/db/schema.sql`. Hand-fix `apps/backroom/src/lib/admin.schemas.ts` and dashboard `src/types/` (per-column panel). |
| Miner kit | `local-rehearsal.py`: `MAX_BENCH_VERSION = 16`, plus a local slice from the public default generator (`challenge-scorer run`) so miners can rehearse the composite. `src/protocol.rs`: `MAX_SUPPORTED_BENCH_VERSION`. `.agents/skills/mine/SKILL.md`. |
| Release + CI | `release.yml`: the supported-set identity gate. `release/components.toml` owners for new paths. Tests that use 16 as the invalid version move to 17. |

## Phased rollout

| Phase | What happens | What is needed first | Exit criterion |
|---|---|---|---|
| 0. Prerequisites | Rounds at 70 cases per column (done, settings revision 10). At least 3 eligible miner generators across 2+ scored rounds (43 qualified on 2026-11-03). v15 active (done). | Generator competition in enforce. `owner_key` audited. | Eligibility filter produces a stable top 5 across 3 consecutive rounds. |
| 1. Contract, OFF | Datagen, scorer, challenge-scorer `slice`, Platform seasons (no issuance), validator support. All CI green. | This design approved. | Release advertises 16. v2-v15 vectors unchanged. |
| 2. Shadow canary | `issue_benchmark_canary` at v16. First default-only slices, then miner columns. Non-authoritative. | Fleet advertises `dueling-slice-v1`. Slice preparation deployed. | Slice preparation p95 < 60 s. Runs fit the TTL. No false zero on cleared top agents. Pair-mask fold vectors match on Go and Python. |
| 3. Shadow rollout | `start_benchmark_rollout` desired 16. Top cohort scored in parallel with the active version, through at least 10 season rotations (about 2 days). | Phase 2 clean. KOTH v16 constants drafted. | Crown stability and rank correlation against the active version reviewed. Per-column means stay above the floor. Rotation retest load fits capacity. |
| 4. Activate | v16 activation, and the generator track share (10%) and upload fee through governance, together. | An independently reviewed activation decision. | Not retroactive. Rollback is the inherited version, still served. |

## 6. Open questions (recommendations in bold)

1. **Weight and size. Decided:** 50/50 at 350 cases (175 datagen + 175
   generator, 5 x 35). Rounds keep about 70 per column.
2. **Datagen base. Decided:** inherit v15 (active since 2026-11). The datagen
   half is v15 at the dueling profile, plus the section 8 lessons.
3. **Season length. Decided:** about 4.8 h: one 4-epoch round by default (`rounds_per_season = 1`), with the
   Backroom early-rotation control and the new generator path (section 1).
   The minimum safe season is 2 epochs (section 3).
4. **Neutrality.** **Self-mask every composite, and pair-mask the dethrone test
   and top-5 ordering.** Banning miners who run both an agent and a generator is
   too blunt. Self-mask alone is exploitable by about 6x the KOTH margin.
5. **Owner identity for masks.** **Use `emission_owner` (coldkey), the key rounds
   already use.** Attestation links stay copy-review evidence only.
6. **Case visibility to agent miners.** **Show per-column aggregates only.**
   Never show generator case content or transcripts, so generator families
   don't leak faster than agents can generalize.
7. **Audit. Decided:** delayed reproducibility. Publishing each pinned
   generator's source after the embargo is required (section 2). Until then,
   validator-side `ValidateGenerated`, seed and digest binding, and Platform
   retention cover the gap.
8. **Canonical statistics paying generators.** **No in v16.0.** Rounds stay the
   only, pairwise-neutral, generator scorer. Canonical column means are
   monitoring.
9. **Eligibility thresholds.** **Unsolved fraction <= 20%, column score > 0,
   and a clean 32-seed battery.** Tighten if shadow shows per-seed variance.
10. **Gates on slice cases.** **Ungated in v16.0, with v13 provenance and causal
    gates in observe mode on slice cases for data.** Promote only through a
    later version with calibration.
11. **Rotation rescore budget.** **Champion first, then `retest_cohort_size`
    (keep it at 5-10 with 4.8 h seasons). Previous-season scores are provisional
    for one season.** Confirm this against measured slot capacity in phase 3.

## 7. v16 implementation contract (2026-11-03)

This section pins the names and wire shapes every layer implements. Each item
ships OFF and additive; nothing here changes v2-v15 bytes or behaviour.

### 7.1 Contract constants (`research/dittobench-datagen`)

- `protocol.BenchVersionV16 = 16`, with its own dataset epoch and rotate seed.
  `protocol.DuelingMinBenchVersion = 16`; every dueling gate is a floor on it.
- `protocol.DuelingSliceColumns = 5`, `protocol.DuelingSliceCasesPerColumn = 35`,
  `protocol.DuelingColumnWeight = 0.1`. Changing any of them is a new bench
  version.
- `gen.ProfileForVersion(run_size, 16)` returns the **datagen half**:
  `full` is 50 tool + 125 memory (`small`/`medium` scale down the same way).
  The v16 datagen surface is the v15 surface plus section 8.1.
- Pack policy 16: `challenge.GradingBenchVersionV16` in
  `supportedGradingBenchVersions` (grades like 15 unless `grade/` adds a 16
  branch). A v16 season accepts packs declaring 15 or 16.

### 7.2 Season and slice primitives (`challenge/season.go`)

```
SeasonCommitment(s, secret) = sha256("dittobench-dueling-season-commit/v1" || 0x00 || u64be(s) || secret)
SeasonSeed(s, secret, anchor_block_hash) = sha256("dittobench-dueling-season-seed/v1" || 0x00 || u64be(s) || secret || block32)
SliceSeed(season_seed, run_seed, column_id) = sha256("dittobench-dueling-slice/v1" || 0x00 || season_seed_hex || 0x00 || u64be(run_seed) || 0x00 || column_id)
```

All three return lowercase hex. `s` is the season index (the generator round
index the season opens on). `secret` is 32 random bytes. The anchor block is
the season's opening round block (the round open block hash), so a season
seed never exists before its commitment is published. `run_seed` is the
ticket's datagen seed (`>= 0`). Vectors live in
`challenge/testdata/season_vectors.json` and are mirrored by a Python test.

The slice dataset (`challenge.EncodeSliceDataset` / `DecodeSliceDataset`,
contract `dittobench-dueling-slice/v1`):

```json
{
  "contract": "dittobench-dueling-slice/v1",
  "bench_version": 16,
  "season_id": "<uuid>",
  "season_index": 26119,
  "run_seed": 123,
  "columns": [
    {"column_id": "c0", "submission_id": "<uuid|default>", "owner_key": "coldkey:..|", "miner_hotkey": "5..",
     "tree_sha256": "<hex>", "grading_bench_version": 16, "slice_seed": "<hex>",
     "public_default": false, "substituted": false, "pack_sha256": "<hex>", "pack": {...}}
  ]
}
```

Columns are sorted by `column_id` (`c0`..`c4`), every pack is compacted, and
the digest is the SHA-256 of the encoded bytes. Decode refuses a dataset
whose pack does not echo its `slice_seed` and `miner_hotkey`, declares a policy
other than the pinned `grading_bench_version`, fails the pack contract
(`ValidateGenerated`), or does not hash to the pinned digest. A substituted
column (the pinned generator failed on this seed) carries the public default
generator's pack on the same `slice_seed`, `public_default: true`,
`substituted: true` and an empty `owner_key`. `challenge.SliceExecutionCases`
drops a case whose fingerprint repeats an earlier column's case, so every
consumer dedupes identically.

### 7.3 Composite math (`challenge/dueling_score.go`, mirrored in Python)

`DuelingComposite(datagenRaw, datagenGate, columns, mask)` implements section
1 exactly: float64, columns in `column_id` order, no fused multiply-add
reordering. A column aggregate is `{column_id, owner_key, n, sum, sum_sq,
substituted}`; `col_c = sum / n` (`n = 0` scores 0). Owner keys equal to `""`
(public default or substituted) are never masked. Shared vectors live in
`challenge/testdata/dueling_composite_vectors.json`; the Python mirror is
`ditto_screening_protocol.dueling` and must reproduce every vector to the bit.

Stratified standard error (section 4) is computed by the scorer and reported;
the fold does not need it.

### 7.4 Generator scorer work (`services/dittobench-api/cmd/challenge-scorer`)

The scorer polls `GET /api/v1/generator-scorer/dueling-work` (Platform answers
`{"work": null}` when idle) and advertises `dueling_work_kinds=slice,battery`
on its capability poll. Two kinds:

- `battery`: `{kind, lease_id, season_id, bench_version, cases: 35, seeds:
  [32 hex], candidates: [{submission_id, miner_hotkey, owner_key,
  source_sha256, source_base64}]}`. For each candidate: build once, generate
  every seed, `ValidateGenerated`, require all 32 to declare the same policy
  inside the season window, then regenerate seed 0 and require identical pack
  bytes. Posts `POST /api/v1/generator-scorer/dueling-battery` `{lease_id,
  season_id, scorer_version, candidates: [{submission_id, ok,
  grading_bench_version, error}]}`.
- `slice`: `{kind, lease_id, preparation_id, season_id, season_index,
  bench_version, run_seed, cases_per_column: 35, columns: [{column_id,
  submission_id, miner_hotkey, owner_key, source_sha256, source_base64,
  grading_bench_version, slice_seed, public_default}], fallback:
  {submission_id, miner_hotkey, source_sha256, source_base64}}`. Builds are
  cached by `source_sha256`. A column whose generator fails on its seed is
  regenerated with `fallback` on the same `slice_seed` and marked
  `substituted`. Posts `POST /api/v1/generator-scorer/dueling-slice` `{lease_id,
  preparation_id, scorer_version, columns: [{column_id, ok, substituted,
  error}], slice_sha256, slice_base64}`.

Faults use the existing `/api/v1/generator-scorer/fault` with
`work: "dueling"`. `challenge-scorer reproduce-slice -season-secret ...
-anchor-block-hash ... -run-seed ... -source <archive>...` rebuilds a revealed
slice byte for byte.

### 7.5 Scorer delivery (`services/dittobench-api`, `dueling-slice-v1`)

- `datasetFeatures()` adds `dueling-slice-v1` and `dueling-slice-bench-<n>` for
  every bench version `>= 16` the scorer runs. The validator forwards the set
  as `scorer_benchmarks.dueling_slice_bench_versions`.
- `/v2/score` gains `dueling_slice_mode: "dueling-slice-v1"`,
  `dueling_slice_bytes` (base64, `<= challenge.MaxRoundDatasetBytes`),
  `expected_slice_sha256` and `dueling_owner_key`. A `bench_version >= 16`
  request without them is refused (`dueling slice required`); a `< 16` request
  with them is refused. The datagen half is still regenerated from the public
  seed and checked against `expected_dataset_sha256`.
- Slice waves run after the datagen waves on the same harness, ticket
  inference session and tool endpoint, through `internal/challengerun`, with an
  opaque ID nonce derived from `slice_seed`.
- Gates, efficiency, outlier and canary checks see the datagen half only.
- The report gains `details.dueling`: `{season_id, season_index,
  slice_sha256, owner_key, datagen_raw, datagen_gate, column_weight, columns:
  [{column_id, owner_key, submission_id, grading_bench_version, n, sum,
  sum_sq, substituted}], composite_stderr}`. `composite` is
  `composite(A, {owner(A)})`. Per-case rows for slice cases use
  `category = "dueling_slice"` and case IDs `<column_id>/<case_id>`; their text
  never leaves the scorer (aggregates only, open question 6).

### 7.6 Platform (`apps/platform`)

- Tables: `dueling_seasons` (`id`, `bench_version`, `season_index`, `shadow`,
  `status` committed|open|closed|revealed|void, `commitment`, `secret`
  (Platform-only, never served before reveal), `anchor_block`,
  `anchor_block_hash`, `season_seed` (served only after reveal), `columns`
  JSONB pin, `opened_at`, `closes_at_round`, `reveal_at`, `revealed_at`,
  `void_reason`, `rotation_actor`, `rotation_reason`); 
  `dueling_season_batteries`; `dueling_slice_preparations` (`season_id`,
  `run_seed`, unique together; status pending|leased|ready|failed; slice bytes
  in Platform-private storage; `slice_sha256`; attempts; error). Tickets gain
  `dueling_season_id` and `slice_sha256`; scores gain `dueling_season_id` and
  `dueling` JSONB (the report aggregates).
- Settings: generator competition settings gain `dueling_enabled` (default
  false) and `rounds_per_season` (default 1). Seasons open only while
  `dueling_enabled` and a contract `>= 16` is active or is the desired version
  of an open rollout (shadow seasons), and refuse to open while the source
  release policy has `disclosure = never`.
- Lease: a `>= 16` ticket (canonical, retest, confirmation and canary) needs a
  ready slice for `(season, run_seed)`. A missing slice reserves a preparation
  row in its own transaction and the lease answers 503 with `Retry-After`
  (the private-dataset preparation pattern). Leases at `>= 16` go only to
  validators forwarding that version in `dueling_slice_bench_versions`.
- Backroom MCP: `get_dueling_season` (current, next and by id; commitment,
  columns, battery results, per-column means across in-season scores),
  `list_dueling_slices` (preparations with status, latency, digests),
  `rotate_dueling_season` (confirmation phrase `ROTATE DUELING SEASON`,
  audited reason; closes the open season at the next round boundary or
  immediately with `immediate: true`).

### 7.7 Validator (`ditto/`)

Forwards slice bytes and digest to `/v2/score`, signs `season_id` and
`slice_sha256` with the score, and folds v16 rows only against rows of the same
`(bench_version, season_id)`. `_paired_dethrone` recomputes both agents'
per-seed composites with `M = {owner(A), owner(B)}` through
`ditto_screening_protocol.dueling`.

## 8. v15 lessons applied to v16 (2026-11-03)

### 8.1 Connectors stop self-identifying

v15 connector descriptions end in the fixed suffix "Needs the <App> connector to
be linked in Settings." (`catalog/v15.go`). A harness filtered all 200 with one
line, so the production-scale catalog added almost no difficulty: top scores
stayed at 0.94-0.97. v16 changes two things:

1. **No marker.** v16 connector descriptions use the same description grammar
   as product tools. Nothing in a tool definition says whether its connector
   is linked.
2. **Some connectors are linked, and some cases need them.** Each v16 seed
   links a small seeded set of connector apps. Their tools execute against
   the seed's fixtures like product tools; every other connector still answers
   "not connected". A seeded share of the 50 tool cases asks for an action that
   only a linked connector performs, named the way a user would ("post this to
   the #launch channel in Slack"). A harness that drops every connector fails
   those cases; one that calls connectors the request does not ask for pays
   the existing over-call price. Tool selection becomes a scored axis.

3. **Wording is not enough either.** A seeded share of tool cases name an app
   whose connector is **not** linked for the seed (the gold path tells the user
   it is not connected and offers the product-tool alternative when one
   exists), and some name an app while the right action is a product tool
   ("add the Slack standup to my calendar"). Link state is learned only at
   runtime: an unlinked connector answers "not connected", which is a correct,
   unpenalised probe when the request asked for that app. A filter keyed on
   request wording therefore loses some cases whichever way it guesses.

**Screening (operator ruling, 2026-11-03).** Harnesses **may** filter connector
or app-specific tools, by an availability marker or by guessing from the request
wording. That is an I7 W4 allowance, not an I7 capability removal, so v16 does
not ban filters and screening does not change. The difficulty comes from the
cases above: v16 publishes no marker, some connectors are linked and needed,
and wording decoys price a guess. A static denylist of the public connector
pool is permitted and simply fails the linked-connector cases on its own.

### 8.2 Bounded previous-era backfill (follow-up)

Activation admits only the frozen rescore cohort, so an agent scored before
activation outside the cohort never gets the new bench
(`list_stranded_previous_era_submissions`, #3327). The operator path today is
the per-submission stranded admission tool (backfill option B, built in
`db/queries/benchmark_admission.py`). The automatic form is a follow-up built
**on top of that admission tool once it has merged**, not part of v16 phase 1:
the queue-policy setting `previous_era_backfill` (`enabled: false`,
`max_agents`, `min_previous_composite`) admits, for the active version, the
best stranded agent per owner (one per coldkey, by previous-era composite),
capped at `max_agents`, queued behind every current-era item. It ships **off**;
enabling it is an operator decision.
