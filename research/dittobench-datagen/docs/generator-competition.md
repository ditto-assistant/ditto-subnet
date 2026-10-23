# DittoBench generator competition

The generator competition is the counterbalance to the agent competition.
Agent miners are paid for harnesses that answer DittoBench cases. Generator
miners are paid for **cases**: they find what the best agents still get wrong.
Cases that split the top agents are the benchmark's next frontier, so the two
competitions push each other. Agents must keep generalizing, and generators must
keep finding new blind spots as agents close the old ones.

This document is the design. The contract lives in
`research/dittobench-datagen/challenge`; each layer below names the component
that owns it.

## Goals and non-goals

Goals:

- Pay for cases that are **valid**, **solvable**, and **discriminating**: some
  strong agents solve them and others do not.
- Make every rule a generator is judged on reproducible locally and
  deterministic (no LLM judge, no clock).
- Resist the obvious exploits:
  - unanswerable or ambiguous "gotcha" cases
  - answers baked into a colluding agent
  - copying another generator's cases
  - spamming near-duplicates
- Ship weight-zero first, like the coding and router tracks.

Non-goals for v1:

- Running miner-supplied generator **code**. Packs are inert data.
- Tool-call cases. v1 is memory recall only. Tool cases need validator-served
  fixtures that a pack cannot author safely.
- Folding generator cases into the agent benchmark automatically. Promotion is
  an operator decision (see "Feedback loop").

## Why packs, not generator programs

A generator is the miner's private program. Only its output, a **challenge
pack**, crosses the trust boundary. That choice has three consequences:

1. No sandbox or screener is needed. A pack is JSON that is bounded, decoded
   strictly, and checked by a pure function.
2. Every acceptance rule is the same code the miner runs locally:
   `go run ./cmd/challenge validate pack.json`.
3. The competition still rewards generators rather than hand-authored cases.
   The score is per case and normalized to a 32-case target. Credit decays as
   agents improve (see "Freshness"). So sustained earnings need a program that
   keeps producing new, valid, hard cases.

## The challenge pack (contract v1)

Defined in `challenge/pack.go`. A pack contains:

- `contract`: `"dittobench-challenge-pack/v1"`.
- `grading_bench_version`: `13`. Every case is graded by the v13 memory grader
  policy, the newest agent-competition contract. A case cannot opt into an older
  policy.
- `miner_hotkey`: the SS58 hotkey the pack is bound to. It is covered by the
  pack hash, so a pack cannot be re-uploaded under another hotkey.
- `generator`: the miner-asserted name, version, seed, and optional source
  digest. Provenance only; nothing trusts it.
- `memory_waves`: the haystack. These are ordinary `protocol.SeedRequest` waves
  (up to 4 users, 8 waves, 2,000 pairs).
- `cases`: 8 to 64 `protocol.MemoryCase` values. Each case adds `user_id`,
  `run_after_wave`, and `evidence_pair_ids`.

The pack identity is the SHA-256 of the exact uploaded bytes
(`challenge.Digest`). The miner signs it, Platform stores and pins it, and the
scorer refuses served bytes that hash differently. Hashing raw bytes rather
than a re-encoding keeps the identity reproducible in any language. A case's identity across packs is its **fingerprint**: the
SHA-256 of its normalized question, gold answer, and list items.

## Validation (`challenge.Validate`)

Validation is deterministic and runs every rule, so a miner sees every problem
in one pass. A pack is accepted only with zero issues.

