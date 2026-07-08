# Semantic clone prevention — design and work plan

Status: design, 2026-07-07. Plan for the semantic tier of SN118 anti-copy:
detecting agents that implement the same strategy while differing at the source
and AST level. It extends the two fingerprint channels already in the gate; it
does not replace them. Companion to
[`BENCHMARK-V3-IDEAS.md`](BENCHMARK-V3-IDEAS.md).

---

## 1. Scope and the gap

SN118 is a winner-take-all KOTH+ATH competition: the champion agent holds weight
until dethroned. Two fingerprint channels currently guard the score-write path
(`ditto-platform/api_server/scoring_gate.py`, `evaluate_antidup`):

| Layer | Where | Signal | Tolerances | Not resistant to |
|---|---|---|---|---|
| L0 exact | platform upload/gate | tarball `sha256` equality | exact | any byte change |
| L1 lexical | platform `fingerprint.py` | bottom-k MinHash over 4-line whitespace-normalized shingles, k=256 | jaccard ≥ 0.75 or containment ≥ 0.95 | identifier renaming; reformatting |
| L2 structural | dittobench `astfp.go` (build time) | bottom-k MinHash over 6-node tree-sitter AST-type shingles, k=256 | jaccard ≥ 0.85 or containment ≥ 0.98 | AST refactor: reorder, wrap, extract-fn, control-flow rewrite |

Both require the candidate's composite to be within `_DEFAULT_SCORE_TOL = 0.03`
of the compared agent before a match holds, compare only against other miners'
eligible agents, and route a hit to `ath_pending_review` (a human hold, not an
auto-ban). The `first_seen` KOTH tie-break in `weights._champion` keeps the
earliest submission as champion, so a copy can at best tie and never dethrones.

L1 does not survive renaming, L2 does not survive a determined refactor, and
neither survives a re-implementation of the same strategy in a different style.
A semantic clone is the last case: same retrieval strategy, prompt, and tool
logic, expressed in code that shares neither tokens nor parse-tree shape. That is
the class this design targets.

---

## 2. Threat model

Adversaries, in rough order of likelihood:

1. Sybil / self-clone. One operator submits the same or lightly varied agent
   under multiple hotkeys. The gate exempts same-`miner_hotkey` comparisons, so
   this requires cross-hotkey detection.
2. Obfuscated copy. A miner obtains a competitor's crate and refactors it to
   pass L1+L2 while preserving behavior.
3. Strategy/prompt theft. Copies the system prompt, retrieval hyperparameters, or
   tool-use policy into otherwise-different code.
4. Behavioral reconstruction. Reimplements the champion's observed strategy from
   scratch: semantically equivalent, statically unrelated.

What the mechanism protects: the earliest-`first_seen` agent keeps its weight;
clones are held or rejected. The target property is cost(clone) > cost(innovate).

The central tension is copying versus convergence. Every honest agent built on
the reference harness shares the tool contract, standard top-k retrieval, and
similar prompt scaffolding. Those are not clones. A false positive in a
winner-take-all system is costly because a held agent earns zero while it waits,
so the detector must separate a copy from convergent independent work. Section 6
covers how.

---

## 3. Design principles

1. Tiered enforcement with original protection. Auto-reject only near-exact
   copies; route the ambiguous semantic band to `ath_pending_review`; never
   auto-ban. `first_seen` already prevents a copy from dethroning, so holding and
   adjudicating is preferred over rejecting.
2. Orthogonal signals, combined. A clone is similar on multiple independent axes
   (lexical, structural, behavioral). Convergent independents are similar on
   strategy but differ on trajectory-level detail. The semantic hold band
   requires agreement across orthogonal signals. This is the precision control
   against convergence.
3. Run each signal where it is observable and cheap. Static analysis at the
   screener, where the crate is unpacked and a Rust toolchain is present, before
   any scoring run. Behavioral analysis piggybacks on scoring and is escalated
   only for near-neighbors.
4. Moderation, not consensus. Every signal here feeds the hold/reject decision,
   never a signed score or the deterministic weight fold. Non-deterministic
   inputs (LLM embeddings, behavioral agreement rates) are therefore permitted.
5. Calibrate against convergence. Thresholds are set from the measured similarity
   distribution of known-independent agents (Section 6). No threshold ships
   without calibration.
6. Key signals on what a copy must preserve to keep the score: the prompt, the
   retrieval policy, the tool trajectory. Incidental structure a copier can
   freely rewrite is a weak signal.

---

## 4. Signal stack

