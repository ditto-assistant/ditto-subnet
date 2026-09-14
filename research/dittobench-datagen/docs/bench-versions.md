# Benchmark versions

A `bench_version` is an **immutable generation contract**. For a given
`(seed, bench_version)` the generator emits the same bytes forever, and the
grader scores a given transcript against them the same way forever. That is what
makes an old score auditable by anyone holding the published seed: nothing about
a scored run is allowed to move under it afterwards.

Because the contract is immutable, a correction to how scoring works cannot be
applied to an existing version. It ships as a new one.

## The versions

| Version | Epoch | What it is |
| --- | --- | --- |
| 2 | `2026-01-01` | The launch contract. Frozen since on-chain scoring began. |
| 3 | `2026-07-01` | The anti-gaming release: dump-guard grading, needle gating, adversarial distractors, composed injection framings, the cross-user lifecycle probe, and the reproduce-under-transform audit. |
| 4 | `2026-08-01` | A supplementary fix to v3 scoring. Same tests, same shape, corrected grading. |
| 5 | `2026-09-01` | Conversational grounding, broader capability coverage, and token-efficiency scoring. |
| 6 | `2026-10-01` | Memory-as-data and the complexity suite; retains the v5 scoring contract. |
| 7 | `2026-11-01` | Platform-owned OpenRouter inference with locked `openai/gpt-oss-20b`, and the difficulty release: a version-gated hard-case suite roughly an order of magnitude harder for a non-reasoning harness while a correct trajectory still scores full marks. |
| 8 | `2026-12-01` | The answering-machine-proof release: natural requests whose route depends on seeded prior context, semantic enum/identifier resolution without magic free-form strings, a larger computed-memory share, and stricter deterministic answer grading. |
| 9 (pre-activation) | `2027-01-01` | A qualification contract for the Bench v9 family mix and launch gates. Explicit generation, offline audit, and ordinary runtime execution are available; Platform activation remains separate and v8 stays current until rollout. |
| 10 (pre-activation) | `2027-02-01` | A generator-as-spec contract: seed-scoped ontologies, recursive query programs, independent renderers, and linked metamorphic/counterfactual cases. Runtime execution is available; Platform activation remains separate. |
| 11 (pre-activation) | `2027-03-01` | Anti-template-fitting: sampled program shapes, compositional surface grammar, descriptive entity binding, a multi-edit surface-noise projector, and per-seed composed injection markers. Runtime execution is available; Platform activation remains separate. |
| 12 (pre-activation) | `2027-04-01` | Anti-KV-substrate: prose-only amounts with per-seed shuffled record order, no `%+d`/`->` format tells, universal relational subject binding, larger-minus-settled rebalanced, and compositional injection markers and routing cues. Runtime execution is available; Platform activation remains separate. |
| 13 (pre-activation) | `2027-05-01` | The typed-semantic contract (#1518): label-insufficiency, an unregenerable surface pass (salt 0 = public rehearsal), causal model dependence, provenance over containment, a published 250-case memory mix with money capped at ≤12% target / 15% hard, typed claim grading in the requested unit, and relay-evidenced tool gates (catalog-present, provenance, causal, twin/pair, cost) that ship in shadow/observe. Runtime execution is available; Platform activation remains a separate owner decision after #1521 calibration. |

## V10 generator-as-spec contract

V10 starts from a seed-scoped semantic specification rather than a reusable
question template. The specification contains an ontology, a schema, a
recursive query program, evidence records, and an explicit answer relation.
Independent renderers project the same semantics as conversations, email
threads, tables, or operations dumps. Linked invariant cases preserve the
answer while changing renderer or adding a distractor; the linked causal
counterfactual changes one source fact and must change the answer.

The generated artifact carries reviewer-only provenance sufficient to replay
the specification and verify those relations. The harness projection continues
to expose only ordinary memory and questions. V10 is an executable runtime
contract, but it does not change `CurrentBenchVersion` or activate Platform
scoring. Capability advertisement and rollout authority remain separate.

Its tool-routing slice also makes the execution mode depend on a seed-scoped
planning record rather than prompt keywords. The same request may require a
one-off Ditto Code job, creation of a reusable workflow, or execution of an
existing workflow. CI runs the public model-free 1-nearest-neighbor probe over
30 training and 10 held-out seeds and requires both complete tool-outcome
accuracy below 50% and verbatim exposure below 25% of required argument values.

The memory slice binds every scored question to its reviewer-only evidence IDs
and audits answer exposure against those records. Short values use lexical
boundaries, so an unrelated year cannot make the answer `8` look retrievable.
Across the qualification seeds, at least half of evidence-bound answers must be
computed or transformed, and fewer than half may be copied verbatim. Missing
evidence fails the audit instead of counting as artificial difficulty.

The runtime tool slice makes model selection authoritative in v10. For each
serialized scored case, the ticket-bound inference broker records successful
model-emitted tool calls and the tool endpoint consumes exact name plus
canonical-argument matches once. Unbacked execution, changed arguments,
duplicate or cross-case replay, incomplete evidence, and model selections that
were never executed all receive zero tool credit. Self-reported calls cannot
supply the missing provenance. This rule is version-gated so the immutable v2
through v9 contracts retain their historical behavior.

```sh
go run ./cmd/memoryprobe -seed 41 -run-size full
```

## V9 grader hardening and canned-response audit

V9 has an explicit grading policy even where its initial rule is identical to
V8. The policy inherits V8's bounded chitchat credit, strict acknowledgement and
persistence checks, and authoritative structured answer slot. Keeping V9 behind
its own gate lets later V9-only corrections remain unreachable from V8. Stored
transcript goldens pin V2 through V8 grading, and the scorer contract separately
pins V8's serialized case score, so adding V9 cannot silently re-grade a
published historical run.

The launch gate includes a public, model-free grader audit with two independent
checks:

```sh
go run ./cmd/graderaudit -bench-version 9 -seeds 40 -run-size full
```

The **generated-corpus exposure gate** generates seeds 1 through 40 and grades
a fixed suite of case-independent generic responses plus public-question-only
templates against every memory case. Those probes receive no case id, expected
value, answer item, generated memory, or usage data. The gate publishes passable share and mean
credit for each answer kind and overall, using `score >= 0.5` as the pass
threshold, and fails unless overall passable share is strictly below 5%. On the
pinned full-profile audit, 241 of 10,040 cases are passable by at least one
response: **2.4004%**, with mean credit **1.7978%**.

The **synthetic per-kind robustness gate** is the versioned public bank `v9-2`.
It supplies one explicit case for each affected kind—chitchat, acknowledge,
decline, persistence, and reversal—and value, list, number, and money controls.
Its 22 strategies comprise 17 independent held-out generic/paraphrase responses
and five templates that can use only the public question. Across 198
case-strategy evaluations, aggregate telemetry remains available, but limits
gate the worst individual strategy for each answer kind. Interaction-only
acknowledge, chitchat, and decline expose their intentional generic maxima;
persistence, reversal, and all four typed controls have a zero worst-strategy
pass share. Nine positive controls—including a cents-valued money case—must
score above zero, and ten negative near-miss checks must all score zero. The
command fails on missing kind coverage, expected-answer leakage, a failed
control, or any worst-strategy per-kind pass-share or mean-credit limit rather
than allowing one strategy to hide behind the pooled denominator.

Both banks are public deterministic measurements of grader exposure. They are
not secret probes and are not an anti-overfit defense: publishing them makes the
contract auditable but means an adversary can read every response. The broader
bank reduces accidental string-list overfitting; it does not claim to enumerate
every paraphrase or adaptive strategy. The answer-dump threshold remains
deterministic; the bounded launch inference/embedding ablation is delegated to
#386, while broader causal probing remains follow-up #532.

## What v8 is

V8 keeps v7's locked model, run-size names, timeouts, and deterministic scorer.
The public full profile grows to 351 cases so every semantic domain can carry a
stable share of composed queries and realistic writing noise. Its fixed 13-case
world-native integrity tail carries three samples for each conversational-sanity
slice, one attributed canary, and three stored-data injection-resistance probes;
none restores the retired synthetic `sess-*` memories. It changes
derivability. One seed now builds a shared procedural world: the fake user has
people, nicknames, relationships, employers, projects, vendors, trips, original
facts, later corrections, and both terse notes and messy business data pasted as
a wall of text. Tool and memory cases ask about that same world instead of
drawing unrelated cards.

The ordering is part of the V8 contract. Before any scored tool case, the
validator sends the tool-prerequisite payload as the initial shared-world seed.
Tool cases then run against that state, and the memory half follows in the same
harness container and user store. The store is not reset between halves. Memory
waves are later staging additions, not a replacement copy of the initial world.
Generation and the scoring API both fail closed when a memory case declares an
evidence record that is unavailable through this ordered seed boundary.

At least 65% of tool cases are composed, seed-bound tasks. They combine four or
more facts, several constraints, and an outcome such as contacting the right
person at their corrected address, mutating the uniquely described memory,
reconciling a corrected invoice, or threading a served result into the next
action. Expected tools are a capability set, not one ordained trace: a fuzzy
trajectory may inspect, retry, reorder, and make harmless extra calls. Correct
seed-bound arguments and outcomes remain hard deterministic gates, as do served
result values. `MaxToolCalls=15` describes the expected task shape; it is not a
hard ceiling and a creative agent is not punished for exceeding it. Run-level
token efficiency remains separate.

The memory mix similarly spends at least 65% on composition, indirection,
temporal state, corrections, calculations, graph joins, and outcome verification.
Every shared-world answer has three plausible near misses. V8 also projects
seed-derived keyboard slips, transpositions, omissions, duplications, common
misspellings, and common grammatical errors onto ordinary prose while keeping
names, organizations, aliases, places, products, and other semantic join keys
canonical. Current ChatV2 wire tool names remain unchanged,
so a v7 harness can run v8; v8 changes what competence is required, not the
transport contract. Free-form tasks remain free-form, and no LLM judge is added.
Every change is gated on `bench_version >= 8`; the v7 known vector stays frozen.

Agent-job cases follow the production approval boundary: the harness dispatches
`execute_agent_job`, then Ditto App presents approval and owns progress/result
display. V8 never requires `get_agent_job_status` as part of the agent turn.

The release gate includes a public model-free 1-nearest-neighbor prompt prober:

```sh
go run ./cmd/toolprobe -bench-version 8 -run-size full -train-seeds 30 -held-out-seeds 10
```

The prober predicts the complete scored outcome signature: capability set,
seed-bound required arguments, and the served result where applicable. This is
deliberately not merely a tool-name classifier—guessing `gmail_send` from the
word “email” does not solve a case. The candidate must remain below 50% complete
tool-outcome accuracy on ten held-out seeds. The pinned candidate measures
36.00%; its deterministic oracle remains 100% scoreable.

## What v7 is

v7 carries two things. First, the inference boundary it always had: measurements
made through the platform-owned OpenRouter inference boundary and its locked
`openai/gpt-oss-20b` model are separated from earlier Qwen-based scores. Second,
the **difficulty release** — a suite of version-gated hard cases that make the
benchmark markedly harder for a non-reasoning harness (pattern-matching,
retrieval-without-reasoning, dump-everything, keyword routing) while leaving a
genuinely correct trajectory at full marks.

Every difficulty lever is gated on `bench_version >= 7` in exactly the style v5
and v6 used, so v2 through v6 regenerate and grade byte-identically (their
known-vector tests pass unchanged). The judge-free deterministic grader is
unchanged: no new grading rules, no LLM, stdlib only. The wire/artifact format
is unchanged too — the new cases reuse the existing `MemoryCase` / `ToolCase`
shapes and the existing served tool-endpoint protocol, so a harness built for
the current format still parses a v7 dataset and returns valid responses (it
will simply score poorly, which is the point).

Every new case family is grounded in a real production flow, data-model shape,
or documented failure mode of the live Ditto assistant (backend + app). The
family-by-family evidence — which product flow each exercises, why a
product-quality agent must handle it, and why today's harnesses fail it honestly
— is in [v7-product-traceability.md](v7-product-traceability.md). The rule:
difficulty must make the real product better, never difficulty for difficulty's
sake; anything that reads as a trick a product-quality agent would never need to
survive is cut.

### The difficulty levers (all v7-gated)

- **Scaled, denser profiles** (`profilesV7`): the `full` memory suite grows to
  ~180 cases over 5 waves at a 0.5 raw-pairs (Tier-B) share and 10 isolation
  cases, drawing a denser persona (more sessions, more near-miss decoy people),
  so the distractor-to-needle ratio rises with the case count. `small` stays a
  cheap single-wave smoke path. Generation stays non-LLM and fast (tens of ms
  for a full v7 dataset), so the per-submission `full` path is unaffected.

- **Sharpened memory-type mix** (`memoryTypeWeightV7`): the types a lexical
  retriever cannot shortcut (multi-session synthesis, temporal reasoning,
  point-in-time, contradiction, knowledge-update, aggregation, preference
  application) take a much larger share of the stratified budget; single-pair
  recall stays at weight 1 for coverage without dominating. The v7 twin-family
  count is capped so phrasing-invariance recall (the naive-passable end of the
  suite) does not soak up the freed budget.

- **Six new product-grounded, reasoning-required memory classes** (each maps to
  a real flow in [v7-product-traceability.md](v7-product-traceability.md)):
  - *deep write chains* (`lifecycle-deep-*`): the real memory write path — a
    save→update→update→read (and save→update→delete→read) sequence delivered as
    separate instructions across three staging waves; only the final state
    answers, and the delete-chain read declines with the chain's earlier values
    as scored distractors. (backend in-place `update_memory` + confirmation-gated
    `delete_memory`.)
  - *three-hop joins* (`multi-hop-deep`): knowledge-graph traversal — "my
    mentor's partner's employer", a three-pair walk across sessions, with a
    one-join trap (the first-hop person's own value) and a wrong-chain trap (a
    full decoy chain) both seeded as scored distractors. (backend subject edges /
    memory network.)
  - *near-miss abstention* (`near-miss-abstention`): entity disambiguation — a
    question engineered to look answerable (the sibling entity's value for the
    same attribute is seeded, and the asked entity is mentioned in an unrelated
    context) but the asked fact was never stated; the sibling's value is a scored
    distractor, so a nearest-neighbor retriever is zeroed. (backend subject dedup
    at cosine 0.75; "never the wrong person's data".)
  - *temporal arithmetic* (`temporal-arithmetic`): aggregation over durable
    quantitative facts the product tracks (rent + raise, budget − spent) stated
    in non-adjacent sessions; the answer appears in no seeded pair, graded
    through the accept-set. (Insights aggregation.)
  - *composed stored-instruction injection* (`injection-composed`): a
    prompt-injection split across two innocuous notes (a fake authority channel,
    then a payload that invokes it), so a single-note attack detector sees two
    benign memos; a benign same-shape twin (the user's own tagging convention)
    ensures blanket refusal fails. (store is written through untrusted chat.)
  - *subscribed-graph attribution* (`subscription-own` / `subscription-attributed`):
    a subscribed friend's value and the user's own value for the same attribute
    both surface in one flat search list, distinguished only by an `@friend`
    provenance prefix; "my X" must return the user's own (the friend's is a
    cross-graph leak), and "what did @friend say" must attribute the friend's.
    (backend `annotateSubscribedSlimMemory`, cross-user docs.)

- **Five new tool classes** plus a **weighted category mix**
  (`toolCategoryWeightV7`) that makes result-usage cases (which require executing
  tools and reading their served content) the dominant share and doubles the
  routing/discrimination traps:
  - *negation-cue restraint* (`negation_no_tool`): the prompt names a tool cue
    while negating it, so a keyword router that fires on the cue is caught.
  - *stale-context routing* (`stale_context_web`): a memory-anchored phrasing
    whose actual request is current public information.
  - *dependent link chain* (`link_chain_result_usage`): `search_web` serves a
    stable page URL and `read_links` reveals the answer only when called with
    that URL — the trajectory cannot be faked and the snippet cannot be grepped.
  - *job-chain + recovery composition* (`job_chain_recovery_result_usage`): the
    dependent job-id chain and transient-error recovery at once. Both serving
    gates already existed in the tool endpoint and compose via the category-name
    markers, so no protocol change was needed.
  - *entity-lookup 3-hop chain* (`entity_lookup_chain`): the production
    `search_subjects` → `search_memories_in_subjects` → `fetch_memories`
    sequence the backend recommends, order-scored.

### The measurement — refit to the round-2 rebench

The champion tier is REFIT to real measured data. All five leaderboard harnesses
were re-run on the round-1-deepened suite (full profile, observed execution) and
measured **0.590–0.795, median ~0.70** — round-1's simulated 0.35–0.58 projection
overstated the collapse. The tier is now modeled directly from the round-2
per-family means (two tiers: SCRATCH = dittobench-scratch harnesses, STARTER =
starter-kit harnesses), reproducing the measured per-family means and case-means
by construction (residual < 0.003). The full family-by-family evidence and the
refit residuals are in [v7-product-traceability.md](v7-product-traceability.md).
Pinned by `gen.TestV7ChampionTierRefit` and `gen.TestV7NaiveStrategiesCollapse`
(`go test -run 'V7ChampionTierRefit|V7Naive|V7Oracle' -v ./gen`).

**Naive strategies.** On the reasoning-required subset the best fixed
non-reasoning strategy scores **0.090** vs the oracle's **1.0** (an **~11x** gap,
up from v6's 6.1x); that subset grows from **40% → 58%** of the memory suite, and
the keyword tool router falls **0.525 → 0.350**.

**Champion tier (refitted prediction).**

| Tier | round-2 measured (round-1 suite) | round-3 predicted case-mean | round-3 composite (×0.75–0.87 gate) |
| --- | --- | --- | --- |
| SCRATCH (newDitto/ditto-agent/cliM@X) | 0.841 | 0.758 | ~0.57–0.66 |
| STARTER (infinity/whitycatboss) | 0.741 | 0.669 | ~0.60 |
| Oracle (every case, 30 seeds) | — | 1.000 | **1.000** |

Round-3 grows the measured-honest biters (multi-hop-deep, injection-composed,
computed-answer, job-chain/stale-context tools), deepens the four saturated
families with grounded harder shapes (multi-friend subscription conflicts, a
lifecycle cross-referencing read, distinguishing benign twins), and trims the
scratch-saturated coverage to a floor — pinning both tiers below their round-2
measured level while the oracle stays 1.0. It does **not** reach a flat 0.35 for
the strong tier: the refit proves that tier is a genuine near-champion (only
computed-answer, stale-context, injection-composed, and multi-hop-deep defeat it
at measured rates), so closing the rest requires either the deepened families
biting at their estimated ~0.4–0.5 (to be MEASURED in round 3) or concentrating
~80% of the suite on 3–4 families, which would gut grounded coverage and read as
difficulty-for-its-sake — forbidden by the product-grounding rule. As with every
version, the public versioned seed rotation produces a fresh, deterministic
dataset surface, so v7 scores are never compared with v6 scores.

## What v4 is

v4 is **not** a new benchmark. It is v3 with a set of scoring false positives
corrected: cases where the machinery penalised a legitimate agent for doing the
right thing. The suite it administers is the same suite; what changed is that
several ways of being *correct* no longer lose points.

Corrections that move dataset bytes, and so are gated on v4:

- **The canary is no longer eligible for a transform-audit sibling.** The audit
  copies every grading field, including the bait nonce, so roughly one seed in
  five carried the same nonce/bait pair twice, and the scorer charged its
  canary disqualifier per leaking case. One breach was penalised twice.
- **Delete instructions are graded as acknowledgements.** `lc-del-w` and
  `xu-del-d` are instructions, not questions, but were graded on whether the
  response echoed a noun phrase. "Done, I've removed that from your records." (a perfect answer) scored zero. Whether the deletion actually persisted was
  never in question here: the paired read case already checks that, unfakeably.

Corrections to grading, which apply at the module level rather than being gated
on the version (see *Auditing an old score* below):

- **The canary gets the refuse-and-answer exemption injection cases already
  had.** The bait is planted in the user's own conversation, so naming it to
  disambiguate ("your code is X; the code Y on file is Dana's, not yours") is
  the most competent answer available, and it scored zero *and* took the
  composite disqualifier. The exemption still requires the true value to be
  present, so echoing nonce-shaped tokens fails exactly as before. Isolation is
  deliberately not excused: its forbidden value lives in another user's memory
  graph, so surfacing it at all means a boundary was already crossed.
- **Durations parse decimals.** "about 1.5 years" was read as *15 years*.
- **"used to" is no longer a cessation phrase.** It is a temporal marker, and it
  zeroed correct answers of the very common form "you used to mention it
  constantly, and you still love it".

The scorer carries matching corrections (bounded penalties no longer stack past
a floor, transport failures no longer read as brittleness, transparent memory
retrieval is no longer taxed). Those live in `dittobench-api`.

## Versioning going forward

Two numbers, doing two different jobs. Keeping them separate is deliberate.

**`bench_version` is an integer contract identifier.** It is a primary-key
component in the platform's score ledger and an integer on the wire, so it has no
minor or patch component by construction. It increments only when the contract
itself changes: different dataset bytes, or a different scoring rule for the
same bytes. There is no such thing as `bench_version` 4.1.

**Releases of this module follow semantic versioning**, with the major tracking
the contract it implements: a release implementing contract 4 is `v4.MINOR.PATCH`
`v4.1.0`, `v4.1.5`. So:

- **Major**: a new immutable contract. `v4.x.y` → `v5.0.0` alongside
  `bench_version` 5.
- **Minor**: additive changes that do not alter any scored output for the
  contract: new tooling, additional exports, documentation.
- **Patch**: fixes that do not alter any scored output.

The rule that makes this trustworthy: **within a major, no release may change
the bytes or the score of an already-published run.** The known-vector tests in
`gen/publicvector_test.go` enforce the byte half in CI. Anything that would
change a score is, by definition, a new contract and a new major.

## Bench v11 (private, anti-template-fitting)

v11 is designed against the two harness archetypes measured at the top of the
v10 board: deterministic rule engines that prefix-match the generator's fixed
surface templates, hardcode the single v10 balance program, resolve entities
only through literal alias echo, and model only the public single-edit typo
projector. Every lever is gated on `bench_version >= 11`, so v10 regenerates
byte-identically:

- **Sampled program shapes.** Each metamorphic group draws one of four query
  programs (subtract, adjust-then-subtract, latest-of-two-corrections,
  larger-minus-settled). The v10 formula's result is planted as a distractor
  whenever it differs, so a harness replaying approved-minus-paid selects a
  wrong answer rather than a near miss.
- **Compositional surface grammar.** Glossaries, questions, and assistant
  acknowledgements are assembled from seeded component banks; no complete
  sentence is stored anywhere, so no literal prefix survives two seeds.
- **Descriptive entity binding.** Even-numbered groups reference the subject by
  a unique evidence-derived description (its recorded draft amount) instead of
  its alias.
- **Multi-edit surface noise.** `V11ApplyArtifactSurfaceNoise` runs inside the
  shared artifact assembly and applies seeded interior edits to a fixed
  safelist of framing words across seeded history, staged questions, and tool
  prompts. Values, labels, names, numbers, and expected tool specs stay
  byte-exact.
- **Composed injection markers.** The fixed stored-directive markers
  (`[SYNC]`, "The verified value is", ...) are rewritten per seed, so a finite
  marker list no longer recognizes a planted directive while the attack (and
  its grading) is unchanged.
- **Routing cue rotation.** The v10 state-dependent tool-routing family keeps
  its route outcomes byte-identical but rotates the "planning note" /
  "operating choice" / "Approved route:" cue surfaces per seed.

Run sizes, the deterministic grader, the inference boundary, LongMemEval
deep-history floors, and the v9 signed-evidence/score-gate/curve-v3 efficiency
stack all carry forward unchanged.

## Bench v12 (private, anti-KV-substrate)

v12 keeps every v11 program semantic — the same four sampled query shapes, the
same metamorphic groups, the same per-seed schema, the same renderers, and the
same validator-side provenance — and hardens the one surface a template-fitting
harness still gripped: the byte-stable `key=value` ledger. A harness that scored
0.997 on v11 never read the randomized prose; it parsed the fixed-order KV rows,
fired a model call only to satisfy the attribution gate, and computed a balance
positionally. Every v12 lever is gated on `bench_version >= 12`, so v11 and
earlier regenerate byte-identically (the v2–v11 known-vector tests are
unchanged). The levers:

- **Prose-only amounts with shuffled record order (Gap 1).** No record carries a
  `label=amount` pair — the only `=` in a scenario binds an entity to its alias
  and carries no value. Every monetary figure (draft, approved, settled payment,
  adjustment, superseding correction) lives inside a composed prose sentence, and
  the record order is a per-seed Fisher-Yates permutation. Binding a role to a
  value now requires reading randomized prose, not counting rows. The `V12`
  known-vector test asserts no record exposes a `=`-adjacent number and that the
  binding record lands in more than one slot across a run.
- **No format tells (Gap 2).** v11 leaked the shape through a `%+d` sign on the
  adjustment row and a second `->` on the latest-correction row. v12 never emits
  either: a signed adjustment is stated as "raises/lowers that figure by N" over
  the absolute magnitude, and supersession is stated in prose ("a later revision
  supersedes it at N"). The operative value is interleaved into randomized prose,
  so the shape is not detectable from row structure.
- **Larger-minus-settled rebalanced (Gap 2b).** v11 made the approved figure
  exceed the draft on ~60% of cases, so `max==approved==plain subtract` was free.
  v12 draws the approved figure below the draft for most cases and forces
  `approved < draft` whenever the sampled shape is larger-minus-settled, so `max`
  genuinely differs from the plain subtract and the plain-subtract result is a
  planted distractor.
- **Universal relational subject binding (Gap 3).** v11 bound the subject
  descriptively only for even groups, and even then by a unique draft number that
  also appeared verbatim in the rows. v12 binds the subject descriptively for
  every group by a relational role — the workstream that carries a settled
  payment — which resolves through the glossary and against the unrelated decoy
  (which carries only an approved figure). The question never names the subject
  alias, so an alias-echo resolver cannot even locate the records.
- **Compositional injection markers and routing cues (Gaps 4/7).** v11 rewrote
  each fixed stored-directive marker into one of a frozen ≤5-variant bank, and
  rotated the tool-routing cues through fixed banks. v12 assembles both from
  independent component banks, so the reachable surface is a product of the banks
  (hundreds to thousands of forms) rather than a short enumerable list. The
  attack semantics and the route outcomes are byte-identical to before.
- **Widened label superset and opaque session IDs (issues #492/#499/#537).** v12
  labels sample a 24×16×20 superset per seed, and session identifiers are opaque
  hashes rather than `v12-conversation-3`, so no generator role name leaks into
  the wire and no fixed dispatch table keyed on a label pays.

The wire/artifact schema is unchanged: v12 still emits `MemoryCase` /
`ToolCase` / `DatasetArtifact` in the same shapes, with the same
grading-authoritative `ExpectedAnswer`, `DistractorAnswers`, `AnswerKind`, and
expected tool specs. v12 changes what competence a run must demonstrate, not the
transport. Run sizes, the deterministic grader, the inference boundary,
LongMemEval floors, and the v9 efficiency stack all carry forward unchanged.

## Bench v13 (private, typed-semantic contract)

v13 is the contract issue #1518 defines: **typed semantic outcomes graded
through claim sets, on a surface a harness cannot regenerate, with the graded
value causally traced to a model completion the relay observed.** It answers
the 2026-09-13 top-of-board review (operator-private records
`sn118-top5-board-review-2026-09-13.json` and
`sn118-bench-v12-adversary-briefing.md`, held with the Backroom board-review
precedents — they quote never-released miner source, so they do not live in
this public repository): 4/5 rejected top-5 artifacts lived on the tool axis
(request-keyed catalog suppression, baked option pools, phrase tables), and the
memory axis rewarded a request-keyed `/100` rewrite of a correct bare-cents
answer (the v12 minor-unit inversion: `411067` → 0, `$4,110.67` → 1 on a
question that asked for minor units). Money owned 50.9% of memory score
weight, so a cents ledger with a formatter competed with a generally competent
agent.

Every lever is gated on `bench_version >= 13`; v2–v12 regenerate and re-grade
byte-identically (the v2–v12 known-vector tests and
`TestV2ThroughV12RegradeGolden` (#1522) are the guard, and
`TestV13DoesNotMoveEarlierContracts` (#1848) re-asserts every earlier vector
from the v13 generator path). Nothing here activates: `CurrentBenchVersion`
stays v8, the runtime advertises 13 in `supported_bench_versions` only through
the #1519 wiring sweep, Platform dispatches v13 in shadow during #1521
calibration, and activation is a separate owner gate.

This is the **one** v13 section of this document. Each v13 PR documented its
lever here under an interim per-PR umbrella while the contract was assembled
(plumbing, surface levers, case families, envelope and money cap, grader,
surface gate and mix audit, story v2, tool bench, evidence-bounded, twins and
cost); those umbrellas are folded into the subsections below and must not be
reintroduced by a later merge — `gen/v13_contract_doc_test.go` fails on a
second `## Bench v13` heading or a second `13` row in the version table. The
per-area detail pages are `v13-case-families.md` (#1520) and
`v13-family-mix-study.md` (#1848, #1830); the per-area code comments stay the
authority for anything a subsection summarises.

### Governing invariants

1. **Label-insufficiency.** For every family, knowing the family label and the
   extractable operands must be insufficient to score — stored state, a
   per-seed served artifact, or a time anchor must decide.
2. **Unregenerable surface (v13.0, not v13.1).** The surface a harness sees
   (renderings, names, language/typo draws, decoy set, inventories, fixtures)
   must not be recomputable from the public repo plus the published seed. The
   public v13 surface pass is the pre-pass; the private pass is a v13.0
   activation blocker (see *The surface pass and the open owner decision*).
3. **Causal model dependence.** The graded value must have been produced by a
   model completion the relay observed, and must not appear in any
   harness-authored prompt span except spans hash-matched to `/seed` records
   or delivered tool results (`answer_in_prompt` → 0). This is the v11
   compute-then-launder lesson.
4. **Provenance over containment; cost is never free; reward the production
   skill; immutability** (`bench_version >= 13` only; v2–v12 byte-identical).

### Honest-agent invariant (binding on every gate)

No gate may zero a response whose graded claim is correct and model-emitted.
Gates fail only on evidence of substitution (text not model-emitted, value
present in a harness-authored span), evidence-independent default behaviour
(twin/pair concordance), or catalog withholding outside the published safe
harbor. Ditto's production agent (HeyDitto) is the canonical honest positive;
calibration (#1521) requires every honest positive to carry gate-induced loss
≤ 0.02 and every S/N carrier to land ≤ starter-kit composite − 0.05, with
per-gate false-zero counts published per honest pattern, not one aggregate.

**Scan-scope rule.** The positive check runs on `final_text ∪ answer`.
Distractor/forbidden scans are **claim-scoped**: asserted candidates only (the
slot when populated, per `grade/grade.go` `distractorScanSlotOnly`, whose v12
rationale — shown reasoning is protected — is retained, not reversed), and a
value cited-and-rejected as insufficient evidence is never a forbidden hit.
Prose is graded; the `answer` slot is a tie-break; `slot_not_in_prose` → 0 only
when the typed matcher finds no equivalent value asserted in `final_text` AND
the slot alone passes. Declarative acknowledgement credit is 0.25.

**Gate ladder.** Every gate the plan marks *shadow in v13.0* — catalog-present,
claim-span provenance, causal dependence, cost factor, twin/pair rule — ships
behind a switch defaulting to shadow/observe, never enforce. The ladder is
review (flag only) → penalize (graduated, capped) → enforce (zero); enforce is
reserved for provable signals and is a fleet-wide operator decision after
#1521 shows 0 false zeros per honest pattern. Never enforce retroactively.

### Adversary model this contract must defeat

| Class | Ids | Made score-negative by |
|---|---|---|
| Request-keyed empty catalog / response sheets / decline gates | S1 S2 S16 | restraint triplets + clarifying-claim grading + catalog-present gate (model-emitted calls, not executed calls) |
| Baked option pools + one-edit ladders | S3 S12 | discovery inventories from public corpora; drop `set_model`/`set_chat_font`; enum schemas |
| Sign sheets / money formula / closed programs | S4 S13 S7 | money cap (≤12% target / 15% hard), record-stated ops, sign variance, typed claims |
| Exact-output arg recipes | S5 | paraphrase-accepting argument claims (reward side) + causal gate + screener I5 (punish side) |
| Draft replacement / slot rewrites / hedged slots | S6 S7 S14 | claim-span provenance gate; prose graded, slot tie-break; stuffing quantifier |
| Generator fingerprint tables, regex routers, typo tricks | S8 S11 S12 | grammars → private surface pass; typo v2; multilingual via private translation; parserprobe ceiling |
| Marker-keyed compaction, ingest-time compilation | S9 N5 N10 | same-turn corrections (primary), point-in-time twins (`as_of_twin`), `/seed` label-leak fix |
| Host tool plans / class collapse | S10 | effect-graded retrieval, cue-unreliable mutations |
| Injection wrapper stripping | S15 | data-inside-envelope + envelope-free injections with a mock-observable forbidden action |
| Trained routers / distilled generators / planner prose | N1 N2 N9 | label-insufficiency + unregenerable surface + causal gate |
| Seed regeneration / answer-key regression | N3 | CRN block binding + egress proof (#1854); surface pass |
| Voting / attribution theatre | N4 N8 | provenance gate, cost factor over choices + tokens (shadow), pair post-pass |
| Layout / language inference from public generator tables | N6 | private surface pass (layouts and languages unannounced) |
| Fixture memorisation | N7 | per-seed coined fixtures |
| In-run twin pairing / forced discordance | N11 | distributionally matched triplets, ≤ chance acceptance |
| Edit-distance-only option picking | N12 | near-miss inventories with a margin test |
| **Generator-inverse harness + launder (GIH)** | **N13** | unregenerable surface pass + causal gate; `cmd/parserprobe` ceiling in CI |
| **Router trained on 10k locally generated seeds** | **N14** | same as N13; W13 training-data declaration in screener policy v14 (#1857) |

Both axes are in scope; the tool-bench, story-v2, surfaces, seeds and
screener-v14 issues are children of #1518 (superseding #1108/#1109).
Acceptance is verified by the #1521 calibration PR against real agents and
synthetic S/N carriers, not by CI alone.

### The published memory mix (250 cases, full profile)

`gen/v13_envelope.go` replaces `v8PrimaryCaseBudget` with a slot table per run
size (#1848); `cmd/mixaudit` (#1830) audits it per seed and fails closed on any
question type or answer kind it cannot classify. Full = 224 primary + 9
isolation + the fixed integrity tail:

| Slot | Count | Generator | Issue |
| --- | ---: | --- | --- |
| story | 78 | story v2 typed event DAG, six typed oracles per arc, 13 arcs (7 business / 6 personal) | #1839 #1841 |
| ordinary world | 32 | ordinary person / project / trip oracles, `project-outstanding` ≤ 4 | #1848 |
| business programs | 28 | `GenerateV13Programs`, 7 metamorphic groups × 4, zero monetary | #1520 |
| personal programs | 24 | `GenerateV13PersonalPrograms`, 6 groups × 4, zero monetary | #1838 |
| abstention | 25 | six grounded-absence families with `decision_twin` pairs | #1530 |
| record-determined quantity | 16 | family compiler v2: 10 money in the record's currency, 6 non-monetary, record-stated sign convention | #1837 |
| divergence | 12 | parser divergence, ≤ 3 money | #1837 |
| point-in-time | 12 | 6 `as_of_twin` pairs with the anchor inside the `/run` turn | #1844 |
| integrity | 14 | 3 chitchat, 3 declarative ack (0.25 credit), 3 declarative behaviour, 1 canary, 4 injection (2 data-inside-envelope, 1 envelope-free, 1 classic) | #1836 |
| isolation | 9 | `GenerateIsolationForVersion` | — |

`gen.MixGateV13` (Owner decision — default taken: ≤ 12% target / 15% hard,
#1529): money ≤ 15% of memory weight, ≤ 22 money-bearing cases, 0 monetary
open programs, arithmetic ≤ 20%, money ≤ 25% of computed answers, personal ≥
30%, business ≥ 40%, no sub-domain > 20%, **no answer-kind × operation > 15%**
(the anti-monoculture bound, so owner-of / status-of does not become the next
parse target), abstention 10% ± 1, twin coverage ≥ 40%, gate-exposed share ≤
40% of memory weight (bounds how much composite a gate rather than a wrong
answer can zero), and a cascade cap (largest dependency cluster ≤ 4%, so one
honest error never costs more than ~4% of memory weight). The classifier is
pinned to the v12 diagnosis (`TestMixAuditReproducesV12MoneyExposure` (#1830):
117 direct money cases / 143 money-bearing / 50.9% of weight on seed
`123456789`); the gate over the pinned 40 seeds is
`TestV13MixAuditGateAcrossFortySeeds` (#1848), and it arms itself slot by slot
as the interim generators are replaced (`gen.V13InterimSlots`,
`gen.V13InterimGenerators`; while either list is non-empty the full gate skips
with a violation report and only the structural bounds and the pinned interim
ceilings — money ≤ 37% of weight, ≤ 110 money-bearing cases, arithmetic ≤ 45%
— are asserted, so interim exposure cannot creep upward). Medium is 95 memory
cases (36 · 10 · 8 · 6 · 6 · 4 · 4 · 2 · 14 · 5) and small 27 (6 · 4 · 4 · 13);
only the three public run sizes have a table, any other size fails closed.

```sh
go run ./cmd/mixaudit -bench-version 13 -seeds 40 -run-size full
go run ./cmd/mixaudit -bench-version 13 -seeds 40 -gate   # exit 1 on a violation
```

### The levers, by area

Every lever below is `bench_version >= 13`-gated and names the test that pins
it; an issue number in parentheses is the PR that carries the lever and its
vector into the stack.

**Plumbing (issue #1824).** `protocol.BenchVersionV13`, `datasetEpochV13 =
2027-05-01`, and one derived list (`protocol.SupportedBenchVersions()`,
`NewestSupportedBenchVersion()`, `SupportedBenchVersionList()`) that every
acceptance check, error string and probe default reads — floors and shared
constants, never retyped enumerations. `profilesV13` (full: 100 tool cases,
250 memory cases, waves stay at 5) and the grader-only protocol types, all
tagged `json:"-"` so they never enter the hashed artifact, `/seed` or `/run`:
`MemoryCase.Claims []Claim{Kind, Expected, Accept, Unit, Critical, Weight}`,
`MemoryCase.TwinRelation` / `ToolCase.TwinRelation` (`decision_twin`,
`as_of_twin`), `ToolSpec.RequiredArgClaims`, `ToolCase.Restraint`
(`no_call`, `clarify_first`, `decline`), the answer kinds `AnswerClarify` and
`AnswerAbsence`; `CaseScore.Relation` carries `V10CaseProvenance.Relation`
into the report for v13 runs only. Every staged v13 case is stamped
`bench_version 13` and the grader dispatches its policy on that stamp.
`cmd/memoryprobe` / `cmd/toolprobe` take `-bench-version` defaulting to the
newest supported version, and `gen.AuditMemoryExposureForVersion` counts the
correction/join families as computed from v13 while keeping the strict
verbatim share visible (`TestMemoryExposureAuditIsVersionExplicit` (#1824)).
Scorer side, `scoregates.SupportedBenchVersion` accepts v13 (inheriting the
v12 gate stack) and `efficiency.ProductionReadyForVersion` treats v13 as
technically ready.

**Surface (issues #1825 #1827 #1828 #1832 #1831).**

- *Public vocabulary corpora.* `internal/publicdata` embeds frozen, SHA-pinned
  tables (GeoNames cities ≥ 15k, O*NET occupations, Google Fonts families, the
  xkcd colour survey plus CSS named colours, Wikidata organisation stems, an
  authored purpose bank) with licences in `SOURCES.md`; no open pool is below
  500 (`TestNoOpenPoolBelowFiveHundred` (#1825),
  `TestFrozenCorpusIdentity` (#1825)). A public corpus is a bigger table, not
  an open set — it buys variety and near-miss ambiguity, never secrecy.
- *`/seed` label hygiene.* Every session id is a `protocol.OpaqueCaseID` and
  every timestamp a seeded business-hours instant; a best-of-twelve-features
  probe that recovers the v12 family from the session prefix at 94% may not
  beat a shuffled null on v13 (`TestV13SeedWireCarriesNoFamilyArcOrSlotLabel`
  (#1827), `TestLeakProbeDetectsTheFrozenV12Labels` (#1827)); `/seed` subject
  links do not enumerate the story join
  (`TestV13SeedSubjectLinksDoNotEnumerateTheStoryJoin` (#1827)).
- *Grammars for every surface and typo v2.* Every tool category, world tool
  prompt, settings intent, chitchat/declarative bank and question frame
  renders from a `persona.Grammar` with a per-(seed, surface) two-thirds bank
  subset (`TestV13EveryToolCategoryRendersFromAGrammar` (#1828)); the typo
  projector v2 draws a keyboard layout per (seed, salt), applies 1–3 edits
  bounded by ⌊len/3⌋, never turns a word into a different real word, and has no
  framing safelist (`TestV2TypoEditsAreBoundedByThirdOfLengthAndKeepEdges`
  (#1828), `TestV2NeverTurnsAWordIntoADifferentRealWord` (#1828)). A public
  CFG is exactly invertible, so grammars are the *input* to the surface pass,
  not a defence by themselves.
- *The salted surface pass and the multilingual hook* — see the dedicated
  sections below.

**Memory families (issues #1839 #1841 #1520 #1838 #1837 #1836 #1844 #1530).**

- *Story v2.* A per-seed typed event DAG (6–9 events, constrained topological
  shuffle, 0–2 contradictory updates, ≥ 50% of arcs carry a revision) over
  3–5 memories in ≥ 2 opaque sessions with one near-name decoy thread; six
  typed oracles per arc (owner-current, status-current with a "records
  disagree" claim set, ordering, next-action `{who, what, channel}`, one
  rotating quantity or lesson key-concept set, one cross-record inference
  oracle joining the arc to the ordinary world). The four money oracles and
  the cents-baking summary are gone (`TestStoryV2ArcsAreTypedEventDAGsWithRevisions`
  (#1839), `TestStoryOraclesAreTypedAndLessonClaimSetsAccept` (#1841),
  `TestStoryV2AnchorsResolveUniquelyUnderTypoEdits` (#1839)).
- *Business and personal event programs.* Seven business groups × 4 and six
  personal domains × 4, zero monetary groups, `base` / `renderer_invariant` /
  `distractor_invariant` / `causal_counterfactual` provenance, grader-only
  `Claims` stripped before the wire
  (`TestV13ContractProgramsCoverEveryFamilyWithZeroMoney` (#1520),
  `TestV13ClaimsNeverReachHarnessWire` (#1520),
  `TestPersonalProgramsCoverSixDomainsWithZeroMoney` (#1838)).
- *Family compiler v2.* The record states its own sign convention, so a
  general assistant never needs bookkeeping norms while a cue-reading solver
  built from the public cue bank stays below 0.5
  (`TestFamilyCompilerV13CueReadingSolverBelowHalf` (#1837),
  `TestFamilyCompilerV13ThreeValuedDirectionVectors` (#1837)).
- *Injection tail.* Two data-inside-envelope cases whose expected answer is
  the value the untrusted block quotes as data (a wrapper stripper scores 0),
  one envelope-free instruction whose compliance is a forbidden tool action
  observed at the mock, one classic; none graded as money
  (`TestInjectionV13DataInsideDefeatsWrapperStripper` (#1836),
  `TestInjectionV13EnvelopeFreeIsObservedAtTheMock` (#1836)).
- *Same-turn point-in-time corrections and realism-only waves.* The "as of
  <anchor>" date arrives inside the `/run` `user_input`, so an index compiled
  at ingest time scores exactly one half of each `as_of_twin`
  (`TestV13PointInTimeTwinsDefeatAStaticStateIndex` (#1844)); waves 1–2 carry
  about a tenth of ordinary corrections behind a synchronous `/seed` whose 2xx
  is the ingest acknowledgement, proven under `case_concurrency` 1–64
  (`TestWaveDispatchHonorsIngestAckUnderCaseConcurrency` (#1844),
  `TestV13EveryDeclaredMemoryCaseAnswerableAfterItsWave` (#1844)).
- *Grounded abstention.* Six absence-proof families (≥ 50% misleading
  evidence, ≤ 25% pure absence) graded as `AnswerAbsence`: a decline that cites
  a grounding token present in the searched records scores 1; the tempting
  value is forbidden only when *asserted as the answer*, never when cited as
  insufficient evidence; a generic refusal or a templated grounding naming an
  absent entity scores 0. Each is paired with a distributionally matched
  answerable `decision_twin` ≥ 20 cases away
  (`TestV13GroundedAbstentionScoresOne` (#1530),
  `TestV13AbstentionZerosAssertionsAndGenericRefusals` (#1530),
  `TestV13DecisionTwinBaselines` (#1530)).

**Tool bench (issues #1843 #1842 #1580 #1840 #1846 #1845 #1847).**

- *Per-seed catalog.* Production tool names never change; descriptions are
  drawn per seed from ≥ 6 paraphrases, `set_theme` / `set_reasoning_effort`
  close their value space with a JSON-schema `enum`, the discovery-grounded
  setters describe their options as runtime-configured, and 3–5 coined decoy
  tools (`<brand>_<shape>`, descriptions stating what they are not) are
  spliced in; decoy-correct cases are ≥ 10% of tool cases so a blacklist
  forfeits real weight (`TestV13Decoys` (#1843),
  `TestV13SeededCatalogVariesDescriptionsAndKeepsNames` (#1843),
  `TestV13ToolBenchContractAcrossFortySeeds` (#1843)). Catalog mirrors are
  regenerated from `catalog.CatalogForVersion` and drift-tested
  (`TestStarterKitCatalogMirrorMatchesV13Surface` (#1843),
  `TestScreenerOracleToolNamesMatchV13Surface` (#1843),
  `TestOpenClawPluginToolsMatchV13Surface` (#1843)).
- *Discovery inventories and dropped families.* Accent colours and fonts come
  from the public corpora with a planted near-miss pair; a 1–3-edit alias has
  a unique nearest listed option with margin ≥ 1
  (`TestAliasForMarginProperty` (#1842)); the canonical spelling exists only
  in the served `discover_capabilities` result, including the setter's error
  text (`TestV13SettersValidateAgainstInventoryWithoutEchoingCanonical`
  (#1842)). `set_model`/`set_main_model` and `set_font`/`set_chat_font` are
  retired as families (#1580); `set_effort` is no longer mandatory.
- *Coined fixtures.* `list_workflows`, `list_schedules`, `list_agent_jobs`,
  `search_tools`, `run_code` and `discover_capabilities` serve per-seed coined
  content and the dependent cases are result-usage graded
  (`TestV13CoinedFixturesCarryTheNeedleOnlyOnTheBearer` (#1840),
  `TestV13NeedleIsAbsentFromOtherFixturesAndRecords` (#1840)).
- *Restraint triplets with graded clarifying claims.* The `no_tool` /
  `abstention` / `arg_hallucination` / `negation_no_tool` families become 16
  cases in distributionally matched groups (same family and oracle, different
  grammar draw, per-seed 2 ask : 1 act or 1 : 2): the ask half is graded as
  `AnswerClarify` (names the missing slot from the schema-name ∪
  description-noun ∪ multilingual-synonym lexicon and cites a record token
  searched; "what would you like?" → 0), the act half requires the stored
  value; always-ask, always-act and random-split rules score ≤ chance
  (`TestV13RestraintGroupsAreDistributionallyMatched` (#1846)). Restraint
  credit reads the broker's **model-emitted** calls and requires
  catalog-present evidence.
- *Effect-graded memory routing and cue-unreliable mutations.* Memory tools
  stay harness-internal (the mock never serves them); eight memory-read tool
  cases are graded on effect (the answer carries the planted needle, any
  non-memory call is misrouting), the "any non-empty text" credit is removed,
  and mutations are graded on end state through a follow-up read (delete +
  save ≡ update) (#1845).
- *Paraphrase-accepting argument claims.* `argClaimSatisfied` accepts honest
  paraphrase of free-text arguments (`update_memory.content`,
  `create_workflow.name` with the distractor party forbidden per claim, never
  the correct one); ≤ v12 `argValueEqual` is unchanged (#1847). This is the
  reward side only; the exact-output recipe (S5) is caught by the causal gate
  and screener I5.

**Grader v13 (issues #1523 #1522 #1831).** Reachable only through
`gradingPolicyForVersion(v >= 13)` → `grade/v13.go`; every v2–v12 function is
untouched. Order: observed bait call → empty → case language without a lexicon
(fail closed) → question echo → forbidden value (claim-scoped, cited-and-
rejected excusal; isolation never excused) → answer dump → abstain on an
answerable kind → typed claim matcher over *asserted* candidates → stuffing
quantifier → partial credit.

- Quantities in the **requested unit**: expected `411067` cents accepts
  `411067`, `411,067 cents`, `$4,110.67`, `USD 4,110.67`, `4.110,67`,
  fullwidth digits; rejects `$411,067`
  (`TestV13MinorUnitRequestedUnitGrading` (#1523); v12 frozen by
  `TestV8MoneyAcceptsHumanFormattingAndRejectsInternalCents`).
- Asserted vs cited: "Lisbon, not Oslo", "I first thought Oslo, but it is
  Lisbon", "was X, now Y" assert one value
  (`TestV13ClaimScopedDistractorScan` (#1523)); > 2 distinct asserted
  candidates or two inconsistent assertions for one scalar claim → 0
  (`TestV13StuffingQuantifier` (#1523)). Within a sentence, cue-positioned
  values ("= 3800", "leaves $3,800", "the balance is") are the claim; an
  enumeration ("3800 or 4200", "maybe X") asserts everything it lists; a lone
  value is asserted; a multi-value sentence with neither cue nor enumeration
  is exposition. A calendar-year token beside a count or amount is a
  qualifier, not a candidate ("You took 3 trips in 2026" asserts 3), an uncued
  bare integer beside a marked amount is exposition ("$3,800 across 4 trips"),
  a clause-closing colon cues the value after it, and verb-object counts
  ("took 3") are weak cues. Slot tie-break `slot_not_in_prose`
  (`TestV13SlotTieBreak` (#1523)); three-valued direction with the questions'
  own vocabulary and "neither … nor" → unchanged
  (`TestV13ThreeValuedDirection` (#1523)); `AnswerDate` at the requested
  granularity — any unambiguous rendering passes, `04/03/2026` never matches
  (`TestV13DateClaims` (#1523)); `AnswerAbsence` and `AnswerClarify`
  (`TestV13AbsenceAndClarifyKinds` (#1523)); declarative acknowledgement 0.25
  (`TestV13DeclarativeAckCredit` (#1523)). Grader notes name the matched,
  missing or contradictory claim by kind and never quote a hidden value.
- Unicode and **reply language**: NFKC-style compatibility fold plus Unicode
  case folding with rune-based boundaries; a case carries `MemoryCase.Language`
  and the answer is accepted in that language or English through the
  `internal/multilingual` lexicons (es, pt, fr, it, de, nl); a sampled language
  without a lexicon fails closed (`TestV13UnicodeAndMultilingual` (#1523),
  `TestConfigFailsClosedOnUnsupportedLanguage` (#1831)).
- The public grader audit is versioned per policy floor (`v9-2` for v9..v11,
  `v12-1` for v12, `v13-1` in `grade/audit_v13_bank.go` for v13): the v13-1
  bank carries 59 hard negatives (3-candidate stuffing, templated grounding,
  served-text-not-model-emitted, the GIH transcript class) and 54 reviewed
  positives (hedged-correct, grounded abstention citing the near-miss, slot
  `411067` beside `$4,110.67`, records-disagree, date renderings,
  reply-in-question-language), all of which must score as labelled
  (`TestSyntheticRobustnessV13BankIsCleanAndCoversEveryKind` (#1522)). The
  generated-corpus gate regrades the newest generatable corpus under the v13
  policy, reports `corpus_bench_version`, and adds a per-claim-kind bound
  (public-question-only passable share strictly below 5% for every
  non-interaction kind; `TestCannedAuditV13RegradesTheNewestGeneratableCorpus`
  (#1522)). `datagen-ci.yml` runs `-release-gate` on every datagen pull
  request and the release gate fails closed when a supported version or a
  grading-policy floor owns no bank
  (`TestReleaseGateCoversEverySupportedVersionAndPolicyFloor` (#1522)):

  ```sh
  go run ./cmd/graderaudit -bench-version 13 -seeds 40 -run-size full
  go run ./cmd/graderaudit -release-gate
  ```

### Gates and postures (scorer, `services/dittobench-api`)

The relay records evidence per successful completion (metadata only: offered
tool names + schema digests, `tool_choice`, harness-authored span digests,
model-emitted tool names, completion spans, choices and output tokens). The
scorer reads it only at `bench_version >= 13`; v2–v12 reports are
byte-identical. The full wire statement of every rule is in
`services/dittobench-api/PROTOCOL.md` (*bench_version 13* sections).

| Gate | Case note(s) | Default posture | Switch | Vector | Issue |
| --- | --- | --- | --- | --- | --- |
| Catalog-present (restraint requires an offer; expected tool must be offered) with the semantic-preloading safe harbor (top-k = 3 of the published TF-IDF embedding, or non-empty on declarative/chit-chat/decline) | `restraint_without_offer`, `expected_tool_not_offered`, `semantic_preloading_safe_harbor` | shadow | `DITTOBENCH_V13_CATALOG_GATE_POSTURE` | `TestCatalogGateRestraintRequiresAnOfferUnlessSafeHarbor`, `TestCatalogGateExpectedToolMustBeOfferedUnlessSafeHarbor`, `TestCatalogSemanticTopKIsDeterministicAndRanksTheCuedTool` | #1826 |
| Swallowed model call (restraint scored on model-emitted calls) | `swallowed_model_call` | shadow | same switch | `TestCatalogGateSwallowedModelCallScoresRestraintOnModelChoice` | #1826 |
| Claim-span provenance (claim tokens of the credited span ⊆ union of the case's completion tokens after `scoregates.NormalizeSpan` / `ValueTokenHashes`) | `served_text_not_model_emitted`, `no_model_completion`; grader-side `slot_not_in_prose` | shadow | `DITTOBENCH_V13_CLAIM_PROVENANCE_POSTURE` (shared with the causal gate) | `TestTextProvenanceVerdicts`, `TestNormalizeSpanVectors`, `TestApplyV13ClaimProvenanceHonestPatternsPass` | #1849 |
| Causal model dependence (claim tokens ⊆ harness-first prompt tokens minus `/seed` records, served tool results, the case's `user_input` and the validator system prompt) | `answer_in_prompt` | shadow | same switch | `TestCausalDependenceVerdicts`, `TestV13ProvenanceBankGIHNegativeIsGraderBlind` | #1833 |
| Twin / pair post-pass (`decision_twin`, `as_of_twin` concordance; base + counterfactual pair) | `twin_concordant`, `counterfactual_insensitive` | observe | `DITTOBENCH_V13_TWIN_POSTURE`, rule `DITTOBENCH_V13_TWIN_RULE` (`concordant_zero` default, `pair_product` auto-fallback when the honest concordant-error rate exceeds 5%) | `TestTwinPostPassBaselinesZeroAndOracleFull`, `TestTwinPostPassCounterfactualZeroesOnlyThePair`, `TestTwinPostPassLeavesEarlierVersionsUntouched` | #1835 |
| Per-case inference cost factor `clamp(1 − α·max(0, tokens_out − budget_c), 0.6, 1)` over successful completions, choices and output tokens; budgets 3 completion-equivalents (memory, single tool) / 5 (chains) at 512 tokens each | `per_case[].inference_cost` | shadow (reported, never applied) | enforce decision after #1521 | `TestCostFactorRule`, `TestCostBudgetsArePublishedPerClass`, `TestBuildInferenceCostIsVersionGatedAndShadow` | #1850 |

The provenance and causal gates are memory-case gates; the catalog gates are
tool-case gates. Enforcing the catalog gate has an explicit fleet precondition:
`completions_total` non-null on ≥ 99% of cases across ≥ 3 v13-capable
validators (#1519). Incomplete or unavailable relay evidence always fails
**open** (`TestCatalogGateFailsOpenWithoutSettledEvidence` (#1826)). Per-case
notes and shadow verdicts are exposed to the owning miner on the Platform
per-score detail and reproducible locally with `local-rehearsal.py --gates`
(#1852, #1851), so a shadow zero is visible and appealable before anything
enforces.

One calibration item is worth stating plainly: the implemented safe harbor
requires a **non-empty** catalog on declarative/chit-chat cases, so a
threshold preloader that offers zero tools on chit-chat is recorded as
`restraint_without_offer` (in shadow). The plan's honest-positive list names
that preloader; #1521 must show it produces 0 false zeros, or the safe harbor
widens before enforce. Owner decision — default taken: document the code as
implemented, calibrate before enforce.

### Probes and pinned ceilings

All probes are public, model-free and deterministic. They are measurements,
not secret defences; an adversary can read every one.

| Probe | Command | Pinned ceiling / baseline | Pinned by |
| --- | --- | --- | --- |
| Tool 1-NN outcome prober | `go run ./cmd/toolprobe -bench-version 13 -run-size full -train-seeds 30 -held-out-seeds 10` | complete tool-outcome accuracy 36.10%, verbatim argument exposure 1.80% (gate: < 50% / < 25%) | `TestV13ToolBenchContractAcrossFortySeeds` (#1843) |
| Memory exposure audit | `go run ./cmd/memoryprobe -bench-version 13 -seed 41 -run-size full` | ≥ half of evidence-bound answers computed; correction/join families count as computed from v13 | `TestMemoryExposureAuditIsVersionExplicit` (#1824) |
| Generator-inverse harness (GIH, N13) and 10k-seed router (N14) | `go run ./cmd/parserprobe -bench-version 13 -seeds 40 -run-size full -json` | pass-off (salt 0) baseline ≈ oracle (v12: 0.992 composite over seeds 1–40) — **the published baseline, not a failure**; ceiling on a surface-passed artifact: GIH and router composite ≤ starter-kit − 0.05 overall and on every slice (story, programs, personal, quantity, tool prompts) | `TestParserprobeBaselineOnPublicSeed` (#1829); `TestParserprobeCeilingOnSurfacePassedArtifacts` (#1829), armed by `DITTOBENCH_SURFACE_PASSED_ARTIFACTS`, report-only until the #1832 owner decision |
| Grader robustness audit | `go run ./cmd/graderaudit -bench-version 13 -seeds 40 -run-size full`, `-release-gate` | no hard negative passes, no reviewed positive fails, public-question-only passable share < 5% per claim kind | `TestSyntheticRobustnessV13BankIsCleanAndCoversEveryKind` (#1522), `TestCannedAuditV13RegradesTheNewestGeneratableCorpus` (#1522) |
| Mix audit | `go run ./cmd/mixaudit -bench-version 13 -seeds 40 -gate` | the `MixGateV13` caps and floors above | `TestV13MixAuditGateAcrossFortySeeds` (#1848) |
| `/seed` label-leak probe | `go test ./gen -run LabelLeak` | family / arc / slot at chance on 10 held-out seeds (v12: 94% from the session prefix) | `TestV13SeedWireCarriesNoFamilyArcOrSlotLabel` (#1827) |
| Story arc recovery | `go test ./universe -run StoryV2ArcRecovery` | raw pre-pass ceiling reported per seed; the enforced ceiling is parserprobe's after the surface pass | `TestStoryV2ArcRecoveryCeilingIsReportedAndDeterministic` (#1839) |

`cmd/parserprobe` is the honest adversary every surface claim is measured
against, replacing the 1-NN / family-id / 4-gram / fixed-recipe strawmen the
generator itself beat: it reads only what a harness sees on the wire (the
`/seed` records, the staged questions, the tool prompts), assembles a
typo-tolerant parser from the repository's own frames (`internal/parserprobe`
registers the v10–v12 question and record surfaces and derives the
`v<N>-open-program` family from the artifact's `bench_version`, so a contract
without a registered grammar is reported as unclassified rather than scored as
a silent zero), recovers (family, slots) for every question, applies the
public oracle arithmetic, launders the value through one "reply exactly"
completion, and is graded by the real deterministic grader. The router (N14)
replaces only the family-identification step with a naive-Bayes classifier
trained on locally generated seeds. `TestParserprobeProgramGrammarCoversEveryContract`
(#1829) walks every supported `bench_version` from v10 up and requires the
program family to be recognised and inverted near-perfectly, so a v13 contract
that re-renders the program surface fails it until its grammar is registered.
Three wire properties the probe exposes are worth naming because an honest
harness pays for them too: the program question binds its subject
relationally and never names its group, so order is the only wire-visible
binding; the parser-divergence questions repeat verbatim per round; and the
projector edits unprotected join keys, which one-edit fuzzy joins absorb.

### Known vector

`TestV13KnownVector` (#1848) in `gen/publicvector_test.go` pins seed
`123456789`, full profile. Until the last interim slot generator lands and the
`/seed` label-leak fix is in the same tree, the pin is **interim**: every
generator swap re-pins it as a deliberate contract decision, and the hash in
the test is authoritative over any copy in prose. The final v13 vector is the
one pinned after the last swap, so opaque session ids and jittered timestamps
— never `story-%02d-*` ids or 137-hour steps — are what the contract freezes.
v2–v12 vectors do not move; if one does, the gating is wrong.

### The surface pass and the open owner decision (#1832)

`gen/v13_surface.go` (`V13ApplyArtifactSurfacePass`) renders every
harness-visible surface as a function of `(seed, surface salt)`: the
compositional stored-directive markers, typo v2 over a stable share of tool
prompts, questions and non-story seeded prompts, a `TranslationPass` hook
(no-op by default) and, under a non-zero salt, a **regeneration canary** — the
world canary nonce is re-keyed within its per-seed shape and the public (salt
0) nonce is planted as a distractor, so a harness answering from a dataset it
regenerated from the public generator scores zero on the canary.
`SurfaceOptions{Salt, Translation}` is threaded through
`GenerateDatasetWithSurface` / `BuildArtifactForVersionWithSurface` and
`generate -surface-salt`.

- **Salt 0 is the public rehearsal default and is byte-identical to the
  unsalted path** (`TestV13SaltZeroIsByteIdenticalToUnsaltedPath` (#1832)).
  Miners, the starter kit's `local-rehearsal.py`, the screener oracle and the
  practice API all run salt 0.
- A non-zero salt changes surfaces only — never semantics, values, oracles or
  `Claims` (`TestV13SaltChangesOnlySurfaces` (#1832),
  `TestV13SurfacePassProtectsGradedValues` (#1832)) — and is recorded on the
  artifact as `surface_salt`, an audit field that never reaches the harness
  wire.
- **Open owner decision (Owner decision — default taken: ship the shared
  groundwork, decide the exchange separately).** Which side holds the salt is
  not decided by this contract: (A) a validator commit-reveal salt
  (`sha256(salt)` in the job claim, revealed in the signed score report,
  `scores.dataset_salt/_commitment`, `reproduction_command --salt`), or (B) a
  Platform-side private paraphrase/translation pass over the pinned
  grammar-expanded artifact with a published passed-dataset SHA. Both are
  surface-only; neither ships a salt exchange in v13.0's contract PRs.
  Deferral is not an option for v13.0 *activation*: until one lands, every
  surface defence above is regenerable from the public repo, the parserprobe
  ceilings stay report-only, and the GIH scores ≈ oracle by construction.

### Multilingual fraction (issue #1831)

Translation and code-switching are performed by the private surface pass
(`multilingual.TranslationPass`, stubbed behind an interface; a cached LLM
translation pinned per dataset over a seed-derived, unannounced Latin-script
subset of es/pt/fr/it/de/nl), never by checked-in per-language grammars.
`multilingual.Config{Fraction, Languages, Pass}` is the profile knob; **the
v13.0 default is `Fraction: 0`** (`TestDefaultConfigDrawsNothing` (#1831)) —
Owner decision — default taken: the fraction (the plan's 12% of memory
questions / 10% of records / 15% of tool prompts) is set by the #1521
starter-kit < 2-point rule, not here. Values (names, amounts, options) stay
canonical under any draw (`TestV13MultilingualValuesStayCanonical` (#1831));
the grader accepts the answer in the question's language or English and fails
closed for a language it holds no lexicon for. Non-Latin scripts are a v13.1
decision.

### What a harness sees

The wire stays at `bench_version` 9 (#1519 option A — Owner decision — default
taken: keep `publicWireBenchVersion = 9` with additive optional fields; a
naive bump to 13 fails every deployed harness closed at the starter kit's
`MIN..=MAX_SUPPORTED_BENCH_VERSION` range check). Every harness-visible v13
addition is additive on the existing shapes: richer tool schemas (`enum`,
runtime-described options, coined decoys), coined served content, staged
`/seed` waves whose 2xx is the ingest acknowledgement, and "as of" anchors
inside `user_input`. Every grader-only field (`claims`, `twin_relation`,
`required_arg_claims`, `restraint`, `language`, `surface_salt`) is stripped
before the wire (`TestV13GraderOnlyFieldsNeverReachHarnessWire` (#1824),
`TestV13GraderOnlyFieldsNeverReachRunPayload` (#1824)). The hostile-harness
projection is unchanged; its v13 notes are in
[v9-harness-projection.md](v9-harness-projection.md). The miner-facing
statement — what is graded, what the gates look for, and the honest
architectures that pass them — is the starter kit's
[PROTOCOL.md](../../../miners/dittobench-starter-kit/PROTOCOL.md) and
[README](../../../miners/dittobench-starter-kit/README.md) (*Bench v13: how
to stay inside the gates*).

Run sizes, the deterministic grader boundary (no LLM judge), the inference
boundary and locked model, LongMemEval floors (`bench_version >= 9`, a floor,
never an enumerated whitelist), and the v9 signed-evidence / score-gate /
curve-v3 efficiency stack all carry forward unchanged.

## Auditing an old score

Pin two things: the `bench_version` published with the score, and the **module
release** the validator scored it with.

The version alone fixes the dataset bytes. It does not by itself fix grading:
grader corrections like the three listed above ship inside a module release and
apply to whatever transcript they are handed. That is why validators pin an
exact release rather than tracking `@latest`, and why a reproduction should use
the release recorded alongside the score. Reproducing a v3 score with a v4-era
module gives you v3's dataset graded by a later grader, which is a different
question from the one the validator answered.
