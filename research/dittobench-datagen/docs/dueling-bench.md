# Dueling bench (bench_version 15): generator cases in the canonical agent run

Status: **design only**. Nothing here is implemented, deployed or activated.
This is Rollout step 3 of [generator-competition.md](generator-competition.md):
"a new bench version draws its memory cases from the generators".

Operator decision: SN118 becomes a dueling subnet. Each agent's canonical run is
DittoBench datagen cases **plus** cases from the current top generators, scored
as one composite through the existing scoring and consensus path. Generators
also earn their own track (10% share) from rounds. Round columns move to about
70 cases (5 counted columns, about 350 cases).

The version number is **15**. v14 is the live contract (`protocol.BenchVersionV14`,
activated 2026-10-28; [benchmark-v14-rollout.md](../../../docs/benchmark-v14-rollout.md)).

## Accepted amendments (2026-10-27)

The operator accepted sections 1 to 4 of the v15 list in
[tandem-upgrades.md](tandem-upgrades.md) ("v15: generator cases inside agent
runs across a version boundary") as amendments to this design:

1. Slices are graded at each column's declared pack policy
   (`grading_bench_version`), never at a fixed 13 or the run's bench version
   (section 4 below).
2. The season freezes each column's declared policy; the 32-seed battery must
   agree, and a slice that declares another policy fails like a digest
   mismatch.
3. A season belongs to one bench version and never spans an activation.
4. While a v15 rollout is open, Platform runs non-authoritative shadow seasons
   at 15 for the cohort's desired-version runs.

With the two-version window (also decided 2026-10-27), a v15 season accepts
columns declaring 14 or 15 only.

## Summary of the proposal

| Question | Proposal |
|---|---|
| Run size | 350 cases total, unchanged: 175 datagen (50 tool + 125 memory) + 175 generator (5 columns x 35). |
| Weight | `composite = gate_d x (0.5 x datagen + 0.5 x generator)`. Pinned in the v15 contract. |
| Which generators | The top 5 **eligible** generators by ledger score, one per owner, frozen per **season** (4 one-epoch rounds, about 4.8 h). A newly qualified generator can enter canonical runs about 2.4-7 h after qualifying. The public default generator fills empty slots. |
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
non-goal). The v15 contract therefore splits the run into two halves:

| Half | Cases | Inside the half |
|---|---:|---|
| Datagen | 175 | 50 tool + about 125 memory, from the inherited contract (v14 if it activates first, else v13) at a new "dueling" profile. Graded and gated exactly as that contract. |
| Generator slice | 175 | 5 columns x 35 cases, each graded at its pack's declared policy (`grading_bench_version`, pinned per season; see [tandem-upgrades.md](tandem-upgrades.md)). |

```
datagen   = 0.5 x tool_mean + 0.5 x memory_mean      (datagen cases only)
col_c     = mean case score on column c              (a failed /run is a 0, as in datagen)
G_self(A) = mean over counted columns c with owner(c) != owner(A) of col_c
composite = gate_datagen x (0.5 x datagen + 0.5 x G_self(A))
```

Columns get equal weight whatever their case count, so a column that loses
duplicates is not diluted. The final shares are 25% tool, 25% datagen memory and
50% generator memory.

**Why 350 and not 700.** Rounds keep about 70 cases per column for
measurement precision. The canonical slice uses 35 per column so the run stays
inside today's ticket TTL and the 6600 s wall clock
(`.agents/skills/ditto-subnet-benchmark/references/bench-runtime.md`). A
700-case run would double validator slot-hours for every canonical,
retest and confirmation run.

**Governance.** The 0.5 weight, the 175/175 split and 5 x 35 are **contract
constants** (`protocol` and `gen.ProfileForVersion` at `>= BenchVersionV15`).
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

Rounds are one epoch (360 blocks, about 72 min) and seasons are 4 rounds, so a
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

Best case is one round's wait plus one round's run (about 2.4 h). Worst case
adds most of a season (about 7.2 h). A new upload from an already pinned owner
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
v15 delays that promise but keeps it: every slice becomes reproducible once its
generator's source is published. Publishing the source is a required part of
the contract, not an option. For generators that hold a v15 column, it replaces
the "source is never public" rule in generator-competition.md "Privacy".

- **What is published.** The exact archive pinned by `tree_sha256` for every
  generator that held a column in any v15 season, plus the challenge-scorer
  revision that built it. The public default generator is already public.