New layers L3–L4 extend L0–L2. Each entry states what it detects, where it runs,
and the transform required to defeat it while preserving the score.

### L3 — semantic-static (screener, before scoring)

The screener unpacks the crate with a Rust toolchain
(`ditto-platform/api_server/endpoints/screener.py`; worker in `ditto-subnet`),
so it is the location for language-aware static analysis that runs before any
scoring run.

- L3a — normalized-source hash (exact-repack). Canonicalize the crate: strip
  comments, normalize whitespace, drop non-source files, sort files; hash the
  result. Detects repack/reformat/comment-strip/reorder copies that change
  `sha256` but not the source. Auto-reject tier. First to build (Phase S0).
- L3b — strategy/asset fingerprint. Extract and fingerprint the strategy surface
  independently of surrounding code: prompt templates (large string literals /
  format templates), retrieval config (embedding model id, chunk size, top-k,
  rerank policy, similarity metric), and the `Cargo.toml` dependency set. A copy
  that refactors code but keeps the prompt is detected here even when L1/L2
  differ. Defeating it requires changing the prompt or strategy.
- L3c — code-embedding similarity. Embed the normalized source with a code model;
  cosine against near-neighbors. Detects re-implementation that changes tokens
  and AST but preserves logic. Noisier than L3a/b, so it feeds the review band,
  not auto-reject.

### L4 — behavioral (score time, escalated for near-neighbors)

Two agents that produce the same outputs on the same inputs are clones regardless
of code. Grounded in what the trusted harness observes (Section 5): today that is
the tool-call trajectory.

- L4a — tool-call trajectory similarity. Reuse the `toolexec` observed trajectory
  (ordered `(name, args, hop)` per observable case,
  `dittobench-api/internal/toolexec`). Run the candidate and its suspected
  original on the same CRN seed (`crn_seed([cand, orig], version)`, reusing
  `ditto-subnet/validator/crn.py` and `score_tarball(seed=S)`); compare per-case
  trajectories. Match on trajectory idiosyncrasy (the same unexpected extra call,
  the same argument value, the same order), which convergence does not reproduce.
- L4b — answer/score co-behavior. On the same seed, compare per-case correctness
  patterns and answer-text hashes. A clone is correct and incorrect in the same
  cases, including the hard ones; independents correlate only on easy cases.
- L4c — retrieval and prompt trace. Deferred; requires new observability
  (Section 5).

### Escalation and fusion

L4 requires extra bench runs, so it fires only when L1–L3 place a candidate in a
near-neighbor band against a specific existing agent. Fusion rule (calibrated in
S0):

```
auto-reject  ⟸  L0 exact  OR  L3a normalized-hash exact
hold-review  ⟸  score within tol  AND  ≥2 orthogonal signals over their review thresholds
                (e.g. L2-structural high AND L4a-trajectory high;  or L3b-prompt high AND L3c-embed high)
accept       ⟸  otherwise
```

The ≥2-orthogonal-signal requirement for the hold band is the control against
convergence false positives.

---

## 5. Observability boundary

Behavioral detection can only compare what the trusted harness observes without
trusting the agent's self-report. From the sandbox map:

| Channel | Observable today | Mechanism | To make observable |
|---|---|---|---|
| Tool-call trajectory | yes, forge-proof | validator-served `toolexec` endpoint records `(name,args,hop)`; unobserved observable cases are capped at 0.5 | already available (L4a) |
| Answer correctness | yes | LLM judge over `FinalText` | already available (L4b) |
| Latency profile | yes | measured validator-side, overrides self-report | weak side signal |
| Memory retrieval (queries + hits) | no | memory tools withheld from the endpoint to avoid leaking answers | route memory tools through a recording endpoint that answers from the seeded store and logs the query; a redesign, not additive |
| LLM prompts | no | miner BYOK calls TLS-tunneled through a hostname-only CONNECT proxy | a TLS-terminating logging proxy, or validator-proxied LLM |

Consequence for the plan: the prompt is not observable at runtime but is present
in the source, so it is detected statically at L3b rather than behaviorally.
Behavioral detection starts with the tool-call trajectory (L4a/b). Extending
runtime observability to retrieval or prompts (L4c) is a Phase S3+ infrastructure
item, taken up only if static plus trajectory signals prove insufficient. It is
not built ahead of that evidence.

---

## 6. Calibration and evaluation

No threshold ships without this. Build a labeled corpus and an offline eval
harness (`clonecal`, mirroring `benchcal`) that scores each signal's
precision/recall against it.

Positive set (clones, must be detected). Take a seed agent (start from the
reference harness) and apply an obfuscation ladder, each tier a harder clone:

