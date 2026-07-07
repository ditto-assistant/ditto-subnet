# Semantic clone prevention — design & work plan

Status: **design, 2026-07-07 (Nick).** This is the plan for the *semantic* tier
of SN118's anti-copy defense — catching agents that do the **same thing** while
looking different at the source and AST level. It builds on, and does not
replace, the two fingerprint channels already in production.

> Companion to [`BENCHMARK-V3-IDEAS.md`](BENCHMARK-V3-IDEAS.md) (scoring
> reliability). Where that doc makes *scores* trustworthy, this one makes
> *authorship* trustworthy.

---

## 1. Why this, on top of what exists

SN118 is a winner-take-all KOTH+ATH competition: the champion agent holds weight
until dethroned. The standing incentive is therefore to **take the champion's
work rather than out-innovate it**. Today two fingerprint channels guard the
score-write path (`ditto-platform/api_server/scoring_gate.py`,
`evaluate_antidup`):

| Layer | Where | Signal | Tolerances | Defeated by |
|---|---|---|---|---|
| **L0 exact** | platform upload/gate | tarball `sha256` equality | exact | any byte change |
| **L1 lexical** | platform `fingerprint.py` | bottom-k MinHash over 4-line whitespace-normalized shingles (k=256) | jaccard ≥ 0.75 **or** containment ≥ 0.95 | renaming identifiers; reformatting |
| **L2 structural** | dittobench `astfp.go` (build time) | bottom-k MinHash over 6-node tree-sitter AST-type shingles (k=256) | jaccard ≥ 0.85 **or** containment ≥ 0.98 | AST refactor: reorder, wrap, extract-fn, control-flow rewrite |

Both require the candidate's composite to be within `_DEFAULT_SCORE_TOL = 0.03`
of the compared agent before a match holds, compare only against **other**
miners' eligible agents, and route a hit to `ath_pending_review` (a human hold,
never an auto-ban; the original is protected by the `first_seen` KOTH tie-break
in `weights._champion`).

**The gap.** L1 dies to renaming, L2 dies to a determined refactor, and **both
die entirely to a re-implementation** of the same strategy in a different style.
A "semantic clone" is exactly that: same retrieval strategy, same prompt, same
tool logic, expressed in code that shares neither tokens nor parse-tree shape.
That is the class this design targets.

---

## 2. Threat model

**Adversaries (in rough order of likelihood):**

1. **Sybil / self-clone.** One operator submits the same (or lightly varied)
   agent under multiple hotkeys to occupy the tail or hedge. (The gate exempts
   *same-`miner_hotkey`* comparisons, so this specifically needs cross-hotkey
   detection.)
2. **Obfuscated copy.** A miner obtains a competitor's crate (leak, insider, a
   published starter kit) and refactors it to slip L1+L2 while preserving
   behavior.
3. **Strategy/prompt theft.** Copies the crown jewels — the system prompt,
   retrieval hyperparameters, tool-use policy — into otherwise-different code.
4. **Behavioral reconstruction.** Reimplements the champion's *observed*
   strategy from scratch. Semantically equivalent, statically unrelated.

**What we protect:** the **original** (earliest `first_seen`) keeps its weight;
clones are held or rejected. The economic goal is **cost(clone) > cost(innovate)**
— we don't need perfect detection, we need to make copying the losing play.

**The central tension — copying vs. convergence.** Every honest agent built on
the reference harness shares a lot: the same tool contract, standard top-k
retrieval, similar prompt scaffolding. **These are not clones.** A false positive
in a winner-take-all system is expensive (a held agent earns zero while it
waits), so the load-bearing requirement is a detector that distinguishes a
*copy* from *convergent independent work*. Section 6 is how.

---

## 3. Design principles

1. **Original-protected, tiered enforcement.** Auto-reject only near-exact
   copies; send the ambiguous semantic band to `ath_pending_review`, never
   auto-ban. `first_seen` already guarantees a copy can at best tie and never
   dethrone, so time is on the original's side — we can afford to *hold and
   adjudicate* rather than *reject and be wrong*.
2. **Orthogonal signals, combined.** A clone is similar on *multiple independent*
   axes (lexical **and** structural **and** behavioral). Convergent independents
   are similar on strategy but diverge on trajectory-level idiosyncrasy. Require
   **agreement across orthogonal signals** before flagging the semantic band —
   this is what buys precision against convergence.
