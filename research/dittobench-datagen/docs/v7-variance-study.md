# DittoBench v7 variance study (seed-to-seed spread under the ~10x hardening)

> **Revision note.** Sections 1–7 are **study v1**, measured against the
> pre-deepening v7 suite (full = 120 tools / ~131 memory cases). The suite was
> subsequently reworked (product-grounded deepening: full = 72 tools / ~187
> memory cases, ~18 hard families, subscription attribution,
> entity_lookup_chain, champion-tier calibration to 0.36–0.57). **Section 8
> (study v2) re-measures everything on the deepened suite and supersedes the
> v1 verdict**; v1 is retained because its v6 baselines and machinery are
> unchanged and the deltas are informative.

**Question.** Bench_version 7 made the suite ~10x harder (new memory modules:
deepchain, deepjoin, near-miss abstention, tempcalc, composedinj; new tool
categories: negation-cue restraint, stale-context routing, link_chain,
job_chain_recovery; full profile scaled to 120 tool + ~131 memory cases). Does
that added difficulty widen the seed-to-seed composite spread enough to
threaten the leaderboard's fairness machinery (hysteresis bands, the ~0.007
protection margin, the `1.64*sqrt(SE_c^2 + SE_ch^2)` band, confirmation
seeds)?

**Answer (verdict up front): GO.** v7 widens the top-tier between-seed SD by
~6% (0.0179 → 0.0190 composite points; paired CRN noise floor 0.0259 →
0.0279, +7.7%). The pure dataset-difficulty (structural) component of that
spread is 0.0003 composite points in BOTH versions — negligible against the
0.007 margin — so essentially all of the spread is the agent's own per-case
error realization, which the confirmation-seed machinery already averages
away. No single case type dominates the variance (max ~7% share), no v7
category is saturated or floored for a competent agent, and v7 *lowers*
per-case composite leverage relative to v6 because the suite grew. The only
operational change recommended is sizing confirmation-seed counts ~15–20%
higher than their v6-calibrated values (tables below). No generator change is
needed and **no byte-vector change was made**: the v7 dataset vector pinned by
the difficulty release is untouched.

---

## 1. Methodology

Everything is deterministic and LLM-free, using the repo's own machinery:

- **Driver:** `cmd/vstudy` (added by this study; `go run ./cmd/vstudy -seeds
  300 -out <dir>`). For each bench version (6 and 7) it generates the `full`
  profile dataset for seeds 1..300 via `gen.GenerateDataset` and scores fixed
  strategies against every case with the real grader (`grade.Memory`).
- **Analyzer:** `cmd/gstudy` (pre-existing) consumes the JSONL that vstudy
  emits (`runs_v{6,7}_{strategy}.jsonl`) and reports the G-study variance
  decomposition and per-category difficulty/discrimination flags.
- **N = 300 seeds per version** (600 generated full datasets total; ~1 minute
  wall clock). Noise floor is zero by construction — re-running a seed
  reproduces the identical scores (`TestEvalSeedDeterministic`).

**Composite.** `0.5*tool_mean + 0.5*memory_mean`, the v2+ accuracy composite.
Validator-side gates (efficiency, metamorphic, transform audit) multiply this
downstream; for the fixed honest strategies simulated here they are 1.0, so
they are variance-neutral for this comparison.

**Strategies.**