1. reformat + comment strip + rename identifiers (target L1/L3a);
2. reorder items, extract/inline functions, wrap expressions (target L2);
3. control-flow rewrite, equivalent-library swap, dead-code injection (target L3c);
4. prompt paraphrase, config renaming (target L3b + L3c);
5. clean-room re-implementation of the same strategy (target L4).

Negative set (independents, must not be flagged). Independent agents with
different strategies, plus convergent independents: two honest agents both on the
reference harness with standard retrieval. These define the false-positive floor.

Outputs: per-signal ROC; the fusion rule's precision at a target recall; and a
convergence false-positive budget (target: FP ≤ 1% on convergent-independent
pairs). Thresholds are chosen from these curves and re-checked whenever the
reference harness or bench version changes.

Red-team loop: the obfuscation ladder is adversarial and ongoing. When a clone
passes, add its transform to the ladder and re-calibrate.

---

## 7. Architecture and data flow

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

Cross-repo footprint:

- ditto-platform: L3a/b/c fingerprint storage and gate fusion (`fingerprint.py`,
  `scoring_gate.py`), the screener hooks, the review/dispute flow
  (`resolve_review`, a new appeal surface), moderation dashboards.
- dittobench-api: L3b/c static extraction at build/screen time (crate + Rust
  parser, as in `astfp.go`); L4 trajectory-digest emission into `RunDetails`; the
  same-seed behavioral job driver.
- ditto-subnet: behavioral escalation orchestration, reusing `crn.py` and the
  `_rescore_stale_champion_and_tail` pattern to run `{cand,E}` on one seed; an
  optional `status == SCORED` guard in `compute_weights`. Today the held⇒zero-
  weight guarantee rests entirely on the platform ledger query.
- ditto-harness / starter-kit: if L4c is pursued, the memory-tool recording
  redesign or a prompt-fingerprint contract addition lands here.

---

## 8. Phased work packages

Each phase is independently shippable and calibrated before the next.

- S0 — foundation (done).
  - Done. S0.1 this design doc.
  - Done. S0.2 normalized-source hash (L3a). `compute_normalized_source_hash` in
    `ditto-platform/api_server/fingerprint.py` (comment/whitespace/file-order
    canonicalization, string- and lifetime-safe, 8 tests) is wired into the gate:
    an `agents.normalized_source_hash` column and migration, computed at upload and
    surfaced on the eligible ledger, plus a `scoring_gate` rule that holds an
    exact-repack equality match in `ath_pending_review` like the exact-`sha256`
    rule (no score-proximity requirement). The auto-reject tier is deferred to S4;
    until then the rule holds for human review.
  - Done. S0.3 `clonecal` eval harness and labeled corpus:
    `ditto-platform/anticopy/clonecal.py` provides the obfuscation ladder (tier 1
    cosmetic, tier 2 rename), pluggable `Signal`s (L3a and L1 wired; L2/L3c/L4
    append later), a labeled corpus builder, and a precision/recall sweep that
    reports max recall subject to a precision floor. Demo corpus: L3a precision
    1.000, recall 0.500. 10 tests.
  - Acceptance: L3a detects ladder tier-1 repacks with zero FP on independents
    (met on the demo corpus). Next: seed `clonecal` with real reference-harness
    crates and wire the L2 signal so it reproduces the current L1/L2 tolerances'
    ROC.
- S1 — strategy-static (L3b), in progress.
  - Done. S1 #1 prompt fingerprint primitive. `compute_prompt_fingerprint` in
    `ditto-platform/api_server/fingerprint.py` extracts string literals (ordinary
    and Rust raw), keeps prompt-length ones (≥ 8 words), word-shingles them (5-word
    windows), and returns a bottom-k sketch compared by `content_similarity`
    (version `"p1"`, isolated from the lexical/structural channels). Not yet gated.
  - Done. S1 #2 `clonecal` calibration. `L3b_prompt_jaccard` / `_containment`
    signals wired; the demo corpus embeds distinct per-seed prompts and a
    convergent-independent pair (shared harness preamble, distinct strategy). On
    that corpus L3b reaches precision 1.0 / recall 1.0 — catching both ladder tiers
    where L3a and L1 sit at recall 0.5, because the tier-2 rename defeats them but
    preserves the prompt. On the convergent pair L3b fires (0.82) while L1 and L3a
    are 0.0: no single signal is both firing and correct, so L3b stays review-band.
  - Done. S1 #3 gate plumbing (shadow mode). `agents.prompt_fingerprint` column
    and migration; computed at upload, surfaced on the eligible ledger, and passed
    to `evaluate_antidup`, which appends a prompt-overlap note to the audit reason
    of a hold another rule already fired. The prompt sketch does not create a hold
    on its own. Reason: the S1 #2 convergent case scores ~0.8 on the prompt signal
    and structural is likewise shared by same-harness crates, so neither is
    orthogonal to convergence; a prompt-based hold would false-positive on
    convergent independents. Storing the sketch now gives every agent a prompt
    fingerprint for later calibration.
  - Deferred: config/dependency extraction; the active prompt-fusion hold (the
    ≥2-signal band), which needs an orthogonal-to-convergence signal (L3c or L4) to
    corroborate the prompt without holding on same-harness scaffolding. Acceptance:
    detects tier-4 prompt/config theft; FP within budget on convergent independents
    under the ≥2-signal fusion rule.