- **When.** `release_at = max(reveal of the last season that pinned it, close of
  the first season that pinned it + embargo_hours)`. `embargo_hours` comes from
  the existing source release policy (`apps/platform/ditto/api_models/artifact_release_settings.py`,
  read with `get_source_release_policy`). The reveal comes from
  `privacy_window_rounds`. So a generator that holds a column for a long time
  still becomes auditable once the embargo runs out. That is deliberate: it
  pushes generator miners to keep submitting new work, which is the point of
  the duel.
- **Activation guard.** v15 activation, and every season open, refuse to run
  while the source release policy has `disclosure = never`. A generator
  uploaded under v15 rules accepts this disclosure at upload time, through the
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
v15 adds a third work kind beside `generate` and `score` in `round.go`:

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

### Transport

The existing private transport (`services/dittobench-api/cmd/dittobench-api/private_dataset.go`,
PROTOCOL.md "Private V13 control-plane transport" and "Generator-round
datasets") already carries Platform-pinned bytes with a canonical digest. v15
adds one mode:

- `datasetFeatures()` advertises `dueling-slice-v1` when screened-image mode is
  on. `validatePrivateDatasetRequest` admits it only for
  `bench_version >= 15`, requiring slice bytes plus `expected_slice_sha256`.
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
to that owner's agent, which is six times `KOTH_MARGIN = 0.007`. So v15 uses
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

An unsolvable column must not drag down everyone. v15 guards against that in
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

- A season is pinned for `rounds_per_season` rounds (default 4; rounds are
  `epochs_per_round = 1`, so about 4.8 h), aligned with `SubnetEpochIndex` round
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
- A ban or copy ruling against a pinned generator mid-season triggers an early
  rotation, not an in-place swap.

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
adding new work. If v15 runs take longer (the generator half is all memory
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
a `rounds_per_season` below 2.

## 4. Scoring

- **Grading.** Slice cases are graded by `scorer.GradeMemory` at the pack's
  declared `grading_bench_version` (13 or 14 today), which the season pins per
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
  `ditto/validator/config.py`) get v15 values from shadow calibration, gated
  on `bench_version >= 15` and never retyped per version.
- **Continual retests.** Unchanged mechanically. Every retest of an agent in a
  season uses the season's set. Rotation is what drives retest demand.
- **Confirmation seeds.** Each CRN seed's slice derives from that seed and the
  season seed, so champion and challenger face identical generator cases and
  the pairing holds. `TOP5_MIN_CONFIRMATION_SEEDS` (8) is rechecked in
  calibration, because 35-case columns are noisier per seed.
- **LongMem confirmation.** An instrument, not a per-epoch contract. The
  existing profile confirms v15 unchanged (bench-version-bump skill, section 1b).
- **Efficiency, outlier escalation, deferred source review, ATH anomaly.**
  Efficiency and gates use the datagen half only in v15.0. Outlier and anomaly
  thresholds are recalibrated on the v15 shadow distribution before
  activation.

## 5. Bench-version bump checklist (v15)

Everything is additive and gated `>= BenchVersionV15`. The v2-v14 vectors must
not move.

| Layer | Change |
|---|---|
| Datagen contract | `protocol/epoch.go`: `BenchVersionV15`, `datasetEpochV15 = 2027-07-01`, `supportedBenchVersions`. `gen/v15_envelope.go`: dueling profile (50 tool + ~125 memory slot table, `mixaudit` gate). `gen.ProfileForVersion`. `challenge/`: `SeasonCommitment`, `SeasonSeed`, `SliceSeed`, `EncodeSliceDataset`/`DecodeSliceDataset`, `testdata/slice_vectors.json`. `grade/`: none. `docs/bench-versions.md`: the one v15 row and section. Known vectors: new v15 datagen-half vector. |
| Scorer | `internal/scoregates`: `SupportedBenchVersion` upper bound. `cmd/dittobench-api`: `supportedBenchVersions`, `private_dataset.go` (`dueling-slice-v1`), datagen+slice runner, report `generator_columns` aggregates and `season_id`. `internal/scorer`: v15 composite and stratified SE. `internal/efficiency`: `ProductionReadyForVersion` (datagen half). `internal/longmemeval/profile.go`: floor check. `relay_preflight.go`, `confirmation*.go`, `v9base` (datagen half). `PROTOCOL.md`. |
| Challenge scorer | `cmd/challenge-scorer/round.go`: `slice` work kind, per-season build cache, season qualification battery, `reproduce-slice` (published source + revealed season secret + run seed). |
| Shared protocol | `packages/ditto-screening-protocol/bench_v9.py`: add 15 to the `V9EvidenceBenchVersion` Literal (derived sets follow). New `dueling.py`: season manifest and column aggregate models used by both validator and Platform. |
| Platform | `db/models.py` + one Alembic migration: `dueling_seasons` (commitment, secret, anchor, pinned columns, reveal), `dueling_slice_preparations`, `slice_sha256` on tickets, `season_id` and column aggregates on scores, every `bench_version` CHECK reviewed as a floor. `db/queries/`: `tickets.py` (slice pin on lease, released like the round pin), `score_ranking.py` (season-scoped), `queue_order.py` and `benchmark_rollout.py` (rotation cohort), `confirmation_seed_anchors.py` (season key). `api_server/`: `validator.py` lease payload, `generator_rounds.py` (season open after a scored round), `crn.py` and `koth.py` v15 constants. Backroom tools: `get_dueling_season`, `list_dueling_slices`, `rotate_dueling_season` (confirmation phrase), `rounds_per_season` setting (floor 2). Generator source release: `generator_source_releases` keyed to `privacy_window_rounds` and `embargo_hours`, published like agent source releases; season open refuses `disclosure = never`. |
| Validator | `SUPPORTED_BENCH_VERSIONS` (from shared). `dittobench.py`: forward slice bytes and SHA. `weights.py`: same-season comparisons, pair-masked `_paired_dethrone`, aggregate-aware `_entry_seed_composites`. Signed envelope carries `season_id` and aggregates. Regenerate `validator_contract.json` from Platform. |
| Screener | Screening stays datagen-only. Review version-named policy fingerprints. |
| Generated + mirrors | Regenerate `apps/backroom/src/generated/platform-api.ts` and `services/model-relay/db/schema.sql`. Hand-fix `apps/backroom/src/lib/admin.schemas.ts` and dashboard `src/types/` (per-column panel). |
| Miner kit | `local-rehearsal.py`: `MAX_BENCH_VERSION = 15`, plus a local slice from the public default generator (`challenge-scorer run`) so miners can rehearse the composite. `src/protocol.rs`: `MAX_SUPPORTED_BENCH_VERSION`. `.agents/skills/mine/SKILL.md`. |
| Release + CI | `release.yml`: the supported-set identity gate. `release/components.toml` owners for new paths. Tests that use 15 as the invalid version move to 16. |

## Phased rollout

| Phase | What happens | What is needed first | Exit criterion |
|---|---|---|---|
| 0. Prerequisites | Rounds at 70 cases per column. At least 3 eligible miner generators across 2+ scored rounds. v14 decided (activate or skip). | Generator competition healthy in shadow. `owner_key` audited. | Eligibility filter produces a stable top 5 across 3 consecutive rounds. |
| 1. Contract, OFF | Datagen, scorer, challenge-scorer `slice`, Platform seasons (no issuance), validator support. All CI green. | This design approved. | Release advertises 15. v2-v14 vectors unchanged. |
| 2. Shadow canary | `issue_benchmark_canary` at v15. First default-only slices, then miner columns. Non-authoritative. | Fleet advertises `dueling-slice-v1`. Slice preparation deployed. | Slice preparation p95 < 60 s. Runs fit the TTL. No false zero on cleared top agents. Pair-mask fold vectors match on Go and Python. |
| 3. Shadow rollout | `start_benchmark_rollout` desired 15. Top cohort scored in parallel with the active version, through at least 10 season rotations (about 2 days). | Phase 2 clean. KOTH v15 constants drafted. | Crown stability and rank correlation against the active version reviewed. Per-column means stay above the floor. Rotation retest load fits capacity. |
| 4. Activate | v15 activation, and the generator track share (10%) and upload fee through governance, together. | An independently reviewed activation decision. | Not retroactive. Rollback is the inherited version, still served. |

## 6. Open questions (recommendations in bold)

1. **Weight and size. Decided:** 50/50 at 350 cases (175 datagen + 175
   generator, 5 x 35). Rounds keep about 70 per column.
2. **Datagen base.** **Inherit v14 if it activates before phase 2, otherwise
   v13.** The datagen half is that contract at the dueling profile.
3. **Season length. Decided:** 4 one-epoch rounds (about 4.8 h), with the
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
8. **Canonical statistics paying generators.** **No in v15.0.** Rounds stay the
   only, pairwise-neutral, generator scorer. Canonical column means are
   monitoring.
9. **Eligibility thresholds.** **Unsolved fraction <= 20%, column score > 0,
   and a clean 32-seed battery.** Tighten if shadow shows per-seed variance.
10. **Gates on slice cases.** **Ungated in v15.0, with v13 provenance and causal
    gates in observe mode on slice cases for data.** Promote only through a
    later version with calibration.
11. **Rotation rescore budget.** **Champion first, then `retest_cohort_size`
    (keep it at 5-10 with 4.8 h seasons). Previous-season scores are provisional
    for one season.** Confirm this against measured slot capacity in phase 3.
