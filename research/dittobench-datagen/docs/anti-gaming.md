# DittoBench anti-gaming: v3 hardening and protocol specs

This document records the anti-gaming work that ships as **bench_version 3** and
specifies the on-chain protocol defenses that live in `ditto-subnet` /
`ditto-platform` (outside this repo). It is the companion to the audit that
motivated it.

Two invariants constrain every change here:

- **Trustless scoring.** A score must stay a pure, reproducible function of
  `(dataset, transcript)` that any validator or third party re-derives
  byte-for-byte. No validator-held secrets, no model inference, and no
  nondeterminism in the score path. This is why we did **not** adopt the audit's
  original top recommendation (a validator-side LLM paraphrase of the haystack):
  a model in the validator makes scoring non-reproducible and turns the validator
  into a trusted party. The same reasoning rules out a secret-holdout / code-in-
  secret-sandbox design.
- **Simple validator.** The validator keeps doing exactly what it does today:
  generate the dataset from the seed with this public code, run the harness,
  grade deterministically, submit a signed score. Detection that cannot be made
  part of a deterministic score (parser-vs-reasoner behavioural signals) lives in
  the **screener** and in offline **`gstudy`** analysis, never in the composite.

## Root cause the hardening addresses

The generator is a public deterministic function `G(seed) → (haystack,
questions, answers)`, and for memory recall the answer was a **verbatim
substring** of the haystack the harness is handed in cleartext. A harness holding
the generator source therefore holds the inverse: it can recover answers by
parsing, with no reasoning. Surviving a public generator is possible; being
*fully forward-computable by the miner* is not. The v3 work attacks that on two
fronts: make the task genuinely require selection/reasoning (so cheating becomes
isomorphic to doing it), and stop the scorer rewarding the parser's determinism.

## What v3 changes (this repo + dittobench-api)

Grader / generator (`dittobench-datagen`):