- S2 — code-embedding (L3c), in progress. Code-embedding similarity as a
  review-band signal; the first signal orthogonal to convergence, which unblocks
  the S1 prompt-fusion hold.
  - Model (Open decision 1, resolved): self-host. Primary Qwen3-Embedding-0.6B
    (Apache-2.0, 32k context, MRL output dims, deployable via
    text-embeddings-inference); CPU fallback jina-embeddings-v2-base-code (161M,
    8192 context, Rust-aware, code-similarity trained). Hosted APIs (voyage-code-3,
    zembed-1) rejected: embedding private miner crates off-platform is unacceptable
    egress, and they are non-reproducible and per-call.
  - Done. S2 #1 embedding-input builder. `compute_embedding_input` in
    `ditto-platform/api_server/fingerprint.py` produces the deterministic text fed
    to the model (comments and blank lines dropped, code kept; files sorted and
    joined without path names; capped to the backend context window). Model-free
    and unit-tested.
  - Done. S2 #2–#4 client + infra. `ditto-platform/api_server/embedding/` — an
    env-driven `EmbeddingConfig` (disabled unless `L3C_EMBEDDER_URL` is set), a
    best-effort `Embedder` (null when disabled; TEI client otherwise), and a pure
    `cosine`. The upload path embeds each crate and stores the vector +
    `model@revision` tag on `agents.code_embedding` / `code_embed_model` in shadow
    mode; the ledger surfaces both (same-model comparison only). `clonecal`
    `embedding_signal(embed)` wires the cosine signal for calibration. The TEI
    service ships as an opt-in `embedder` compose profile with `.env` + Make
    targets + `docs/l3c-embedder.md`.
  - Remaining: provision the service on the deployed host (infra repo — add
    `embedder` to `DITTO_COMPOSE_SERVICES` + `L3C_EMBEDDER_*`); backfill embeddings
    for existing agents (re-embed sweep); calibrate the prompt+embedding ≥2-signal
    fusion on real crates; then activate the hold. Acceptance: raises tier-3 recall
    without breaching the FP budget under the ≥2-signal fusion rule.
- S3 — behavioral (L4a/b). Trajectory-digest emission, same-seed escalation job,
  and fusion into the gate. Acceptance: separates tier-5 clean-room clones from
  convergent independents at the target precision.
- S4 — ops and hardening. Dispute/appeal flow, moderation dashboard, the
  `compute_weights` status guard, and the red-team loop (Section 6). Decide here,
  on evidence, whether L4c (retrieval/prompt observability) is worth its infra and
  privacy cost.

---

## 9. Open decisions

1. Embedding model for L3c (resolved). Self-host, not hosted: agent crates are
   private miner IP, so embedding them through a hosted API is unacceptable egress,
   and hosted models are non-reproducible and per-call. Primary
   Qwen3-Embedding-0.6B (Apache-2.0, 32k context, MRL dims 32–1024,
   text-embeddings-inference); CPU fallback jina-embeddings-v2-base-code (161M,
   8192 context, 31 languages incl. Rust, trained on code↔code pairs). See the S2
   entry in Section 8.
2. L4c: whether prompt/retrieval observability warrants a TLS-terminating proxy
   or a memory-tool redesign, or whether L3b (static prompt) plus L4a (trajectory)
   suffice. Deferred to S4 evidence.
3. Sybil/self-clone across hotkeys: whether to add an operator-identity signal
   (funding graph, timing, infra fingerprint) or rely on artifact similarity.
   Out of scope here; belongs to a separate anti-sybil design.
4. Dispute SLA: since holds earn zero weight, the maximum adjudication latency and
   who adjudicates (owner CLI today; automated process later).
```