- *Naive tiers* (replicated from `gen/v7difficulty_test.go`, the difficulty
  release's measurement): `parrot`, `overlap`, `recency`, `dump`, `abstain`
  memory strategies, each paired with the fixed keyword tool router.
- *`strong` (mid tier, the decision-boundary model):* the oracle response
  (`AnswerKind`-keyed canonical answer, verified score 1.0) downgraded by a
  deterministic per-case error draw — a hash of (version, seed, case id, salt)
  compared against a per-case-type error rate (near-zero on plain recall,
  ~0.10 on routine reasoning, 0.18 on v5/v6 hard classes, 0.20–0.30 on the v7
  deep classes and dependent tool chains; full table in `cmd/vstudy/main.go`).
  This stands in for a realistic top miner near the champion boundary
  (composite ~0.92 on v6, ~0.89 on v7). Two independent salts ("A"/"B") give
  two *equal-skill* agents for the paired CRN comparison.
- *`uniform` (control):* every case fails with the same p = 0.10, removing
  the per-type rate assumptions entirely.
- *`oracle` (ceiling):* canonical answers everywhere. Scored 1.0 on **every
  memory case of all 600 datasets** (0 oracle failures) — the
  answer-key-sufficiency invariant of `TestV7OracleSolvable` holds at 10x the
  seed count it pins.

**What is not modeled.** Real model stochasticity (temperature, retries) adds
run-to-run variance on top of these numbers for both versions equally; the
validator's v7 scoring levers (result-usage gate, mismatch penalties) act
per-case and are consistent with the 0/1 per-case scoring used here.

## 2. Composite spread per strategy (300 seeds each)

Quantiles are (p5, p50, p95). SD is the between-seed SD of the composite —
i.e. the SE of a single-seed score.

| strategy | v6 mean | v6 SD | v6 p5/p50/p95 | v7 mean | v7 SD | v7 p5/p50/p95 | SD change |
|---|---|---|---|---|---|---|---|
| parrot + router | 0.3326 | 0.0123 | .3125/.3337/.3511 | 0.2726 | 0.0094 | .2560/.2727/.2887 | −24% |
| overlap + router | 0.4354 | 0.0251 | .3945/.4353/.4759 | 0.3440 | 0.0178 | .3149/.3445/.3722 | −29% |
| recency + router | 0.3625 | 0.0246 | .3229/.3623/.4023 | 0.2731 | 0.0154 | .2493/.2730/.2990 | −37% |
| dump + router | 0.3331 | 0.0163 | .3066/.3317/.3599 | 0.2835 | 0.0140 | .2609/.2835/.3077 | −14% |
| abstain + router | 0.3583 | 0.0121 | .3385/.3577/.3749 | 0.3023 | 0.0094 | .2857/.3023/.3170 | −22% |
| uniform p=0.10 | 0.8985 | 0.0222 | .8615/.8980/.9328 | 0.8999 | 0.0191 | .8674/.9004/.9296 | −14% |
| **strong (top tier)** | **0.9187** | **0.0179** | .8888/.9184/.9474 | **0.8860** | **0.0190** | .8526/.8853/.9155 | **+6%** |
| oracle | 1.0000 | 0.0000 | — | 1.0000 | 0.0000 | — | — |

Readings:

- **The top-tier SD rises only 0.0179 → 0.0190 (+6% relative, +0.0011
  absolute).** The added per-case difficulty is offset by the larger suite
  (205 → ~252 cases): the `uniform` control — same skill, no difficulty-mix
  assumptions — actually *tightens* under v7 (0.0222 → 0.0191), exactly the
  `sqrt(N)` prediction. The +6% for `strong` is the difficulty *mix* effect
  (more weight on high-error case types), not dataset instability.
- Every naive tier scores lower AND spreads *less* under v7 — the hardening
  pushed them toward a floor, which compresses their variance. No fairness
  concern arrives from below.
- Mean drop for the fixed top tier (0.9187 → 0.8860) is the intended
  difficulty, not variance.

## 3. G-study decomposition (cmd/gstudy)

Per-case scores, seed facet vs item (category) facet vs residual:

| runs file | grand mean | seed frac | item frac | residual frac | dominant |
|---|---|---|---|---|---|
| v6 strong | 0.919 | 0.43% | 3.2% | 96.4% | residual |
| v7 strong | 0.886 | 0.35% | 5.7% | 93.9% | residual |
| v6 overlap (naive) | 0.443 | 0.24% | 61.1% | 38.7% | item |
| v7 overlap (naive) | 0.341 | 0.15% | 66.4% | 33.5% | item |

- For a competent agent both versions are **residual-dominated with a tiny
  seed component** — the classic "per-case Bernoulli noise" regime where
  averaging over cases/seeds works and CRN pairing helps. The seed facet
  *shrinks* under v7 (0.43% → 0.35%).
- For the naive tier the item facet dominates (and grows under v7): the
  benchmark discriminates by *category*, which is precisely the design intent
  of the difficulty release (parser-like harnesses collapse on the reasoning
  categories — see the `parser-like` trap-gap telemetry, 0.31 → 0.28 gap with
  plain-recall accuracy falling 0.48 → 0.43).

**Structural vs Bernoulli split** (strong tier; structural = SD of the
*expected* composite per seed, i.e. pure case-mix difficulty drift):

| | v6 | v7 |
|---|---|---|
| structural SD | 0.0003 | 0.0003 |
| Bernoulli SD (paired diff / √2) | 0.0183 | 0.0197 |
| total SD | 0.0179 | 0.0190 |

The stratified per-category quotas hold the dataset's expected difficulty
essentially constant across seeds in both versions: **structural spread is
~4% of the protection margin.** The memory mix does drift slightly per seed
(e.g. point-in-time 2–6 cases, multi-session 3–6, isolation 5–8 across seeds)
but its composite effect is the 0.0003 above — not worth a generator change.

## 4. Per-category flags and variance offenders

`cmd/gstudy` flags on the strong-tier runs: **no v7 category floors, and
nothing saturates beyond the plain-recall categories that are near-1.0 by
construction** (declarative-write 0.98 etc. — coverage/floor-catching cases,
intentionally easy). On the naive runs the new v7 classes floor
(lifecycle-deep-read, multi-hop-deep, near-miss-abstention,
temporal-arithmetic, link_chain, job_chain_recovery, stale-context…) — that
is the difficulty design working, not a reliability defect: the same
categories discriminate strongly for the mid tier.

Top shares of the strong tier's composite variance (Var of each category's
weighted contribution / total across categories):

