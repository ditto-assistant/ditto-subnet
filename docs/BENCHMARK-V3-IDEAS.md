# DittoBench v3 — Ideas & Design Note

*Successor to [`BENCHMARK-V2.md`](BENCHMARK-V2.md) (the v2 design) and
`dittobench-api/docs/BENCHMARK-V2-REVIEW.md` (the Phase-B review). Where v2 made
the **dataset** contamination-resistant and broad, v3 makes the **measurement**
reliable and the **score** hard to game. Research-grounded; prioritized; with a
concrete build plan for the first two items.*

**Confidence key:** `[P]` primary-verified · `[S]` secondary/single-source ·
`[U]` post-cutoff or unverified — a lead, not a citation.

---

## 0. The reframe

Two shifts drive everything below:

1. **Score relations between answers, not isolated point-answers.** An adversarial
   miner (rewards are on-chain) can spike any single question by luck or lexical
   shortcut, but cannot cheaply satisfy a web of relational constraints the
   benchmark author controls and they cannot see: invariance, monotonicity,
   consistency, calibration.
2. **Treat the score as a paired, information-weighted *estimate*, not a number.**
   Benchmark items are not equally informative; two near-equal agents must be
   compared with a **paired test on shared items**, never by eyeballing whether two
   separate error bars overlap. The reliability of the on-chain *weights* comes
   from this, not from the mean.

The load-bearing consumer is the **KOTH weight fold** (`ditto/validator/weights.py`):
a champion holds ~all emissions; a challenger dethrones only by beating it by a
margin. Today that margin is a flat 5% relative. v3's central move is to make it a
**statistically-principled indifference band** fed by **common-random-number
(CRN) paired scoring**.

---

## 1. Priority stack

| # | Strategy | Axis | Effort | Status |
|---|---|---|---|---|
| **1** | **CRN common-seed champion-vs-challenger scoring** | reliability | med | **building** |
| **2** | **Paired / indifference-band + sequential dethroning** | reliability | low–med | **building** |
| 3 | Metamorphic sub-scores (invariance / monotonicity / contradiction) | anti-gaming | low | planned |
| 4 | G-study variance decomposition (item/seed/prompt/judge) | reliability | med | planned |
| 5 | Fisher-information item selection at the champion boundary | reliability | med–high | planned |
| 6 | Calibration / proper-scoring + selective abstention | value | med | planned |
| 7 | DRM false-memory / interference probes | coverage | med | planned |
| 8 | Per-run nonce canaries + integrity gate | anti-gaming | low | planned |
| 9 | Computed-answer modalities (temporal-delta, filtered aggregation, implicit preference, premise-correction) | coverage | med | planned |

Items 1–2 harden the weight mechanism that actually ships; 3 is the cheapest
anti-gaming win; the rest deepen coverage and reliability.

---

## 2. Reliability layer (the KOTH instrument)

### 2.1 CRN common-seed scoring (#1)

**Problem.** Every submission today draws a fresh crypto-random seed, so the
champion's and challenger's composites come from *different* datasets of
different luck-of-the-draw difficulty. The variance of their **difference** —
the only quantity KOTH cares about — is therefore `Var(A) + Var(B)`, inflated by
dataset-difficulty noise that has nothing to do with ability.

**Fix (Common Random Numbers).** Score the champion and challenger on the
**identical** freshly-generated dataset for a comparison. Positive correlation
between their scores collapses the variance of the difference:
`Var(A−B) = Var(A) + Var(B) − 2·Cov(A,B)` — the covariance term is free variance
reduction (~⅓ at ρ=0.5). *Law & Kelton; Miller "Adding Error Bars to Evals",
arXiv:2411.00640* `[P]`.

**The consensus problem and its fix.** The weight fold is a *deterministic*
function every validator runs on the shared ledger; if each validator picked its
own CRN seed the resubmitted scores would diverge and consensus would break. So
the CRN seed must be a **deterministic function of the comparison**:

```
crn_seed = H(champion_agent_id ‖ challenger_agent_id ‖ bench_version)  (mod 2^63)
```