| Rule | What it rejects |
|---|---|
| Schema and bounds | Bad IDs, timestamps, sizes, unknown fields, trailing data, more than 4 MiB |
| Allowed kinds | Anything but `value`, `number`, `money`, `date`, `direction`, `list`, `ordered_list`. Stance, decline, clarify, and absence answers cannot be grounded, so they are where unanswerable gotchas would hide. |
| Unsupported fields | Bait tools, twins, grounding tokens, slot lexicons. These select scorer machinery or grader leniency a pack cannot supply. |
| Evidence | Every case names 1 to 8 evidence pairs. Each must be seeded in the same user's memory at or before `run_after_wave`. |
| Grounding | Every accepted answer item (the grader's own `ClaimAlternatives`) appears in the evidence text. Money also matches `1,620.00`, `1,620`, and `1620`. |
| Leakage | No answer item appears in the question. |
| Gold passes | The production grader scores the gold answer 1.0, and each `accept_any` alternative scores at least 0.5. |
| Trivial answers fail | An empty answer, an abstention, and a verbatim echo of the question each score below 0.5. |
| Distractors fail | Every `distractor_answers` and `forbidden_answer` scores below 0.5. |
| Memory dump fails | A response that recites the user's entire visible memory scores below 0.5. A case therefore needs interference (a decoy fact that is itself in memory), so credit requires choosing the right fact. |
| Duplicates | Two cases in one pack share a fingerprint, or ask the same normalized question of the same user. A repeated question has no single right answer. |

The reference generator (`challenge.ReferencePack`) emits valid packs. It is
used by tests, by `challenge example`, and by the generator starter kit.

## Scoring (`challenge.ScoreCases`)

The trusted scorer runs a validated pack against a **panel**: the current top N
agents, drawn from distinct owners where possible, plus the champion. Each
panel run uses a fresh harness, seeds the pack's waves, runs the cases, and
grades them with the production memory grader. Infrastructure failures are
marked `fault` and excluded, not counted as misses.

For each case, the eligible agents are those not owned by the generator's
coldkey and with an unfaulted result. Over those agents:

```
solvable       ⇔ solved (score ≥ 0.5) by ≥ min_solver_owners distinct owners
trivial        ⇔ solvable ∧ solved by every eligible agent
discriminating ⇔ solvable ∧ ¬trivial
credit         = (1 − mean score) × (1 + champion_bonus · [champion failed])
```

For the pack, with `n` cases, `dup` duplicate cases (fingerprints an earlier
pack already holds), and `u` unsolved cases:

```
score = Σ credit / ((1 + champion_bonus) · max(n, target_cases)) × (1 − u / (n − dup))²
```

The shipped parameters (`DefaultScoreParams`) are:

| Parameter | Value |
|---|---|
| `min_panel` | 3 |
| `min_solver_owners` | 2 |
| `solve_threshold` | 0.5 |
| `champion_bonus` | 0.5 |
| `target_cases` | 32 |

Why each term is there:

- **Solvability needs two owners.** A miner who controls both a generator and a
  top agent could bake a pack's answers into the agent image. That makes a case
  "solvable" for exactly one owner, which is not enough. Defeating this needs
  collusion between two independent top owners. The generator's own agents never
  count at all.
- **The squared solvability factor** is the counterweight to the incentive to
  "defeat" agents with broken cases. If half a pack's cases are unsolved, the
  pack keeps at most a quarter of its credit.
- **Trivial cases earn nothing.** They do not move the frontier.
- **The champion bonus** pays most for the king's blind spots.
- **The 32-case target** stops a tiny pack of lucky cases from matching a
  full-sized one.
- **Duplicates score zero for the later pack.** The first scored pack that
  holds a fingerprint keeps it.

If any case lacks `min_panel` eligible results, the pack is
`insufficient_panel`. It scores zero and is rescored when more panel agents are
available. A partial panel never decides a pack.

Panel members are summed in `agent_id` order, so float results are identical
across implementations. `challenge/testdata/score_vectors.json` holds golden
vectors that Platform's Python port must reproduce exactly.

## Freshness

A pack's score is a snapshot against the panel at scoring time. The ledger
counts each hotkey's best scored pack within a rolling window (7 days by
default). As agents improve, yesterday's discriminating cases become trivial.
A generator therefore has to keep producing.

## Privacy

Pack contents are never served publicly or to agent miners. If they were, agent
miners could train on the cases and the "discriminating" signal would collapse.
Platform publishes per-pack scores and case status counts only. The owning
miner can read their own per-case statuses. Panel agent identities stay
aggregated.

## Feedback loop

Cases scored `discriminating` are recorded as promotion candidates. Operators
can review them in Backroom and curate them into a future private benchmark
pool. That is the counterbalance at its strongest: a generator's best cases
become the next bar the agents have to clear. Promotion is manual in v1, so no
pack can move agent emissions without an operator ruling.

## Components and ownership

| Layer | Component | Responsibility |
|---|---|---|
| Contract | `research/dittobench-datagen/challenge`, `cmd/challenge` | Pack schema, validation, scoring reference, golden vectors, reference generator |
| Executor | `services/dittobench-api/internal/challengerun`, `cmd/challenge-scorer` | Run a pack against a ready harness with the production runner and grader; poll Platform for work and post results |
| Platform | `apps/platform` | Signed pack uploads, private storage, revisioned settings, scorer work and result intake, Python scoring port, generator ledger |
| Validator | `ditto/validator` | `generator` track in `SHADOW` at 0 bps, folding the generator ledger |
| Backroom | `apps/backroom` | Read the competition, packs, and verdicts; set settings with revision and confirmation |
| Miner | `ditto generator`, `miners/dittobench-generator-starter-kit` | Validate, sign, and upload packs; reference generator to fork |

## Rollout

1. **Shadow (this stack).**
   - Settings `mode=shadow`.
   - Every ledger entry is `weight_eligible: false` and the validator track is
     `SHADOW`, so nothing touches emissions.
   - The scorer drives operator-provisioned panel harnesses.
2. **Panel automation.** The scorer launches panel agents from their screened
   images through the DittoBench sandbox and inference broker, the same path a
   validator scoring run uses.
3. **Enforce.**
   - Operators set a generator track share through governance.
   - Settings move to `mode=enforce`.
   - The ledger marks entries eligible.
   - An upload fee is added at the same time.