| v6 | share | v7 | share |
|---|---|---|---|
| mem temporal-reasoning | 7.0% | mem temporal-reasoning | 6.9% |
| mem preference-application | 6.9% | mem multi-session | 5.2% |
| mem multi-session | 5.4% | mem preference-application | 5.1% |
| mem multi-hop-relational | 3.9% | mem isolation | 4.4% |
| mem temporal-depth | 3.5% | mem point-in-time | 4.1% |
| — | | tool job_chain_recovery_result_usage | 3.6% |
| — | | tool link_chain_result_usage | 3.3% |

- **No single case type dominates** (max ~7%, same as v6, and the top
  offenders are *pre-existing* v5/v6 types, not the new v7 classes; the
  largest new-class share is 3.6%).
- The five all-or-nothing result-usage tool chains jointly carry ~17% of
  category-level variance on ~21% of the tool suite — proportionate, not a
  design smell.
- Per-case composite leverage (one case flipping): v6 = 0.0052 per memory
  case / 0.0045 per tool case; **v7 = 0.0038 / 0.0042 — lower than v6**
  because the suite grew. No case is worth "multiple margins": the worst
  correlated block (a deep-chain write miss cascading into its read) swings
  ~2 cases ≈ 0.008, about one margin, and that is a *skill* signal, not
  noise.

## 5. Decision-boundary impact

Live-fold machinery assumed: protection margin **m = 0.007** composite
points; challenger band `1.64*sqrt(SE_ch^2 + SE_c^2)`; confirmation seeds run
champion and challenger on the same fresh seeds (CRN pairing).

**Equal-skill false-separation probability (single seed).** Two equal-skill
top-tier agents:

| comparison | v6 | v7 |
|---|---|---|
| same seed (CRN), P(\|Δ\| > m), empirical | 0.750 | 0.793 |
| same seed (CRN), normal approx (σ_diff = 0.0259 / 0.0279) | 0.787 | 0.802 |
| different single seeds (σ√2 = 0.0253 / 0.0269 per side) | 0.782 | 0.794 |

Single-seed comparisons at the 0.007 margin were already ~75–80% likely to
spuriously exceed the margin under **v6**; v7 moves that by ~1–4 points. The
margin only functions because of the SE band + confirmation seeds, and that
was equally true before the hardening.

**Confirmation seeds to resolve a true gap Δ** (paired CRN, one-sided
α = 0.05; "detect" = band exactly equals the gap, 50% power; "95% power" =
`n = ((1.645+1.645)·σ_diff/Δ)²`):

| true gap Δ | v6 detect | v6 95% power | v7 detect | v7 95% power |
|---|---|---|---|---|
| 0.005 | 73 | 292 | 85 | 337 |
| 0.010 | 19 | 73 | 22 | 85 |
| 0.020 | 5 | 19 | 6 | 22 |
| = margin 0.007 | 38 | 149 | 43 | 172 |