3. **Run each signal where it's observable and cheap.** Static semantic analysis
   at the **screener** (crate already unpacked, Rust toolchain present, *before*
   an expensive bench run). Behavioral analysis **piggybacks on scoring** and is
   only escalated for near-neighbors.
4. **Moderation, not consensus.** Every signal here feeds the **hold/reject**
   decision, never a signed score or the deterministic weight fold. So it may use
   non-deterministic inputs (LLM embeddings, behavioral agreement rates) that
   would be illegal in the weight path.
5. **Calibrate against convergence, not against zero.** Thresholds are set from
   the measured similarity distribution of *known-independent* agents, not from
   first principles. No number ships un-calibrated (Section 6).
6. **Raise the cost of the winning behavior, not the incidental code.** The best
   signals key on the parts a cloner *must* preserve to keep the score (the
   prompt, the retrieval policy, the tool trajectory) — not on incidental
   structure they can freely rewrite.

---

## 4. The signal stack

New layers L3–L4 extend the existing L0–L2. Each row: what it catches, where it
runs, and the cost to defeat it *while keeping the winning score*.

### L3 — semantic-static (screener, pre-bench)

The screener already downloads and unpacks the crate with a Rust toolchain
(`ditto-platform/api_server/endpoints/screener.py`; worker in `ditto-subnet`).
It is the natural home for language-aware static analysis that runs **before**
any expensive scoring.

- **L3a — normalized-source hash (exact-repack).** Canonicalize the crate
  (strip comments, normalize whitespace, drop non-source files, alpha-rename
  locals if cheap, sort files) → single hash. Catches repack/reformat/comment-
  strip copies that change `sha256` but nothing that matters. *Auto-reject tier.*
  Cheap; the first thing to build (Phase S0).
- **L3b — strategy/asset fingerprint.** Extract the **strategy surface** from the
  source and fingerprint it independently of the surrounding code:
  - **prompt templates** — large string literals / format templates that are LLM
    prompts (the crown jewel; a clone that refactors code but copies the prompt
    is caught here even though L1/L2 differ);
  - **retrieval config** — embedding model id, chunk size, top-k, rerank policy,
    similarity metric;
  - **dependency set** — `Cargo.toml` crates + versions.
  Normalize and MinHash/embed each surface separately. High precision for prompt
  and config theft. Defeating it means *changing the prompt/strategy* — i.e.
  doing real work.
- **L3c — code-embedding similarity.** Embed the normalized source with a code
  model; cosine against near-neighbors. Catches re-implementation that changes
  tokens **and** AST but preserves logic — the case L1/L2 miss entirely. Noisier
  than L3a/b (an LLM embedding), so it feeds the **review band**, not auto-reject.

### L4 — behavioral (score time, escalated for near-neighbors)

The semantic ground truth: two agents that *do the same thing* on the same inputs
are clones regardless of code. **Grounded in what the trusted harness can
actually observe** (Section 5) — today that is the tool-call trajectory.

- **L4a — tool-call trajectory similarity.** Reuse the forge-proof `toolexec`
  observed trajectory (ordered `(name, args, hop)` per observable case,
  `dittobench-api/internal/toolexec`). Run the candidate and its suspected
  original on the **same CRN seed** (`crn_seed([cand, orig], version)`, reusing
  `ditto-subnet/validator/crn.py` + `score_tarball(seed=S)`); compare per-case
  trajectories. Key: match on trajectory **idiosyncrasy** — the *same* unexpected
  extra call, the *same* argument value, the *same* order — which convergence
  does not reproduce (independents agree on the *correct* action, not on quirks).
- **L4b — answer/score co-behavior.** On the same seed, compare per-case
  correctness patterns and answer-text hashes. A clone is right-and-wrong in the
  *same places* including the hard cases; independents correlate only on easy
  ones.
- **L4c — retrieval & prompt trace (deferred — needs new observability,
  Section 5).**

### Escalation & fusion

L4 is expensive (extra bench runs), so it only fires when L1–L3 place a candidate
in a **suspicious near-neighbor band** against a specific existing agent. The
fusion rule (calibrated in S0):