Every validator scoring the same pair at the same version uses the same seed →
same dataset → same expected score. It is still **anti-cheat**: the seed depends
on agent ids not known before submission and rotates per pairing, so no miner can
precompute. This is the synthesis of "randomize across matches, fix within a
match."

**Where it lands.** `dittobench-api` must honor an optional `seed` in
`/v1/submit` (instead of always `FreshSeed`); the validator's `dittobench.py`
forwards it; the worker's re-score sweep scores the champion + tail on the
**common** `crn_seed` so their refreshed composites are CRN-comparable. Building
now: the seed-honoring path, the deterministic `crn_seed` helper, and the
worker plumbing.

**Bonus.** Once champion and challenger are scored on the same items, we can keep
their **per-case** vectors and run a true **paired** test (§2.2) instead of the
conservative unpaired one.

### 2.2 Paired / indifference-band + sequential dethroning (#2)

**The pitfall we must avoid.** Deciding a winner by whether two *marginal*
confidence intervals overlap is statistically invalid — non-overlap implies
significance, but overlapping intervals can still be significant. *Schenker &
Gentleman, The American Statistician 2001* `[P]`. The correct instrument is a
test on the **difference** `d̄`.

**The fold change (consensus-safe, additive).** Replace the flat relative-margin
dethroning predicate

```
challenger.composite > champion.composite * (1 + margin)
```

with an **indifference band = max(relative margin, measurement uncertainty)**:

```
Δ = challenger.composite − champion.composite
band = max( margin * champion.composite,  z * sqrt(se_c² + se_champ²) )
dethrone  iff  Δ > band
```

where `se_*` is an **optional** per-entry `composite_stderr` the platform may
surface (read via `getattr`, exactly like `bench_version` in v2 — no wire-model
change, additive-optional). When the ledger carries no SEs the band collapses to
the current flat relative margin → **byte-identical behavior, zero regression**.
When SEs are present the champion is only dethroned when the challenger's lead
exceeds the combined measurement uncertainty — the KOTH margin becomes a
statistical significance test. The whole thing stays a **pure, deterministic**
function of ledger fields + the `margin`/`z` constants, so Yuma consensus holds.
*Miller arXiv:2411.00640 (SE, paired difference, power); CAT SE(θ̂)=1/√ΣI,
arXiv:2306.10512* `[P]`.

**Unpaired now, paired after CRN.** Without common items we can only use the
**unpaired** two-sample band `z·√(se_c²+se_champ²)` (conservative). With CRN
(§2.1) providing per-case vectors on shared items, tighten to the **paired** SE
`sd(dᵢ)/√n` (or McNemar on binary per-case), which is smaller by the `−2·Cov`
term and detects real gaps with fewer items.

**Sequential / anytime-valid dethroning (next).** KOTH re-evaluates every epoch;
fixed-n tests are invalid under repeated "peeking." Promote the challenger the
first time an **anytime-valid confidence sequence** lower-bound on `d̄` exceeds a
promotion margin `δ_min > 0`. That indifference band is the KOTH margin, now
peek-safe. *Waudby-Smith & Ramdas, JRSS-B 2024, arXiv:2010.09686; Howard et al.
arXiv:1810.08240* `[P]`. (Design-only for now; the fold stays single-shot until
the platform can persist per-pair evidence across epochs.)

### 2.3 Fisher-information item selection (#5)

Model each item-template's difficulty β and discrimination α (2PL); item
information peaks where β≈θ and scales with α². Spend the item budget on
**high-α items whose difficulty sits at the champion boundary
θ\* = ½(θ_champion + θ_challenger)**, and drop **saturated** (everyone-passes) and
**floor** (everyone-fails) items — they carry ≈0 information there. `benchcal`'s
multi-seed harness is the natural place to estimate α/β from historical
submissions. *tinyBenchmarks arXiv:2402.14992; metabench arXiv:2407.12844 (858
items reconstruct 6 benchmarks at 0.58% RMSE); Rodriguez et al. ACL 2021 "items
are not equally informative"; CAT arXiv:2306.10512* `[P]`.

### 2.4 G-study variance decomposition + power sizing (#4)