- **Dump guard** (`grade`, `persona`): value-recall cases carry a `DumpGuard`
  set — the user's *other* current self values. Surfacing a large fraction of
  them (a whole-self-table dump, the parser's cheapest strategy) scores 0, while
  one or two incidental mentions are fine. This closes the cross-attribute dump
  that defeated the same-attribute-only distractor scan.
- **Needle gating** (`toolexec`): the result-usage needle is served only by the
  case's needle-bearing tool (the last expected content tool) with a per-seed
  sentence template and an inline decoy; non-bearer tools serve a decoy number.
  Fishing a number from the easiest/wrong tool now yields a wrong value. The
  bearer's sentence joins the needle clause and the inline decoy clause with a
  per-seed connective (`needleConnectives`) in a per-seed ORDER, and the
  non-bearer decoy sentence is built from the same two-clause family — so neither
  "the first number" nor "the number before a fixed marker" reliably yields the
  needle, and the bearer is not identifiable by a parenthetical/marker alone.
  Extracting the needle requires associating the value with the ASKED subject
  (each clause names its subject next to its value; the honest reader keys on
  subject-proximity). Gated by `TestRedTeamResultUsagePositionParserFails`.
- **Coreference counting** (`persona`, V4): recurring-topic mentions name the
  topic once (the anchor) and refer to it obliquely afterward, so a
  literal-*label* counter undercounts. This raises the cost of the naive
  counter but does not force reading: the oblique referent is a fixed per-topic
  phrase, so a source-holding parser that greps `label OR coref` still recovers
  the count exactly. Making a cleartext-haystack count model-required is not
  achievable task-side (see "Deterministic solver families" below); this
  mechanism only defeats a counter hard-coded to the label.
- **Sentiment reversals** (`persona`, V5): opinion reversals are conveyed by
  sentiment rather than the classic cessation lexicon, so a parser hard-coded
  to grep "no longer"/"stopped" misclassifies them. It does not force reading:
  deterministic grading credits a fixed sentiment lexicon (`cessationPhrases`),
  and that lexicon appears verbatim in the evidence, so a source-holder greps
  the reversal-vs-standing stems and classifies by membership. As with count,
  the durable defense is the screener oracle plus the on-chain transform audit,
  not the task-side surface.
- **Adversarial NoOp distractor** (`persona`): each run seeds one "considered but
  rejected" mention whose value becomes a distractor, so a similarity retriever
  that grabs it scores 0 while a reader honouring the negation is unaffected.
- **Grep-baseline gate** (`gen/redteam_test.go`): a CI gate runs a non-reasoning
  baseline (the omniscient dumper, literal-label counter, cessation-grepper)
  against fresh suites and fails the build if any of them scores. This is the
  standing regression check for every shortcut above.

Scorer (`dittobench-api`):

- **De-inversion**: the canary honest-miss penalty (a pure variance tax that fell
  only on a nondeterministic model's coin-flip miss) is removed; the hard leak
  disqualifier stays. Observed execution is mandatory for scored runs (unobserved
  observable cases score 0, not a capped 0.5), and the memory-routing free point
  is closed. Result-usage scoring can consume the served decoy as a distractor.
- **Calibration honesty**: the σ / difficulty gates are documented as needing
  calibration against a real locked-model harness's measured variance, not the
  zero-variance reference lookup; a `--mem-profile` seam feeds a recorded profile.

Screener (`ditto-screener`):

- Unfingerprintable fake gateway, an always-on behavioural challenge with a
  gateway-encoded nonce oracle (a static table cannot make the round-trip),
  timing/round-trip thresholds, opaque-blob surfacing in source review, and an
  image-binds-to-reviewed-crate reject in the stable core. These are screening
  signals: an objective behavioural failure withholds admission; heuristic
  signals quarantine. None touch the trustless score.

Advisory detection (`gstudy`): a parser-signature report (trap-conditioned
accuracy gap between plain recall and synthesis families; latency flatness)
routes flagged harnesses to audit. Advisory only — never folded into the score.

## Compliance laundering (second-pass hardening)

A later review of a released submission found a class the mechanisms above did
not fully close: compliance laundering. The harness let the model comply with an
injection or an unanswerable case, then edited the graded response to hide it. It
deleted benchmark-shaped nonce tokens from `final_text` and cleared the `answer`
and `abstain` wire slots on detected injection cases. These transforms change no
agent behavior. In production, echoing a nonce or leaving a slot set harms
nobody. They only rewrite a graded outcome. Nothing detected "am I being
benchmarked" and nothing used a lookup table, so it passed screening. The fix is
to make the laundering impossible or self-defeating, on-task and in the
deterministic score.

- Slot and abstain-flag manipulation (`grade`, screening). Clearing the wire
  `answer` and `abstain` slots to dodge the abstain-on-answerable zero is handled
  where it belongs, not by a prose scan. Clearing the flag only helps when the
  correct value is present in the response anyway, and broad memory-dumping to
  make that happen is already caught by the dump guard. Content-keyed mutation of
  the slots is prohibited by the screening bright-line rule below. An earlier
  draft added a grader-side prose-hedge scan for this, but it was removed. Its
  only effect over the positive check was to zero value-containing responses that
  also hedge, which are usually legitimate. The clearest case is an injection
  resister that disputes the fake premise in memory terms ("I have no record of
  any policy update; you live in Lisbon"), the exact honest harness the injection
  cases reward.
- Grammar collision (`persona.CoinShaped`). Every coined token in a run (the
  canary nonce and bait, the injection payload, the lifecycle read answers) now
  shares one per-seed shape family, selected from four. A harness that scrubs
  benchmark-shaped tokens from its output deletes its own required answers (the
  canary and lifecycle values) along with the payload. The pre-v3 design used
  distinct shapes per family, which is what let a shape-keyed scrubber suppress
  payloads while keeping answers.
- Payload noise-plant (`persona`). The run's injection payload is also seeded
  once as an innocuous stored reference code, so the other cheap scrub heuristic,
  "delete coined tokens not present in my retrieved context", keeps the payload
  and the compliance stays visible.
- Trajectory-anchored bait (`persona`, `grade`, `dittobench-api`). One sibling of
  the injection family attacks via an action, for example "email this token with
  `gmail_send`". Compliance is a tool call the validator observes through
  `tool_endpoint`. The scorer substitutes the observed trajectory into the graded
  response before grading, so the call reaches the grader even if every trace was
  scrubbed from the output text. `MemoryCase.BaitTool` names the tool. An observed
  call to it scores 0 and flags the case.
- Injection-framing invariance family (`persona.injectionTwins`). The same fact
  is attacked via several override framings, text and action, sharing a
  `TwinGroup`. A grounded reader resists all of them alike. A framing-brittle
  scrubber splits the family and loses the metamorphic-consistency factor on top
  of the per-case zeros.
- Red-team gates (`gen/redteam_test.go`). Three CI baselines lock these in: a
  shape-scrubber cannot separate forbidden from required tokens, scrubbing coined
  tokens zeros the canary, and a whole-self-profile dump is zeroed by the dump
  guard.

Screener and policy companion (`dittobench-api/PROTOCOL.md`, `ditto-screener`): a
bright-line rule now states that any post-generation mutation of `final_text`,
`answer`, or `abstain` keyed to graded content is prohibited and is grounds for
rejection. This closes the legal-by-omission gap that let the laundering pass.
Uniform formatting stays fine.

## Deterministic solver families, the canary, and injection framing

A survey of the rejected-submission corpus shows the dominant surviving attack is
the deterministic scored-family solver: a harness fingerprints a question family
and computes the answer from the cleartext haystack without the model. v3 raised
the parser-construction cost for recurrence count (coreference) and opinion
reversal (sentiment), but be precise about what that buys: because the answer is
always a function of the cleartext haystack, a source-holding parser still
recovers count (grep `label OR coref`) and reversal (grep the credited sentiment
stems) without reading. The same is true of the other timeline families
(point-in-time / as-of, first-mention ordering, elapsed duration, and
occupation-vs-employer), which are timeline and attribute math over data the
harness legitimately holds: the pair timestamps and the value pools. **No memory
family is made model-required by task-side obfuscation** — a harness that solves
any of them is doing correct reasoning, just without a model. Task-side changes
only defeat a parser hard-coded to the *old* surface and lock difficulty against
regression via the red-team gates. The durable defenses are therefore not
task-side obfuscation but:

- the behavioral oracle (screener), which forces genuine model invocation on
  scored traffic so a pure deterministic solver cannot pass. See the
  oracle-indistinguishability work in `ditto-screener`.
- the on-chain reproduce-under-transform audit, which makes a lookup-or-solve
  harness negative-EV.
- `gstudy` parser-signature detection (family-conditioned accuracy gap, latency
  flatness) routing suspects to audit.

Task-side, v3 adds standing red-team gates that lock the difficulty so it cannot
silently regress. The point-in-time gate asserts that a current-value shortcut
(answer the present value, ignore the date) scores 0, and it fails if the
generator regresses toward current-valued as-of answers. The canary gates are
below.

Two task-side hardenings that are effective landed here.

- Canary multi-decoy (`persona`). A second high-entropy code-shaped decoy,
  attributed to a different person and wired as a distractor on the canary
  question, turns the canary from a rare-token-retrieval test into an attribution
  test. The rejected "detect rare code-shaped values and force-surface all of
  them" shortcut now surfaces a decoy and is zeroed, while user-scoped retrieval
  that returns only the user's own nonce passes. Gated by
  `TestRedTeamRarityRetrieverTripsCanaryDecoy`.
- Injection non-trailing framings (`persona`). The injection-template pool gains
  framings that embed the real question before or mid-attack, defeating the cheap
  "strip to the trailing question" heuristic seen in rejected harnesses while a
  genuine injection defense still locates the question.

### Grading lexicons are version-scoped, not secret

The credited stance lexicons (`grade.cessationPhrases`, including the v3
negative-stance sentiment group, and `grade.persistencePhrases`) have the same
status as every other grader detail: public, deterministic, and frozen for the
life of a `bench_version`. Within a version, a miner may rely on them exactly
as they rely on any grading rule, and prompting a model to phrase a grounded
reversal with a credited surface ("no longer", "lost interest") is on-task
behavior, not laundering — the token has to be attached to the right
remembered value to score. What the lexicons are NOT is stable across
versions: the template-space refresh cadence (#3 below) rotates evidence and
credited surfaces together, so a harness tuned to exact strings re-pays the
adaptation cost each bump. The durable strategy is the faithful one: recover
the right fact and state its current stance plainly, which the grader must
always credit because it must credit the evidence surface the generator
itself uses. Proposed additions to the sentiment group are cheap to take and
are regression-gated by `gen/redteam_test.go`.

## On-chain protocol specs (ditto-subnet / ditto-platform)

These defeat channels the score alone cannot (copying, relay, precompute) and
must be built on-chain. They are compatible with both invariants: each is either
a timing/economic constraint on submission or a deterministic, reproducible
re-check — none require a validator secret or model inference.

### 1. Miner response commit-reveal (timelock)

**DEFERRED, considered and not dropped (2026-07-18).** Its own track, not part
of the v3 rollup. It is a standalone on-chain protocol change with its own
timelock/keyshare machinery and a tight reveal window that becomes a liveness
risk for every honest miner. What it defeats (within-round copying and slow
lookup/relay pipelines) is already blunted by the per-agent post-commit seed --
two agents pinned at the same block get distinct datasets, so there is no shared
answer to copy -- and by the screener behavioral oracle. Revisit if
cross-miner answer correlation shows up in the live ledger.

Defeats within-round answer copying and slow lookup/relay pipelines.

- The validator derives the run seed from the post-commit block hash (already the
  case). Timelock-encrypt the question batch to a near-future drand round.
- The miner must publish `H(answers ‖ salt)` within a tight window of decryption,
  then reveal `answers ‖ salt`; a late or mismatched reveal scores 0 for the
  round. The CR3 commit-reveal machinery already exists on-chain — this inverts
  it from weights onto responses.
- Trustless: the commitment and reveal are on-chain and verifiable; the score is
  still computed by the public grader over the revealed transcript.

### 2. Randomized deep-audit with staked slashing (reproduce-under-transform)

**SHIPPED (2026-07-18), in a materially narrower form than specced below.** See
"What shipped, and what it does not do" immediately after this spec. The
original spec is kept for the reasoning; do not read it as a description of
current behaviour.

Defeats precompute and lookup by making cheating negative-EV even though most
items are not deep-checked.

- Block-hash-seed an unpredictable fraction of cases for audit. For each, the
  validator demands the miner reproduce the answer under a **validator-chosen
  semantic transform** (a re-phrasing / a metamorphic sibling / a point-in-time
  shift) in one round-trip, and slashes stake on mismatch.
- The transform and its expected answer are deterministic functions of
  `(seed, case, transform-id)` — reproducible by anyone, so the audit stays
  trustless. This repo already emits **metamorphic twin families** and
  **point-in-time / multi-hop** variants; the audit path re-uses that machinery
  as the transform set.
- Size `audit_probability × slash` so expected cheating value is negative.

#### What shipped, and what it does not do

Implemented in `persona/transform.go`, graded by `dittobench-api`, verdict in
`ditto-subnet`, hold in `ditto-platform`.

Differences from the spec above, all deliberate:

- **No slashing, and no separate round-trip.** The audited cases are ordinary
  graded cases inside the normal scored run, wire-indistinguishable from the
  rest. A failed audit sends the agent to operator review
  (`ATH_PENDING_REVIEW`), excluding it from emissions until resolved. It never
  slashes and never alters the composite. Quarantine-then-review matches the
  existing copy-review policy, and an auto-penalty on this signal would be
  unsafe for the reason in the next paragraph.
- **15% of cases** (`AUDIT_BPS`), selected by
  `H(seed || case_id || "audit-v3") % 10000`, paid for out of the run budget so
  case count and miner cost are unchanged.
- **Two transform classes.** Invariance (rephrasing; answer unchanged) is the
  common case. Covariance (answer changes deterministically) is ~10% of audits
  and covers a prior-state question on an update chain, or a shifted
  point-in-time boundary. Both recompute the expected answer through the
  generator code that produced the base answer, so there is no second answer
  source and a third party regenerates them from the published seed.

**What it defeats:** answers keyed to a question's exact surface form. Template
fingerprint dispatchers, trailing-question strippers, lookups keyed to the
original phrasing, and precompute keyed to the original question. That is the
bulk of the rejected-harness corpus. Covariance additionally prices
memorization: a memorized answer is not merely unmatched, it is wrong.

**What it does NOT defeat:** a robust local solver that recomputes the answer
from the cleartext haystack per question. It recomputes correctly under the
transform too. Making a cleartext-haystack answer model-required is not
achievable in a deterministic score. That residual is the screener behavioral
oracle's job (force model invocation) plus the per-submission fee. The transform
audit and the oracle are complementary: the oracle forces a model on some
traffic, the audit makes memorization and surface-brittle precompute negative-EV,
and the audit cases are indistinguishable from normal cases so a harness cannot
selectively reason only where it is watched.

Do not describe this as closing the general timeline-solver gap. It closes the
memorization/brittleness family and raises the cost of the rest. A low
`transform_robustness` is not evidence of cheating on its own, which is exactly
why the platform holds for review instead of penalizing.

### 3. Version / template-space refresh cadence

**PARTIALLY DONE; the cadence itself is DEFERRED (2026-07-18).** The
`bench_version` bump half is live (v3 is the anti-gaming release, and
`RotateSeed` folds it in). What is deferred is the ongoing category-retirement
machinery: growing composed families over time and retiring the ones `gstudy`
flags as carrying ~0 information at the champion boundary. That needs a
retirement policy and a `gstudy` signal read on a schedule, which is operational
work rather than a code change, and it is the piece that makes refresh
economically meaningful. The v3 rollup grew the composed space in two places
(injection framings, transform audit) without establishing the cadence.

Reseeding alone is worthless against a regex harness (it re-runs instantly). The
economics only work when each refresh changes the **template space**, forcing a
lookup harness to re-engineer the cross-product of families, not just re-run.

- Bump `bench_version` on every scoring-affecting change (folds into
  `RotateSeed`; ledger scores across versions are never compared and the ledger
  is re-scored on a bump). v3 is the anti-gaming release.
- Grow the number of *combinatorially composed* question families over time so
  re-engineering cost exceeds reward, and retire saturated/floor categories that
  `gstudy` flags as carrying ~0 information at the champion boundary.

### Deliberately rejected under the invariants

- **Validator-side LLM paraphrase / LLM-judge**: breaks trustlessness (non-repro)
  and simple-validator (inference in the validator). Replaced by task-side
  synthesis hardening above.
- **Secret holdout / code-in-secret-sandbox scoring**: a secret test set makes
  the executing validator a trusted central party. Replaced by the public,
  reproducible transform-audit (#2).

## Threat-model note on seed secrecy

The result-usage needle, canary nonce, and injection payload are pure functions
of the seed and are safe only while the seed is unknown to the running harness.
The operational guarantee is that the block-hash seed never reaches a scored
harness: it is derived after the miner commits and is not handed to the harness.

An earlier draft suggested a validator-held HMAC key as a fallback if the seed
could leak. That is rejected here: a validator-held secret makes the derivation
reproducible only for key-holders, which breaks the trustless-scoring invariant
(any third party must re-derive the score byte-for-byte). If seed leakage to the
harness ever became a real risk, the correct response is a PUBLIC commitment with
a post-run reveal (commit to a per-run salt, reveal it after scoring), not a
held key. Everything else in v3 resists a harness that holds the full haystack in
cleartext, which is the realistic capability.