**Recommendation:** scale whatever confirmation-seed count was calibrated for
v6 by `(0.0279/0.0259)² ≈ 1.16` — i.e. **+15–20%** (e.g. 19 → 22 seeds to
detect a 0.01 gap; 73 → 85 for 95% power). SE-based bands that are computed
from the observed per-seed spread (rather than a hard-coded σ) need no change
at all — they will simply measure the slightly larger σ. If any component
still assumes the historical σ ≤ 0.01 design target (`cmd/benchcal`'s
`targetSigma`, dittobench-api), note that a *single-seed* top-tier σ of
~0.019 was already the v6 reality in this model; the 0.01 target only ever
described the structural half, which remains ~0.0003.

## 6. Fix assessment (none required, none made)

The trigger for a variance-reducing change — "a single case type dominates,
or one case flips the composite by multiple points" — is not met:

- max category share ~7% (v6-parity), largest *new* v7 class 3.6%;
- structural (dataset difficulty) SD 0.0003 in both versions, so pinning the
  drifting memory-mix counts (the only candidate generator change, e.g.
  fixed quotas for point-in-time/multi-session/isolation) would buy at most
  0.0003 composite points of SD while re-pinning the v7 byte vector;
- per-case leverage went *down* v6 → v7;
- partial-credit restructuring of the deep chains would reduce the intended
  difficulty (the all-or-nothing final-value contract is the anti-grep
  design) for at most a 2-case-correlation gain.

Accordingly this study ships **measurement tooling only** (`cmd/vstudy` +
tests). **No dataset-generation bytes were touched; the v7 known vector is
unchanged.**

## 7. Reproduction

```
go test ./cmd/vstudy ./cmd/gstudy          # driver invariants
go run ./cmd/vstudy -seeds 300 -out /tmp/vstudy-runs > /tmp/vstudy.json
go run ./cmd/gstudy -in /tmp/vstudy-runs/runs_v7_strong.jsonl
```

Caveats: the strong tier's per-type error rates are documented modeling
assumptions (see `memErrRates`/`toolErrRates`); the `uniform` control shows
the headline conclusion (v7 does not meaningfully widen top-tier spread) is
insensitive to them. Real-model temperature noise is additive to both
versions and is exactly what CRN confirmation seeds cancel.

**Verdict (v1, superseded by §8): GO** — ship v7 at current variance; bump
confirmation-seed counts ~15–20% and leave the 0.007 margin and SE-band
machinery as they are.

---

## 8. Study v2 — the product-grounded deepening (supersedes §1–7 verdict)

The v7-difficulty branch gained the deepening after study v1: full profile is
now **72 tool / ~187 memory cases** (was 120/~131), the reasoning subset is
57.6% of the memory suite, result-usage chains dominate the tool mix (30/72
cases plus 3 entity_lookup_chain), and two new memory families landed
(subscription-own / subscription-attributed, 8+8 cases). The champion tier
was recalibrated: the operator's top-5 rebench (0.607–0.845 on the
pre-deepening suite) is modeled by a STRONG anchor (newDitto-like) and a WEAK
anchor (whitycatboss/infinity-like) that land **0.573 / 0.364** (flat mean)
on the deepened suite (`TestV7ChampionTierLandsNearTarget`).

### 8.1 Method changes

`cmd/vstudy` gained two champion tiers, `champS` and `champW`, whose
per-class expected pass rates are ported verbatim from the gen package's
calibration tables (test symbols are not importable;
`TestChampionTablesMatchAnchors` pins the copies against the anchor bands so
drift is caught). Every model tier (strong, champS, champW, uniform) now runs
an independent equal-skill twin for the paired CRN comparison plus a
no-draws expected composite for the structural split. Same N = **300 seeds
per version**; study-v1 draws for the legacy tiers reproduce byte-identically
(same salts). Composite remains 0.5·tool + 0.5·mem; the anchor-comparable
flat means are also reported (`expected_flat_mean`: champW v7 = 0.364,
champS v7 = 0.573, champS v6 = 0.833 — the port reproduces the calibration).
Oracle: 1.0 on every memory case of all 600 datasets, **0 failures** — the
deepened families remain answer-key sufficient at 300 seeds.

### 8.2 Composite spread (300 seeds; SD = single-seed SE)