Before sizing anything, decompose score variance into **item / seed / prompt /
judge** components (a crossed generalizability study), because the dominant facet
*flips with benchmark size*: large MCQ benches are item-variance-dominated ("buy
items"); small reasoning/agent benches are **seed/stochastic-dominated** — one
question can move Pass@1 >3 pts, so K≥10–30 seeds are recommended ("buy seeds") —
and **prompt-format variance often dwarfs both** (up to 76-pt swings from trivial
formatting). A memory+tool agent bench is almost certainly the small-N regime.
*Madaan arXiv:2406.10229; Hochlehnert "A Sober Look" arXiv:2504.07086; Sclar
FormatSpread arXiv:2310.11324* `[P]`. Power-size items via `n ≈ 7.85·ω²/δ²` for a
target detectable gap δ; report score ± **clustered** SE (items sharing a persona
are correlated — naive SE understates by up to 3×). A fully-worked
item×seed×prompt×judge decomposition for an *agent* benchmark is a near-open gap —
shipping one would be ahead of the literature.

### 2.5 Judge reliability

Current LLM judges frequently fail *intra-rater* reliability (identical verdict
only ~61% across reruns; ω/α often <0.8) — so grow the 2nd-model audit into a
small **cross-family panel with position-randomization + swap-and-average**, and
report inter-rater (target ≥80%) and intra-rater reliability. Prefer verifiable
objective scoring where the task allows (the LiveBench principle). Our graded
(partial-credit) memory scoring already helps: continuous metrics are far less
noisy than exact-match. *Zheng MT-Bench arXiv:2306.05685; "Rating Roulette"
arXiv:2510.27106; Schaeffer "Mirage" arXiv:2304.15004* `[P]`.

---

## 3. Anti-gaming layer

### 3.1 Metamorphic sub-scores (#3)

Score **relations**, reusing the generator (we own ground truth):
- **Invariance (INV):** ask a fact k paraphrastic ways → answers must agree.
  Scores *consistency*, generalizing the NoLiMa work into a scored delta.
- **Directional / monotonic (DIR):** add one trip → count must increment by
  exactly one; inject a later move → location answer must flip. A retriever that
  ignores updates fails DIR even while passing static recall.
- **Contradiction rate:** NLI between logically-linked answers.

*CheckList INV/DIR/MFT arXiv:2005.04118; METAL arXiv:2312.06056; contrast sets
arXiv:2004.02709; ParaRel arXiv:2102.01017; MQuAKE arXiv:2305.14795* `[P]`.

### 3.2 Per-run nonce canaries + integrity gate (#8)

Seed a unique high-entropy token into the conversation and ask a question whose
answer *is* that token — un-memorizable across runs, so it proves genuine
in-context retrieval and catches cross-run answer caching / a leaked static key.
Add "answer-key bait" (a plausible-but-wrong string in context) to separate
*reason* from *retrieve-and-echo*. Score these as pass/fail **disqualifiers**
(multiplicative), so trap failures can't be bought back with easy recall.
*Oren black-box contamination proof arXiv:2310.17623; BIG-bench canary; Jacovi
arXiv:2305.10160* `[P]`.

### 3.3 Calibration / proper scoring (#6)

Require a confidence per answer, scored by a **strictly proper rule** (Brier/log)
so honest confidence is the reward-maximizing strategy — a miner can't game it by
always saying "100%". Add a risk–coverage abstention slice where confident-wrong
is penalized more than abstaining. Most product-aligned dimension: a memory
assistant that doesn't confidently hallucinate. *Gneiting & Raftery JASA 2007;
Kadavath arXiv:2207.05221; Kamath selective QA arXiv:2006.09462* `[P]`.

---

## 4. Coverage / value layer

### 4.1 DRM false-memory & interference (#7)

Cognitive-science paradigm that maps directly onto vector memory. Seed converging
**lures never actually stated** (many trips to Portland/Seattle/Denver → "when did
I visit **San Francisco**?"): a similarity-retriever confidently fabricates; the
correct answer is abstention. Plus proactive/retroactive interference ("what was
my job *before* the current one" with near-duplicate facts). Make the
**embedding cosine-similarity between needle and lures an explicit difficulty
axis**, and report the *interference-induced accuracy drop* vs a matched control —
nearly impossible to game. *Roediger & McDermott 1995; RGB RAG robustness
arXiv:2309.01431* `[P]`. Inverting the agent-memory-*poisoning* threat models
(arXiv:2601.05504 `[U]`) into *tests* is genuinely novel ground.

### 4.2 Computed-answer modalities with matched controls (#9)

Each is a function of many seeded facts, not a lookup, and reports the
**ability-attributable delta** vs a control query:
- **Temporal-delta arithmetic:** "how long between starting at Acme and moving to
  Denver?" *TempReason arXiv:2306.08952* `[P]`.
- **Filtered aggregation:** "how many of my trips were *after* I changed jobs?"
- **Implicit-preference inference:** a preference never stated but derivable
  across sessions (books aisle seats, declines seafood twice → seat/meal choice).
- **Premise-poisoning-with-correction:** a subtly false presupposition → the agent
  must *correct and answer*, not blank-abstain; 3-way rubric (parrot / abstain /
  correct-and-answer), reward only the last. *CREPE arXiv:2211.17257; FalseQA;
  MQuAKE for multi-hop-over-updated-facts arXiv:2305.14795* `[P/S]`.

---

## 5. Build plan for #1 + #2

Staged so each step is independently valuable and consensus-safe:

1. **Fold (weights.py) — #2 core.** `_entry_stderr` (getattr); dethroning
   predicate uses `max(relative margin, z·√(se_c²+se_champ²))`; new
   `koth_dethrone_z` config. Backward-compatible (band=0 when no SEs). Unit
   tests: no-SE == today; with-SE a sub-uncertainty lead does NOT dethrone; a
   clear lead does. *(fully in-repo; ships first)*
2. **CRN seed plumbing — #1.** `dittobench-api` `/v1/submit` honors an optional
   `seed`; validator `dittobench.py` forwards `seed`; deterministic
   `crn_seed(champion_id, challenger_id, version)` helper (pure). Tests for
   determinism + distinctness.
3. **CRN re-score.** The worker's re-score sweep scores champion + tail on the
   common `crn_seed`, so refreshed composites are CRN-comparable; retain per-case
   for the future paired test.
4. **(next) Paired SE + anytime-valid sequential dethroning** once per-case /
   per-pair evidence is persisted.

**Cross-repo dependencies (flagged, not blocking):** the SE-aware fold and CRN
seed are additive-optional and **inert until the platform surfaces
`composite_stderr` on the ledger and reconciles CRN re-scores** — mirroring how
v2's version-bump re-score sweep is inert until the platform surfaces per-entry
`bench_version`. Nothing regresses in the meantime.

---

## 6. Sources (load-bearing, verified `[P]`)

Miller *Adding Error Bars to Evals* arXiv:2411.00640 · Chatbot Arena
arXiv:2403.04132 · Zhuang *Adaptive Testing for LLMs* arXiv:2306.10512 · Polo
*tinyBenchmarks* arXiv:2402.14992 · *metabench* arXiv:2407.12844 · Rodriguez et
al. ACL 2021 · Madaan arXiv:2406.10229 · Hochlehnert arXiv:2504.07086 · Sclar
arXiv:2310.11324 · Zheng *MT-Bench* arXiv:2306.05685 · Schaeffer arXiv:2304.15004
· Waudby-Smith & Ramdas arXiv:2010.09686 · Howard et al. arXiv:1810.08240 ·
Schenker & Gentleman 2001 · GSM-Symbolic arXiv:2410.05229 · CheckList
arXiv:2005.04118 · METAL arXiv:2312.06056 · ParaRel arXiv:2102.01017 · MQuAKE
arXiv:2305.14795 · Oren arXiv:2310.17623 · Gneiting & Raftery JASA 2007 · Kadavath
arXiv:2207.05221 · Kamath arXiv:2006.09462 · Roediger & McDermott 1995 · RGB
arXiv:2309.01431 · TempReason arXiv:2306.08952 · CREPE arXiv:2211.17257 · Cronbach
et al. 1972 / Brennan 2001 (G-theory).

*Post-cutoff arXiv ids (26xx.xxxxx) and single-source items are `[U]`/`[S]` in the
body — verify before citing externally.*
