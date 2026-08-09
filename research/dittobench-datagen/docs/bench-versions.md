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
| 9 (pre-activation) | `2027-01-01` | A qualification contract for the Bench v9 family mix and launch gates. Explicit generation and offline audit are available; the advertised runtime contract remains v8 until activation. |

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