```
auto-reject  ⟸  L0 exact  OR  L3a normalized-hash exact
hold-review  ⟸  score within tol  AND  ≥2 orthogonal signals over their review thresholds
                (e.g. L2-structural high AND L4a-trajectory high;  or L3b-prompt high AND L3c-embed high)
accept       ⟸  otherwise
```

Requiring **≥2 orthogonal signals** for the semantic hold band is the precision
lever against convergence.

---

## 5. The observability boundary (load-bearing constraint)

Behavioral detection can only compare what the **trusted** harness observes
without trusting the agent's self-report. From the sandbox map:

| Channel | Observable today? | Mechanism | To make it observable |
|---|---|---|---|
| **Tool-call trajectory** | ✅ **yes, forge-proof** | validator-served `toolexec` endpoint records `(name,args,hop)`; unobserved observable cases are capped at 0.5 | — (use as-is: L4a) |
| **Answer correctness** | ✅ yes | LLM judge over `FinalText` | — (use as-is: L4b) |
| **Latency profile** | ✅ yes | measured validator-side, overrides self-report | weak side signal |
| **Memory retrieval** (queries + hits) | ❌ no | memory tools deliberately **withheld** from the endpoint (serving them leaks answers) | route memory tools through a **recording** endpoint that answers from the seeded store *and* logs the query — redesign, not additive |
| **LLM prompts** | ❌ no | miner BYOK calls TLS-tunneled through a hostname-only CONNECT proxy | a **TLS-terminating logging proxy**, or validator-proxied LLM (BYOK → validator) — infra + privacy cost |

**Consequence for the plan.** The *prompt* — often the single most valuable,
most-copied artifact — is **invisible at runtime but visible in the source**, so
we catch it **statically at L3b**, not behaviorally. Behavioral detection starts
with the tool-call trajectory (L4a/b), which exists and is forge-proof.
Extending runtime observability to retrieval/prompts (L4c) is a **Phase S3+
infrastructure investment, gated on evidence** that static + trajectory signals
are insufficient — we do not build a TLS-terminating proxy on spec.

---

## 6. Calibration & evaluation (the part that makes it real)

No threshold ships without this. Build a **labeled corpus** and an **offline eval
harness** (a `clonecal`, mirroring `benchcal`) that scores every signal's
precision/recall against it.

**Positive set (clones — must be caught).** Take a seed agent (start from the
reference harness) and apply an **obfuscation ladder**, each tier a harder clone:

1. reformat + comment strip + rename identifiers (must die to L1/L3a);
2. reorder items, extract/inline functions, wrap expressions (must die to L2);
3. control-flow rewrite, equivalent-library swap, dead-code injection (target L3c);
4. prompt paraphrase, config renaming (target L3b + L3c);
5. clean-room re-implementation of the same strategy (target L4).

**Negative set (independents — must NOT be flagged).** Genuinely independent
agents with different strategies, **plus** the hard case: *convergent
independents* — two honest agents both on the reference harness with standard
retrieval. These define the false-positive floor.

**Outputs:** per-signal ROC; the combined fusion rule's precision at a target
recall; and a **convergence false-positive budget** (e.g. FP ≤ 1% on the
convergent-independent pairs). Thresholds are chosen from these curves, then
re-checked whenever the reference harness or bench version changes.

**Red-team loop.** Treat the obfuscation ladder as adversarial and living: each
time a real clone slips through, add its transform to the ladder and re-calibrate.

---

## 7. Architecture & data flow

```
 upload ──► SCREENER (uploaded→evaluating) ───────────────────────────┐
            • L3a normalized-source hash        → exact repack? AUTO-REJECT
            • L3b strategy/asset fingerprint     ┐
            • L3c code-embedding similarity      ├─ store fingerprints on agent
            • (L1/L2 fingerprints)               ┘   flag near-neighbors
                                                                       │
 score  ──► DITTOBENCH (evaluating, scored by validator) ─────────────┤
            • existing scoring + L2 structural fp
            • IF flagged near-neighbor E:
                escalate → behavioral job: run {cand,E} on crn_seed
                emit trajectory digests (L4a) + co-behavior (L4b) in details
                                                                       │
 gate   ──► PLATFORM evaluate_antidup (evaluating→scored) ────────────┤
            • fuse L0..L4 signals (§4 rule)
            • AUTO-REJECT | HOLD(ath_pending_review, duplicate_of) | ACCEPT
                                                                       │
 adjudicate ► ath_pending_review ──► resolve_review → scored | banned ┘
              • original protected by first_seen (zero weight while held)
              • dispute/appeal path (new); no auto-timeout
```