| tier | v6 mean | v6 SD | deepened-v7 mean | deepened-v7 SD | SD change | v1-v7 SD (old suite) |
|---|---|---|---|---|---|---|
| overlap + router (best naive) | 0.4354 | 0.0251 | 0.3137 | 0.0130 | −48% | 0.0178 |
| uniform p=0.10 (control) | 0.8985 | 0.0222 | 0.8995 | 0.0198 | −11% | 0.0191 |
| strong (v1 legacy mid tier) | 0.9187 | 0.0179 | 0.8514 | 0.0234 | +31% | 0.0190 |
| **champS (strong champion)** | 0.8283 | 0.0246 | 0.5976 | 0.0307 | **+25%** | — |
| **champW (weak champion = today's boundary)** | 0.6720 | 0.0270 | 0.3777 | 0.0270 | **±0%** | — |
| oracle | 1.0000 | 0 | 1.0000 | 0 | — | — |

Paired equal-skill CRN noise floor (same-seed diff SD of two independent
equal-skill twins) and margin-crossing probability (m = 0.007):

| tier | v6 diff SD | v7 diff SD | Δ | P(\|Δ\|>m), 1 seed, v6 → v7 |
|---|---|---|---|---|
| strong (legacy) | 0.0259 | 0.0348 | +34% | 0.79 → 0.84 |
| champS | 0.0335 | 0.0443 | +32% | 0.84 → 0.87 |
| **champW** | **0.0383** | **0.0400** | **+4%** | 0.86 → 0.86 |
| uniform | 0.0326 | 0.0293 | −10% | 0.83 → 0.81 |

Readings:

- **At the actual decision boundary (champW ≈ today's best harnesses) the
  deepening does not widen variance at all**: single-seed SD 0.0270 → 0.0270,
  paired noise floor +4%. The weak champion sat at mid pass rates (max
  Bernoulli variance) *already on v6*; the deepening moved its mean
  (0.67 → 0.38), not its spread.
- Tiers that were previously in the low-variance high-score regime (strong,
  champS) widen 25–34% because the deepening pushes their pass rates toward
  0.5 — that is where the binomial is noisiest, and it is exactly the regime
  the deepening intends champions to occupy while they climb.
- The uniform control *tightens* again (more total cases, 205 → ~259): the
  widening is difficulty-mix concentration, never dataset instability.
- **Correction to study v1's headline**: v1 sized the noise floor off a
  0.92-composite mid tier (diff SD 0.026). The relevant boundary tier is
  champW at diff SD **0.040** — v1's confirmation-seed numbers were ~2x too
  optimistic *for either suite*. The right comparison (champW v6 vs champW
  deepened v7) is +4%.

### 8.3 Structural (dataset-difficulty) component

SD of the *expected* composite across seeds (no Bernoulli draws):

| tier | v6 | deepened v7 |
|---|---|---|
| strong | 0.0003 | 0.0003 |
| champS | 0.0009 | 0.0006 |
| champW | 0.0016 | 0.0011 |
| uniform | 0.0000 | 0.0000 |

Still negligible — **≤ 0.0016 composite points everywhere, ≤ 23% of the
protection margin, and the deepening *reduced* it at the champion tiers**
(the rebalanced quotas hold the hard-family mix steadier per seed than the
pre-deepening mix did). The G-study seed facet agrees: champW seed_frac
0.33% (v6) → 0.21% (deepened v7); item facet 26.7% → 36.9% (category
discrimination grew — design intent).

### 8.4 New-family flags and variance offenders (champW tier)

**Floors for today's weak champion** (gstudy, champW v7): multi-hop-deep
(pass 0.002), lifecycle-deep-read (0.005), near-miss-abstention (0.006),
temporal-arithmetic (0.007) — ~46 memory cases carrying ~0 Fisher
information *at the current boundary*. They are not a variance problem
(floored ⇒ near-zero variance contribution) and they discriminate for champS
(0.06–0.11) — they are the intended headroom. But they mean ~24% of the
memory suite is currently dead weight for separating today's top harnesses
from each other; separation happens on the mid families.

**The new families behave well**: subscription-own / subscription-attributed
grade at 0.09/0.15 (champW) and 0.35/0.46 (champS) with the highest
discriminations in the suite (0.29–0.50) and **do not appear in the top-15
variance shares**. entity_lookup_chain is the largest *new* contributor at
3.3%.

**Top variance shares are now ALL tool categories** (champW, v7):
web_result_usage 7.5%, web_recovery_result_usage 5.6%, job_chain_result_usage
4.6%, multi_web_result_usage 4.2%, entity_lookup_chain 3.3%, … — no memory
family makes the top 15. No single category dominates (max 7.5%, comparable
to v1's 7%), but the block exists because of a structural shift:

> **LOUD FLAG — single-tool-case leverage now equals the protection margin.**
> The deepening cut the tool suite from 120 to 72 cases while the composite
> still weights the tool side 0.5. One flipped tool case now moves the
> composite by 0.5/72 = **0.0069 ≈ 0.99 × the 0.007 protection margin**
> (v6: 0.0045; pre-deepening v7: 0.0042; memory case: 0.0027). A single
> lucky/unlucky link-chain serve can cross the margin on its own, and the
> 5-case link_chain family can swing 0.035. The tool side carries ~79% of
> the champS composite variance (tool-mean SD 0.055 vs memory-mean SD 0.028)
> despite being 28% of the cases.

**Fix options assessed (none implemented — see §8.6):** partial credit on
the chains is validator-side and would soften the anti-grep all-or-nothing
contract (reduces difficulty — out of bounds). Count rebalancing (raising
Tools 72 → 110 at the same category proportions) keeps every case exactly as
hard, cuts per-case leverage to 0.0045 and the champW composite SD by ~11%
(0.0270 → 0.0240, tool-mean SE × √(72/110)); at 144 tools, −19%. That is a
generation change that **re-pins the v7 byte vector** and overrides the
deepening's deliberate product-grounded 72-case mix, so it is left as a
recommendation for the suite owners, not made unilaterally here.

### 8.5 Confirmation seeds at the new boundary (champW, paired CRN)

| true gap Δ | v6 detect / 95% power | deepened-v7 detect / 95% power |
|---|---|---|
| 0.005 | 160 / 637 | 173 / 692 |
| 0.010 | 40 / 160 | 44 / 173 |
| 0.020 | 10 / 40 | 11 / 44 |
| = margin 0.007 | 82 / 325 | 89 / 354 |

**Recommendation:** size confirmation seeds from σ_diff = **0.040** (champW).
That is **+8–10% vs the same tier on v6** — the deepening itself is cheap —
but **~2.3x study v1's numbers**, which were computed at a 0.92-composite
tier that no longer exists on this suite. Concretely: ~44 confirmation seeds
to detect a 0.01 composite gap (band = gap), ~89 to detect a margin-sized
gap. Single-seed comparisons remain meaningless at the margin (86%
false-crossing rate for equal-skill agents — true on v6 too).

### 8.6 Verdict (v2, supersedes §7)

**GO** — the deepened suite is variance-safe to ship:

- boundary-tier (champW) single-seed SD unchanged vs v6 (0.0270), paired
  noise floor +4%;
- structural dataset-difficulty SD ≤ 0.0011, *smaller* than pre-deepening;
- seed facet of the G-study shrinks; no new family floors for the strong
  anchor; oracle solvability 100% over 300 seeds;
- no single family dominates variance.

Conditions attached to the GO:

1. **Re-size confirmation seeds from σ_diff ≈ 0.040** (≈ +10% vs v6-tier
   sizing, ≈ 2.3x the numbers study v1 published) — operational only.
2. **The single-tool-case leverage flag (§8.4) goes to the suite owners**:
   either accept that one tool case ≈ one protection margin (and lean on
   confirmation seeds, which handle it), or rebalance tool counts upward in
   a future v7 revision (−11% composite SD at 110 tools, quantified above).
   Not implemented here: it re-pins the v7 vector and reverses a deliberate
   product-grounded mix decision.
3. Note for leaderboard analytics: ~24% of the memory suite (the four
   deep-hard families) is floored for today's champions — expected headroom,
   but per-family telemetry should track when it starts discriminating.

**No generation bytes were touched in this study revision either; the
deepened v7 vector is unchanged.** Reproduction: same commands as §7 (the
driver now also emits `runs_v{6,7}_champ{S,W}.jsonl`).
