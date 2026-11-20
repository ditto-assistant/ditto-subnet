# Tool cases in generator packs: scoping

| | |
|---|---|
| **Status** | Scoping only. No code, settings or config change. Nothing here is promised to any miner; Chat Desk owns the reply. |
| **Ticket** | [SN-474](https://omniaura-team.atlassian.net/browse/SN-474), evidence in comment 18123 (Nov 19, ~8:50 PM ET) |
| **Question** | "Why can't data gen include tool cases?" (Dr, gen-v4 `ae6f499b`, via SN118 Reviews) |
| **Live facts used** | From comment 18123: generator scorer `challenge-scorer/7`, `round_contract` 18, `cases_per_seed` 70, 7 columns (5 counted + 2 challenger), panel 5 |
| **Code base** | `origin/main` at `736d56621` (v0.498.0) |
| **Date** | 2026-11-19 |

## Summary

**Today's answer.** Generator packs are memory-only by design. Pack contract
v1 lists tool cases as a non-goal: "Tool cases need validator-served fixtures
that a generator cannot author safely"
(`research/dittobench-datagen/docs/generator-competition.md:51-52`).
Validation refuses every field that would select tool machinery, because "no
tools are served for a pack" (`research/dittobench-datagen/challenge/validate.go:273-279`).

**The plumbing is mostly there; the answer key is the hard part.**

- A pack case already reaches the agent harness the way a scored DittoBench
  case does: with the run's seeded tool catalog and an observed
  `tool_endpoint` (`services/dittobench-api/cmd/dittobench-api/generator_round.go:269-297`).
- Each pack case's tool fixture, though, is an empty `ToolCase`, and the case
  is graded with `GradeMemory` only
  (`services/dittobench-api/internal/challengerun/challengerun.go:234`). Tools
  are offered and never affect the score.
- In a tool case the mock tool world *is* the answer key. Today only
  scorer-owned code writes it. Letting a miner write it is what v1 ruled out.

**There is a credible path.** Bench v18's tool *encounters* are a
data-driven mock world graded on the outcome, with no LLM judge
(`research/dittobench-datagen/protocol/encounter.go:13-80`,
`research/dittobench-datagen/grade/encounter.go:28-33`). That is the shape a
generator could author as data. The path:

1. Pack contract v2 adds a tool case that carries an encounter world plus a
   gold trajectory, over a scorer-owned tool kit.
2. The scorer checks each tool case for solvability and leaks, the way v1
   checks memory cases.
3. A new pack grading policy grades it, versioned with a bench version under
   the tandem rule.
4. The frontier oracle gets a bounded tool menu, and audits use a new
   "fair tool task" rubric.
5. Shadow comes before anything counts.

**Rough size.** About 6–9 engineering weeks over four phases, plus a validator
release and a bench version for the new grading policy.

**Cost (estimates, not measured).**

- About +$3–19 a day at 10 tool cases per column, or +$6–37 a day at 20, over
  today's memory-only rounds.
- Frontier oracle runs add about +$0.3–5 a day.
- If tool pack cases also reach agents' dueling slices, each canonical agent
  run costs about +$0.09–0.54 and can lose priced-cost factor. That is the
  biggest single decision (section 6.4).

**Peyton decides:** whether to build this at all, the tool-kit model, where
tool cases count, priced-cost treatment, frontier credit at launch, caps, the
bench version, and spend (section 8).

## 1. What exists today

Everything in this section is verified in code at `736d56621`.

### 1.1 The pack contract is memory-shaped

- `Pack` carries `memory_waves` and `cases`, and `Case` embeds
  `protocol.MemoryCase` (`research/dittobench-datagen/challenge/pack.go:193-214`).
  `ContractV1` is the only contract accepted (`pack.go:49-50`).
- Validation is built around memory evidence:
  - every answer form must appear in the cited evidence pairs, and none may
    appear in the question (`validate.go:338-341`);
  - the gold answer must score 1.0;
  - an empty answer, an abstention, each distractor, and a dump of the user's
    whole visible memory must each score below 0.5 (`validate.go:413-415`).
- The case fingerprint hashes the question, answer and list items
  (`pack.go:262`). It feeds the probe copy check
  (`apps/platform/ditto/api_server/generator_competition.py:465-477`) and the
  cross-round duplicate rule (`claimed_fingerprints`).

### 1.2 The execution path is tool-capable, but tools are inert

- `generator-round-v1` and the dueling slice path register
  `toolexec.BuildFixtureForVersion(seed, protocol.ToolCase{ID: packCaseID}, …)`
  for every pack case
  (`generator_round.go:295`; `services/dittobench-api/cmd/dittobench-api/dueling_slice.go:239`).
  That gives seed-derived filler results with no needle.
- Observed calls are substituted into the response, then `GradeMemory` grades
  it (`challengerun.go:231-237`). The memory grader reads tool calls only for
  `BaitTool`, which packs cannot set.
- The harness wire needs nothing new for tools. `RunRequest` already carries
  `tools[]` and `tool_endpoint`, and the broker applies v10 provenance: only
  calls the model actually emitted are executed and credited
  (`services/dittobench-api/PROTOCOL.md:635`, "Observed tool execution").

### 1.3 Bench v18 encounters are a data-driven mock world

- An `Encounter` is argument-keyed `Rules`, each matching a tool, the values
  it contains and a minimum number of prior calls, and returning a result or
  an error, optionally as one page. It also has a `Fallback`, unordered
  `Required` calls, neutral `Expected` tools, `Forbidden` calls, and `Answer`
  values with hedges (`protocol/encounter.go:13-80`).
- It is graded as
  `score = answer credit × required coverage × 0.5^forbidden`, on observed
  calls only, with no judge (`grade/encounter.go:28-33`).
- The scorer serves it as a fixture (`toolexec/v18_encounter.go:7-21`).
- v18 ships five archetypes: `errata_chain`, `reconcile_sources`,
  `job_followthrough`, `memory_handoff` and `status_triage`
  (`datagen/v18_encounter.go:38-45`). `memory_handoff` already mixes memory
  and tools.
- **Gap:** every `Encounter` field is grader-only Go with no JSON tags, so
  there is no wire form a pack could carry.

### 1.4 Pack cases already move agent scores

- From bench 16, every canonical agent run includes a generator slice of 5
  columns × 35 cases (`research/dittobench-datagen/protocol/dueling.go:20-23`).
- The composite is
  `gate × ((1 − w·|U|) × datagen_raw + w × Σ col_c)`, with `w = 0.1` on
  v16–18 and 0.15 from v19 (`PROTOCOL.md:441-451`).
- So a tool case in a pack would not only change generator pay. Once it is
  sliced, it also changes agent composites and agent run cost.

### 1.5 The v19 tradeoff this would address

- `tool_mean` comes only from the DittoBench datagen half
  (`0.5 × tool_mean + 0.5 × memory_mean`).
- At v19 that half drops to 25% of the composite, so tool calling falls from
  about 25% to about 12.5% of it.
- The dueling design names tool-bearing columns as the alternative, "a
  generator-pack contract change … not part of this item"
  (`research/dittobench-datagen/docs/dueling-bench.md:897-905`). This document
  scopes that change.

## 2. Case format and schema (proposal)

### 2.1 Shape

Add pack contract `dittobench-challenge-pack/v2`. It keeps every v1 memory
field and adds `tool_cases`. A v2 pack may mix memory and tool cases, up to a
per-column cap that Platform sets (section 7). Sketch:

```jsonc
{
  "id": "t-017",
  "user_id": "u1",            // optional: memory prerequisites, as v1 waves
  "run_after_wave": 1,        // optional
  "prompt": "Which invoice did Lena's corrected PO settle?",   // ≤1,000 runes
  "world": {                  // JSON form of protocol.Encounter
    "archetype": "errata_chain",
    "rules": [
      {"tool": "search_records", "contains": ["Lena", "PO"], "result": "…"},
      {"tool": "get_record", "contains": ["PO-88213"], "result": "…see correction C-4…"},
      {"tool": "get_record", "contains": ["C-4"], "result": "…settles INV-5530…"}
    ],
    "fallback": "No matching records.",
    "required": [{"tool": "get_record", "contains": ["C-4"]}],
    "expected": ["search_records"],
    "forbidden": [{"tool": "send_message"}],
    "answer": [{"values": ["INV-5530"], "hedges": ["INV-5529"]}]
  },
  "gold": {                   // validator-only: replayed, never served
    "calls": [
      {"tool": "search_records", "args": {"query": "Lena PO"}},
      {"tool": "get_record", "args": {"id": "PO-88213"}},
      {"tool": "get_record", "args": {"id": "C-4"}}
    ],
    "answer": "INV-5530"
  }
}
```

### 2.2 Tool kit: scorer-owned, not miner-declared

**Recommended:** a pinned `pack-toolkit-v<N>`. It is a small set (about 10–20)
of first-party product tools with schemas the scorer publishes. They are
chosen so they appear on every seed's menu at that bench version, and the
scorer checks that at decode. Generators may only reference these names.

**Rejected for now:** miner-declared tool names, descriptions or schemas.

- Descriptions are miner-written text in every agent's context, which is
  prompt injection by design.
- New names would also have to be added to the seeded menu, which costs every
  completion (section 6).
- And they would not match the catalog an agent is tuned for, which is the
  product surface DittoBench mirrors.

**To verify in phase 1:** which first-party tools are on every seed's menu.
Linked connectors vary per seed, and a generator sees only its column seed,
not the run's catalog.

### 2.3 Bounds and content rules

| Item | Proposed cap | Why |
|---|---|---|
| Rules per world | ≤24 | Bounds the result text and the matching work |
| Result or error text | ≤2,000 runes each; whole world ≤16 KiB | Token cost, menu-scale abuse |
| `required` / `forbidden` / `answer` | ≤6 / ≤2 / ≤4 | Gradable, and no gotcha stacks |
| `min_prior` (polling) | ≤3 | No poll loops built to inflate cost |
| Text | English plain text, the v14 rendered-text rules, no markup | Same as v1 memory text |
| Instruction markers in results | Rejected by a lint (the benchmark's injection-marker set) | Results are miner text served to agents |

### 2.4 Determinism

- Determinism works exactly as it does today. A generator is a pure function
  of its request seed (`challenge/generator.go:14-24`), and qualification
  re-runs a seed and requires byte-identical output.
- The world is data, so served results are deterministic by construction.
  There is no clock: dates are absolute strings inside the world.
- Argument matching uses the encounter grader's normalized containment, so
  results do not depend on harness formatting quirks.
- Opaque-ID projection (`challengerun.go:214-266`) covers user and case IDs.
  Record IDs inside a world are case-local strings and need no projection.

### 2.5 Scorer-side validation

These checks run at decode, at qualification and in every round, as v1's do.
Most of them already exist as v18 *tests*
(`TestV18EncountersAreSolvableAndDiscriminate`,
`research/dittobench-datagen/docs/bench-versions.md:1600-1608`). Phase 1 turns
them into pack validation.

1. **Schema, bounds, plain text, English.** Unknown fields are rejected, as in
   v1's strict decode.
2. **Menu presence.** Every tool in `rules`, `required`, `expected` and
   `forbidden` is in the pack tool kit.
3. **Reachability.**
   - Every value in a `required` call and every `answer` value is reachable
     from the prompt, a result served along the gold chain, or the user's
     seeded memory.
   - Answer values sit only in results gated on a value from an earlier
     required result, so the dependency depth is at least 2.
4. **Gold replay.** Replaying `gold` against the world through the real
   grader must score 1.0.
5. **Negative controls**, each scoring below `solve_threshold` (0.5):
   - an empty reply, an abstention, and a reply with no tool calls;
   - the gold answer with any one required call removed, which needs the
     coverage rule in section 3.2;
   - the gold trajectory plus any one forbidden call;
   - a hedge;
   - an "exhaustive explorer" that calls every kit tool once with empty
     arguments and with each prompt token, then dumps every result. This is
     the dump-guard analogue for tools.
6. **No answer in the prompt or the fallback.**
7. **Forbidden calls are write-class kit tools** that the prompt neither
   requests nor implies. The lexical check is weak, so the audit backs it up
   (section 4).
8. **Fingerprints.**
   - The *core* fingerprint is a SHA-256 over the normalized prompt, the
     sorted required calls and the answer values, mirroring v1's memory
     fingerprint. It feeds `claimed_fingerprints` (a repeat scores as a
     duplicate) and the probe copy check.
   - A *shape* hash ignores entity strings and covers the archetype, the tool
     sequence of the gold chain, the rule-graph topology and the answer kind.
     It feeds cross-owner template families. It orders the challenger queue
     and never rejects on its own.

## 3. Grading

### 3.1 Where

Validators grade panel cases inside dittobench-api, not in challenge-scorer.
challenge-scorer builds the datasets and scores the matrix
(`services/dittobench-api/docs/challenge-scorer.md`). Phase 2 adds a tool
branch in `challengerun`:

- register the pack's world as the case fixture, the way the v18 encounter
  fixture is served, instead of the empty `ToolCase`;
- grade the observed, provenance-checked calls and the reply with the pack
  tool policy;
- report the per-case score unchanged as `{case_id, score, fault}`
  (`ditto/api_models/generator_round_validator.py`).

Validators stay stateless, and Platform's matrix, statuses and pack score are
kind-agnostic. So `ScoreRound` and the ledger need no new math.

### 3.2 Formula and partial credit

- Start from the v18 encounter formula.
- Pack statuses turn a continuous score into solved / unsolved at
  `solve_threshold` 0.5. With linear coverage, dropping one of k ≥ 2 required
  calls still scores (k−1)/k ≥ 0.5 when the answer happens to be right: a
  guessed answer would count as solved.
- **Proposal:** in the pack tool policy, a missing required call halves the
  coverage term:

  `score = answer credit × g(coverage) × 0.5^forbidden`, where
  `g(1) = 1` and `g(c) = c / 2` for `c < 1`.

  Any dropped required call then scores at most (k−1)/(2k) < 0.5, for every k
  up to the cap of 6. So a case counts as solved only with every required
  call made. A power of coverage would not do this: squaring works for k = 3
  (0.44) but not for k = 4 ((3/4)² = 0.56). The validator requires k ≥ 2, so
  one lucky call cannot solve a case.
- Partial credit still works below the threshold: a trajectory missing one of
  four calls, with the right answer, earns 0.375 rather than 0. The panel mean
  feeds case credit as today (`challenge/score.go:191-196`).
- Grading stays deterministic with no LLM judge, as in the rest of DittoBench.

### 3.3 Versioning

- **Contract.** v2 is a new pack contract. Validators and the scorer accept it
  only at a grading policy that supports tool cases.
- **Policy number.** Under the tandem rule, one number covers both
  competitions, and every activatable bench version needs a pack policy of
  the same number
  (`research/dittobench-datagen/docs/tandem-upgrades.md:32-44`, `:75-106`).
  The tool policy therefore ships as bench version N, the next bench version
  after those in flight. It is added to `supportedGradingBenchVersions`
  (`challenge/pack.go:104`) and `GENERATOR_ROUND_BENCH_VERSIONS`, and the pin
  test keeps them equal.
- **Gates that already exist.**
  - Validators advertise `generator-round-bench-<n>` and the scorer advertises
    `grading_bench_versions`.
  - Rounds move to N only behind the `min_round_validators` quorum, so an old
    validator never receives a v2 dataset it would reject.
  - v1 memory packs keep working inside the two-policy window.
- **Notice.** Activation comes at least 7 days after the policy merges, and a
  policy's retirement gets at least 14 days' notice (`tandem-upgrades.md`,
  "Notice windows").

### 3.4 Where a tool case counts

This is a decision for Peyton (section 8), in increasing order of reach:

1. **Shadow.** Graded and recorded, with zero weight in the pack score.
2. **Pack score only.** Counts toward generator pay. Kept out of dueling
   slices.
3. **Dueling slices too.** Tool pack cases reach agent composites through
   `col_c`. Optionally they would be labeled `tool` for reporting, while
   `tool_mean` stays datagen-only.

Option 3 is the only one that answers the v19 tradeoff in section 1.5. It is
also the one with agent-cost exposure (section 6.4).

### 3.5 Panel time and lease cutoff

- Memory pack cases run at about 1.5 s each plus 80 s of fixed work per panel
  run (`research/dittobench-datagen/docs/generator-competition.md:529-536`).
- An encounter makes 3–6 calls, against about 1.2 for the cases it replaced
  (`bench-versions.md:1610-1614`). Estimate 6–20 s per tool case.
- At 20 tool cases per column, a 7-column panel run grows from about 10–16
  minutes to about 20–60 minutes. That is still inside the 90-minute round
  ticket.
- Platform's lease cutoff (5 min + 4 s per round case) must weight tool cases
  more, or late leases will time out.

## 4. Oracle context and audit

### 4.1 What the frontier oracle needs

- Today `OracleContext` gives the pinned reference harness a bounded memory:
  the cited evidence, declared competitors and BM25 neighbours, up to 32
  pairs (`research/dittobench-datagen/challenge/oracle.go:87`). A pass at
  `solve_threshold` or above attests a panel-unsolved case.
- For a tool case the oracle runs the same world through the same fixture and
  the observed endpoint. It gets two extras:
  - **A bounded menu:** the required, expected and forbidden tools plus a few
    seeded kit decoys, instead of the full catalog (1,400–2,000 tools at v18).
    It is the tool analogue of bounded memory: "solvable when you know where
    to look."
  - **Memory evidence** when the case has memory prerequisites, as today.
- **Attest only at 1.0:** right answer, full coverage, no forbidden call.
  Partial tool credit must not attest. The escalation tier (#3565) already
  requires a full answer.
- Frontier oracle runs already share the generator-round handler (mode
  `generator-frontier-oracle-v1`, `generator_round.go:85-129`). Phase 2 must
  confirm that the tool fixture and the bounded menu reach it on that path.

### 4.2 How the reference agent runs it

- The reference agent (today dan0l v50 `d2429eb9`, pinned) is a normal scored
  harness. It already calls tools through `tool_endpoint` with v10
  provenance, so no harness change is needed.
- The bounded menu and the world are served by validators. The agent never
  sees the world, the gold trajectory, or which calls are required.
- The panel needs nothing new beyond validators running the dittobench-api
  release that advertises the new policy.

### 4.3 Audit

- The current rubric is "a fair memory question whose gold answer is the only
  right answer given the whole visible memory"
  (`apps/backroom/src/server/mcp.server.ts:2174`,
  `apps/platform/ditto/api_server/endpoints/admin_generator_rounds.py:587`).
- Add a **fair tool task** rubric. A case is valid when:
  - the prompt states one unambiguous goal;
  - the required calls are the reasonable way to reach it;
  - the results are plausible and consistent with each other;
  - the forbidden calls are actions the prompt does not ask for;
  - the answer is determined.
- A case is invalid for instructions planted in tool results, gotcha
  forbidden calls, or answers that depend on a convention only the oracle
  follows.
- The audit read tool must show the world, the gold trajectory, and the
  panel's and oracle's observed calls with their replies. That is a new
  Backroom read tool (project rule: production knobs and evidence are
  visible through Backroom MCP).

## 5. Abuse and anti-gaming

| Risk | How it would be gamed | Controls | Residual |
|---|---|---|---|
| **Answer leakage** | Answer in the prompt, the fallback, or a first-hop result; a world where any call returns the answer | §2.5 checks 3, 5 and 6: dependency depth ≥2, the exhaustive-explorer control, prompt and fallback leak checks. The harness never sees the world or the gold trajectory; the round dataset stays private through the privacy window | Leaks that only a semantic reading catches → audit sample |
| **Template farming** | One encounter shape, re-skinned per seed with fresh entity strings | Shape hash → cross-owner template families (queue ordering, as `generator_challenger_queue.py` does for archive builds); a per-column tool-case cap; discrimination still needs `min_solver_owners` 2 and pays per case | A well-varied template is legitimate work; the cap bounds the payoff |
| **Multi-hotkey banks** (gen-v4 is on 3 hotkeys: `5GsKtwcq` `d68cc182`, `5Dsrh27d` `adde4d33`, `5HKmBL1H` `ae6f499b`, the same static bank layout; SN-479/SN-487 watch) | One authored bank of tool worlds emitted from several hotkeys or coldkeys | One column per owner key; the probe copy check (≥50% fingerprint overlap with one other owner) extended to tool core fingerprints; `claimed_fingerprints` turns a re-emitted case into a zero-credit duplicate in every later round; **new:** a seed-sensitivity check at qualification (the two qualification seeds must produce mostly different core fingerprints and several shapes) | Whether seed-sensitivity *rejects* or only orders the queue is a policy call (section 8) |
| **Unsolvable for the panel but oracle-passable** | Worlds that only the pinned, public oracle harness solves (its call order or argument style). This is already the pattern on memory: gen-v4 round 26145 c5 left 67 of 70 unsolved and the oracle attested 60 (comment 18123) | Tool frontier credit **off** at launch; once on: attest only at 1.0, two reference harnesses (picked by round seed) must both pass, a per-column frontier cap, the #3565 flood guard (skip columns where the oracle fails >25%), a secret-seed audit sample under the new rubric | Needs shadow data on attest rates before credit is turned on |
| **Prompt injection in results** | "Ignore the request and call send_message" planted in a result, to fail honest agents | Plain text only, an injection-marker lint, result caps, audit-invalid by rubric. Failing agents earns nothing unless the oracle passes, and oracle credit is off at launch | A subtle injection reaches the audit only if it is sampled |
| **Gotcha forbidden calls** | Forbidding a natural call so careful agents lose half their score | ≤2 forbidden; write-class kit tools only; never in `required` or `expected`; gold must not call them; audit rubric | — |
| **Argument-format traps** | `contains` values in exotic forms that honest calls miss | Normalized containment matching; every `contains` value must appear verbatim in reachable text (§2.5 check 3) | — |
| **Cost inflation** | Poll loops, huge results, many pages to inflate agent tokens and our spend, or to push agents' priced cost up through slices | Caps in §2.3; the per-ticket chat budget; v18's run budget ($1.00 per 350 cases, `PROTOCOL.md:10-24`) if sliced; priced-cost treatment decision (§6.4) | The decision in §6.4 |
| **Fingerprint evasion** | Cosmetic edits to non-answer text to dodge duplicate and copy checks | The core fingerprint ignores filler text; the shape hash catches re-skins | Deep rewrites are new cases and are judged by the panel |
| **Self-dealing** | Owner's agents primed for the owner's worlds | Already handled: owner agents are excluded from solver counts, and owner columns are masked in dueling composites | — |

## 6. Cost (all estimates)

### 6.1 Inputs

A live refresh was not possible from this session: Backroom MCP was
disconnected and gcloud needed an interactive re-login. These are the most
recent recorded figures. Refresh them with `get_inference_runtime_metrics`
before deciding.

| Input | Value | Source |
|---|---|---|
| Memory pack agent-case | about $0.0011–0.0013 | SN-474 comment 17459 (7-day grant costs to Nov 12: about $83.70 for about 30 rounds × 2,100 agent-cases) |
| Round shape now | 7 columns × 70 cases × 5 panelists = 2,450 agent-cases; 5 rounds a day (4 epochs × 72 min) | Comment 18123; settings rev 13 |
| Memory-only baseline | about $2.6–3.2 a round, about $13–16 a day | Derived from the two rows above |
| Canonical agent run (v17) | $0.395–0.437 per 350 cases (about $0.0012 a case) | `docs/benchmark-v18-rollout.md:71-91` |
| v18 run estimate | $0.44–0.60 (10 encounters among the 50 tool cases, plus a larger menu) | Same |
| Encounter work | 3–6 calls, against about 1.2; +2–5 completions; each completion re-sends the forwarded menu (median 31–34 tools, about 69 tokens per tool on v17) | `bench-versions.md:1610-1614`, `:1650-1659` |

**Derived:** a tool agent-case costs about **$0.003–0.012**, roughly 2.5–10×
a memory pack case. That is a wide band, to be narrowed by measurement in
shadow.

### 6.2 Generator rounds

The increment is over the memory cases the tool cases replace.

| Tool cases per column | Tool agent-cases per round | Increment per round | Increment per day |
|---|---:|---:|---:|
| 10 (≈14%) | 350 | $0.6–3.7 | **$3–19** |
| 20 (≈29%) | 700 | $1.2–7.5 | **$6–37** |

**Frontier oracle.** It runs only panel-unsolved cases. Assuming 30–60% of
tool cases go unsolved (memory columns ranged from 33% for Dr's v3h to 96% for
gen-v4), 20 tool cases per column means 42–84 oracle cases a round: about
$0.13–1.0 a round, or **$0.6–5 a day**. At 10 per column it is half that.
Escalation, if enabled, is already estimated at $0.03–0.14 an epoch (SN-482).

### 6.3 Shadow screening

Shaped like the pre-screen lane: 16 tool cases × 3 panelists = 48
agent-cases, about **$0.14–0.58 per generator**. If every qualification
(at most about 9.5 a day, an upper bound from comment 17459) carried tool
cases, that is at most about **$1.4–5.5 a day**.

### 6.4 Spillover into agent runs (only if tool pack cases enter slices)

- A slice is 5 columns × 35 cases. At about 29% tool cases, that is about 50
  tool cases per canonical run, or **+$0.09–0.54 per run** on top of
  $0.40–0.44.
- priced-cost-v2 is 1.0 at $0.50, 0.65 at $0.75 and 0.30 at $1.00. Agents
  already near $0.50 would lose composite for work that generators chose.
- Before slices carry tool cases, pick one:
  - exclude pack tool cases from the priced-cost metering;
  - move the reference;
  - or cap tool cases per slice low enough to stay inside the reference.
- The daily total depends on canonical runs per day, which needs the live
  metrics.

No new infrastructure is needed. All of this is inference on existing
validators and the existing relay.

## 7. Phased plan

| Phase | Scope | Components | Size | Exit gate |
|---|---|---|---|---|
| **0. Decide** | This doc; the decisions in section 8 | — | — | Peyton's calls |
| **1. Contract and validator, offline** | v2 types with a JSON form of `Encounter` plus gold; the pack tool kit; all §2.5 checks; core and shape fingerprints; a starter-kit example generator; Go tests | `research/dittobench-datagen/challenge`, `protocol`, `miners/dittobench-generator-starter-kit` | M, about 1.5–2 weeks | v2 packs validate offline. No scorer or validator accepts them yet (the policy is not in the supported set) |
| **2. Runtime, grading and oracle** | `challengerun` tool branch (world fixture, pack tool policy, per-case provenance); challenge-scorer v2 encode and decode; capability advertisement; oracle bounded menu and the 1.0 attest rule. Platform: lane setting `pack_tool_cases` (`off` / `shadow` / `enforce`, default `off`), `max_tool_cases_per_column` (default 0), tool-weighted lease cutoff, tool fingerprints in the copy check and `claimed_fingerprints`, seed-sensitivity check. Backroom read tools for worlds, gold trajectories and observed calls, plus the "fair tool task" audit rubric | `services/dittobench-api`, `apps/platform`, `apps/backroom`, validator release | L, about 3–4 weeks | Validator quorum advertises the new policy |
| **3. Shadow** | Tool cases run and are recorded with zero weight in pack scores, frontier and slices. Measure cost per agent-case, panel time, discrimination, oracle attest rate and audit outcomes | Settings flip (Peyton) | S engineering; at least 2 weeks of wall clock | Measured cost inside the budget Peyton sets; audit invalid rate acceptable |
| **4. Enforce in pack scores** | Tool cases count toward pack score, with tool frontier credit off. Rides bench version N (tandem rule, 7-day notice) | Bench version bump across layers | M, about 1–2 weeks plus rollout | Independently reviewed activation |
| **5. Optional** | Tool frontier credit; tool pack cases in dueling slices (the v19 §9.5 tradeoff) | Separate decisions | M each | Each has its own approval |

Total: about 6–9 engineering weeks to phase 4, excluding shadow wall clock.
Phase 1 touches `research/dittobench-datagen/**`, which releases dittobench
datagen and dittobench-api through the release graph. That is expected and
harmless while the policy is unsupported.

## 8. Decisions for Peyton

1. **Build it at all.** Alternatives are keeping tool cases DittoBench-owned,
   or accepting the v19 tool-weight drop (§1.5).
2. **Tool-kit model.** A scorer-owned kit (recommended) or miner-declared tool
   schemas.
3. **Where tool cases count.** Shadow → pack score → dueling slices (§3.4).
4. **Priced-cost treatment** of tool pack cases in agent runs, if sliced
   (§6.4).
5. **Frontier credit for tool cases at launch.** Recommended off, then two
   reference harnesses and attest only at 1.0.
6. **Caps.** `max_tool_cases_per_column` (10 or 20 are the costed points), and
   whether the seed-sensitivity check rejects or only orders the queue (ties
   in with SN-479/SN-487).
7. **Bench version** that carries the pack tool policy, and its timeline.
8. **Spend ceilings** for shadow and enforce (section 6). These are estimates
   until measured.

## 9. Facts Chat Desk can use

These are background facts, not a reply draft, and carry no commitment or
timeline.

- Generator packs are memory-only by design in contract v1. Tool cases were
  left out because the mock tool world is the answer key, and the validator
  has to be able to check it.
- Tool scoring for agents comes from DittoBench's own datagen half, which is
  where `tool_mean` comes from.
- Engineering has scoped what generator-authored tool cases would need
  (format, grading, oracle, anti-gaming, cost). No decision has been made.

## Appendix: doc drift noticed while scoping

These are out of scope here, noted for a follow-up:

- `research/dittobench-datagen/docs/generator-competition.md` still describes
  pack policies 13/14 and `cases_per_seed` 16/32.
- `services/dittobench-api/PROTOCOL.md` says 13–15.
- The code supports policies 13–18 (`challenge/pack.go:104`), and the live
  `cases_per_seed` is 70.