**Cross-repo footprint:**
- **ditto-platform** — most of it: L3a/b/c fingerprint storage + gate fusion
  (`fingerprint.py`, `scoring_gate.py`), the screener hooks, the review/dispute
  flow (`resolve_review`, a new appeal surface), moderation dashboards.
- **dittobench-api** — L3b/c static extraction at build/screen time (it has the
  crate + Rust parser, like `astfp.go`); L4 trajectory-digest emission into
  `RunDetails`; the same-seed behavioral job driver.
- **ditto-subnet** — behavioral escalation orchestration (reuse `crn.py` +
  `_rescore_stale_champion_and_tail` pattern to run `{cand,E}` on one seed); an
  optional belt-and-suspenders `status == SCORED` guard in `compute_weights`
  (today the held⇒zero-weight guarantee rests entirely on the platform ledger
  query — a single point of failure worth hardening).
- **ditto-harness / starter-kit** — if we pursue L4c, the memory-tool recording
  redesign and/or a prompt-fingerprint contract addition land here.

---

## 8. Phased work packages

Each phase is independently shippable and calibrated before the next.

- **S0 — foundation (IN PROGRESS).**
  - ✅ S0.1 this design doc.
  - 🟡 S0.2 **normalized-source hash (L3a)** — `compute_normalized_source_hash`
    landed in `ditto-platform/api_server/fingerprint.py` (comment/whitespace/file-
    order canonicalization, string+lifetime safe, 8 tests). **Still to do:** the
    gate wiring (an `agents.normalized_source_hash` column + migration + a
    `scoring_gate` rule; auto-reject tier deferred to S4 — for now it holds like
    the exact-`sha256` rule).
  - ✅ S0.3 **`clonecal` eval harness + labeled corpus** —
    `ditto-platform/anticopy/clonecal.py`: obfuscation ladder (tier 1 cosmetic,
    tier 2 rename), pluggable `Signal`s (L3a + L1 wired; L2/L3c/L4 append later),
    labeled corpus builder, precision/recall sweep selecting **max recall s.t. a
    precision floor**. Demo corpus: L3a precision 1.000 / recall 0.500. 10 tests.
  - **Acceptance:** L3a catches ladder tier-1 repacks with zero FP on independents
    ✅ (demo); next, seed `clonecal` with real reference-harness crates + wire L2
    so it reproduces the current L1/L2 tolerances' ROC.
- **S1 — strategy-static (L3b).** Prompt/config/dependency extraction +
  fingerprint, wired as a review-band signal. **Acceptance:** catches tier-4
  prompt/config theft; FP within budget on convergent independents.
- **S2 — code-embedding (L3c).** Code-embedding similarity as a review-band
  signal; pick/host the embedding model. **Acceptance:** lifts tier-3 recall
  without breaching the FP budget when fused (≥2-signal rule).
- **S3 — behavioral (L4a/b).** Trajectory-digest emission + same-seed escalation
  job + fusion into the gate. **Acceptance:** distinguishes tier-5 clean-room
  clones from convergent independents at the target precision.
- **S4 — ops & hardening.** Dispute/appeal flow, moderation dashboard, the
  `compute_weights` status guard, and the **red-team loop** (Section 6). Decide
  here, on evidence, whether L4c (retrieval/prompt observability) is worth its
  infra + privacy cost.

---

## 9. Open decisions

1. **Embedding model for L3c** — hosted (OpenRouter, like the judge) vs. a local
   code-embedding model (reproducibility, cost, no egress). Leaning local.
2. **L4c appetite** — is prompt/retrieval observability worth a TLS-terminating
   proxy or a memory-tool redesign, or do L3b (static prompt) + L4a (trajectory)
   suffice? Defer to S4 evidence.
3. **Sybil/self-clone across hotkeys** — do we add an operator-identity signal
   (funding graph, timing, infra fingerprint), or rely purely on artifact
   similarity? Out of scope here; note for a separate anti-sybil design.
4. **Dispute SLA** — since holds earn zero weight, what's the max adjudication
   latency, and who adjudicates (owner CLI today; automated tribunal later)?
```
