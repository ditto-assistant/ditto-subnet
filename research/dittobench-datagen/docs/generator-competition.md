# DittoBench generator competition

The generator competition is the counterbalance to the agent competition.
Agent miners are paid for harnesses that answer DittoBench cases. Generator
miners are paid for **generators**: programs that keep producing cases the best
agents still get wrong. Cases that split the top agents are the benchmark's next
frontier, so the two competitions push each other. Agents must keep
generalizing, and generators must keep finding new blind spots as agents close
the old ones.

This document is the design. The contract lives in
`research/dittobench-datagen/challenge` and `genrun`; each layer below names the
component that owns it.

## Goals and non-goals

Goals:

- Pay for generators whose cases are **valid**, **solvable**, and
  **discriminating** (some strong agents solve them and others do not) on seeds
  nobody sees in advance.
- Keep evaluation cheap: generation is CPU-only, with no model, so running a
  generator costs seconds of CPU. The only expensive part of a run is the agent
  panel, which the agent competition already pays for.
- Make every rule a generator is judged on reproducible locally and
  deterministic (no LLM judge, no clock).
- Resist the obvious exploits:
  - unanswerable or ambiguous "gotcha" cases
  - answers baked into a colluding agent
  - copying another miner's generator
  - a generator that ignores its seed and replays one good pack
- Ship weight-zero first, like the coding and router tracks.

Non-goals for v1:

- Inference inside generators. DittoBench's own datagen uses none, and that is
  the point: a model call would make generation slow, costly, and
  nondeterministic.
- Languages other than Go. One pinned toolchain keeps the build reproducible
  and the sandbox small. DittoBench datagen and this contract are Go, so a
  generator can import the contract types directly.
- Tool-call cases. v1 is memory recall only. Tool cases need validator-served
  fixtures that a generator cannot author safely.
- Folding generator cases into the agent benchmark automatically. Promotion is
  an operator decision (see "Feedback loop").

## Why generator code, not uploaded packs

An earlier draft had miners upload packs (the generator's output) instead of the
generator. Packs need no sandbox, but they let a miner see every case before
submitting. That makes three exploits cheap: pre-screening cases against a
friendly agent, hand-writing a small pack, and colluding on answers. Packs are
also spent once they have run.

Submitting the program fixes all three. The scorer chooses fresh 32-byte seeds
when it runs a generator, so nobody, including the miner, knows the cases in
advance. A generator is scored on several seeds and re-run on new seeds against
the current panel every rescore interval. A winning generator is also a lasting
asset: it can produce fresh cases for any future seed, so it can become a
DittoBench family.

Generators are CPU-only, so this costs very little. A build is a few seconds of
`go build`, and a generation run is milliseconds to seconds.

## The generator program (contract `dittobench-generator/v1`)

Defined in `challenge/generator.go` and `challenge/archive.go`.

**Source archive.** A gzip-compressed tar of a Go main module. It may hold at
most 4 MiB compressed, 32 MiB expanded, and 4,096 files. The archive must
contain:

- only regular files and directories, with clean relative paths (no links,
  devices, absolute paths, or `..`)
- `go.mod` at the root, with a `go` directive no newer than the builder
  (`go 1.27.3`)
- the main package's `.go` files at the root
- `vendor/` with every dependency, if `go.mod` has any requirements, because the
  build has no network

`challenge pack` (and `ditto generator package`) writes this archive
deterministically. It vendors into a temporary directory, so the miner's
checkout is never modified. The archive digest (`challenge.Digest`) is what the
miner signs. The **tree digest** (`challenge.TreeDigest`, SHA-256 over
`path NUL sha256(content) LF` in path order) identifies the files regardless of
tar order, timestamps, permissions, or compression. Platform refuses a tree
that was already uploaded, whoever uploads it.

**Build.** The scorer builds the module with `go build -trimpath` in the pinned
`golang:1.27.3` image. The build runs with:

- `CGO_ENABLED=0`, `GOPROXY=off`, `GOTOOLCHAIN=local`, and `-mod=vendor`
- `--network=none` and a read-only root filesystem
- all capabilities dropped and an unprivileged user
- 2 GiB of memory and 2 CPUs
- a 5-minute limit

**Run.** The scorer runs the static binary in pinned distroless (`static`)
with:

- `--network=none` and a read-only root filesystem
- all capabilities dropped and an unprivileged user
- 1 GiB of memory, 1 CPU, and 64 processes
- a 60-second limit per seed

It writes one `GenerateRequest` to stdin:

```json
{"contract": "dittobench-generator/v1", "seed": "<64 hex>",
 "miner_hotkey": "<ss58>", "cases": 16, "grading_bench_version": 13}
```

and reads one pack from stdout (at most 4 MiB). Stderr is diagnostic only. The
pack must be a pure function of the request. `GenerateRequest.Rand()` is a
convenient seeded source.

**Determinism.** The scorer re-runs the first seed and requires byte-identical
output. A generator that reads the clock or the environment, or iterates a Go
map into its output, is rejected as `nondeterministic`.

## The challenge pack (contract v1)

A generator's output, defined in `challenge/pack.go`. A pack contains:

- `contract`: `"dittobench-challenge-pack/v1"`.
- `grading_bench_version`: `13`. Every case is graded by the v13 memory grader
  policy, the newest agent-competition contract. A case cannot opt into an older
  policy.
- `miner_hotkey`: must equal the request's.
- `generator`: name, version, and `seed`, which must echo the request's seed.
- `memory_waves`: the haystack. These are ordinary `protocol.SeedRequest` waves
  (up to 4 users, 8 waves, 2,000 pairs).
- `cases`: exactly the request's `cases` count of `protocol.MemoryCase` values.
  Each case adds `user_id`, `run_after_wave`, and `evidence_pair_ids`.

A case's identity across packs is its **fingerprint**: the SHA-256 of its
normalized question, gold answer, and list items.

## Validation (`challenge.ValidateGenerated`)

Validation is deterministic and runs every rule, so a miner sees every problem
in one pass. A pack is accepted only with zero issues.

| Rule | What it rejects |
|---|---|
| Binding | The seed, hotkey, or case count differs from the request. |
| Schema and bounds | Bad IDs, timestamps, sizes, unknown fields, trailing data, more than 4 MiB. |
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

One invalid pack on any seed rejects the generator. A generator has to work on
every seed, not just the ones its author tried.

## Evaluation (`genrun.Evaluate`)

One run of a generator:

1. Build the source once.
2. Run each of the run's secret seeds (`seeds_per_run`, 2 by default).
3. Validate each pack against its request.
4. Run the current **probe seed**, an extra secret seed that is generated and
   validated but never shown to agents (see "Copies").
5. Re-run the first seed to check determinism.

A build failure, crash, timeout, invalid pack, invalid probe, or
nondeterministic output is the generator's fault. It rejects the submission,
with the reason recorded. Sandbox failures (Docker unavailable, a host I/O
error) are the scorer's fault. They are never reported, the lease simply
expires, and the run is retried on new seeds.

Only a fully valid generator meets the panel.

## Scoring (`challenge.ScoreCases`, `challenge.ScoreRun`)

The trusted scorer runs each valid pack against a **panel**: the current top N
agents (one per owner, `panel_size` 5 by default) from the same ledger
validators fold, with the first as champion. Each panel run seeds the pack's
waves into a fresh harness, runs the cases, and grades them with the production
memory grader. Case, pair, session, and user IDs are replaced with per-run
opaque IDs. Infrastructure failures are marked `fault` and excluded, not counted
as misses.

For each case, the eligible agents are those not owned by the generator's
coldkey and with an unfaulted result. Over those agents:

```
solvable       ⇔ solved (score ≥ 0.5) by ≥ min_solver_owners distinct owners
trivial        ⇔ solvable ∧ solved by every eligible agent
discriminating ⇔ solvable ∧ ¬trivial
credit         = (1 − mean score) × (1 + champion_bonus · [champion failed])
```

For one seed's pack, with `n` cases, `dup` duplicate cases (fingerprints already
claimed), and `u` unsolved cases:

```
pack score = Σ credit / ((1 + champion_bonus) · max(n, cases_per_seed)) × (1 − u / (n − dup))²
```

The run score is the mean pack score over the run's seeds, in order. Every
fingerprint a seed emits is claimed for later seeds, so a generator that ignores
its seed scores its first pack and duplicates for the rest.

The shipped parameters are:

| Parameter | Value |
|---|---|
| `min_panel` | 3 |
| `min_solver_owners` | 2 |
| `solve_threshold` | 0.5 |
| `champion_bonus` | 0.5 |
| `seeds_per_run` | 2 |
| `cases_per_seed` | 16 |

Why each term is there:

- **Secret seeds** mean neither the miner nor any agent knows a case before the
  scorer generates it. Pre-screening and baking answers into an agent no longer
  work.
- **Solvability needs two owners.** A miner who controls a generator and a top
  agent and shares the generator's source with it could teach the agent the
  generator's families. That makes a case "solvable" for exactly one owner,
  which is not enough. The generator's own agents never count at all.
- **The squared solvability factor** is the counterweight to the incentive to
  "defeat" agents with broken cases. If half a pack's cases are unsolved, the
  pack keeps at most a quarter of its credit.
- **Trivial cases earn nothing.** They do not move the frontier.
- **The champion bonus** pays most for the king's blind spots.
- **Duplicates score zero.** The first scored run that emits a fingerprint holds
  it. With random seeds, repeats come only from low-entropy generators or from
  copies.

If any case lacks `min_panel` eligible results, the run is
`insufficient_panel`. It scores zero and is retried on new seeds after a
backoff. A partial panel never decides a run. Platform does not lease at all
while fewer than `min_panel` agents are on the ledger.

Panel members are summed in `agent_id` order, so float results are identical
across implementations. `challenge/testdata/score_vectors.json` and
`run_vectors.json` hold golden vectors that Platform's Python port must
reproduce exactly.

## Copies

Two copy checks apply.

**Exact source.** Platform refuses an upload whose archive digest or tree
digest matches an existing submission.

**Behavior.** Every run also generates a pack on the current probe seed. That
seed is secret, and it is shared by every run in a probe epoch. The probe
pack's fingerprints are stored per submission and epoch. A new submission's
probe output is compared with earlier submissions from **other owners** in the
same epoch. If at least `copy_overlap_threshold` (50%) of its fingerprints
match one of them, the new submission is rejected as a `behavioral_copy` of
that one.

Renaming variables or re-ordering files does not change what a program emits,
so it does not evade the check. Because the probe seed is secret, a copy cannot
detect the probe and behave differently on it. Operators can rotate the probe
seed (for example after a suspected leak) with the Backroom confirmation
`ROTATE GENERATOR PROBE SEED`. Every active generator is then re-probed on its
next run.

The public starter kit is a starting point, not a submission. An unmodified kit
is accepted once. Every later identical upload is a duplicate, and the kit's
families are ones the top agents mostly solve.

## Freshness

Each run is a snapshot against the panel at that time. An active generator is
re-run on fresh seeds every `rescore_interval_hours` (24 by default). The
ledger counts each hotkey's latest run scored within a rolling window (7 days by
default).

As agents improve, a generator's families become trivial and its score falls.
Earnings therefore need a generator that keeps finding blind spots. Uploading a
new generator supersedes the hotkey's previous one. The previous one's last run
keeps counting until the new one is scored.

## Privacy

Generator source, seeds of in-flight runs, probe seeds, and case content are
never served publicly or to agent miners. If cases were served, agent miners
could train on them and the "discriminating" signal would collapse.

Platform publishes per-run scores and case status counts. Operators can read a
finished run's seeds in Backroom to reproduce its packs exactly. Panel agent
identities stay aggregated.

## Feedback loop

Cases scored `discriminating` are recorded as promotion candidates. Operators
can review them in Backroom and curate them into a future private benchmark
pool. A generator whose families keep earning can be adopted wholesale as a
DittoBench datagen family. That is the counterbalance at its strongest: a
generator's best output becomes the next bar the agents have to clear.
Promotion is manual in v1, so no generator can move agent emissions without an
operator ruling.

## Components and ownership

| Layer | Component | Responsibility |
|---|---|---|
| Contract | `research/dittobench-datagen/challenge` | Generator request and archive rules, pack schema, validation, scoring reference, golden vectors, reference pack |
| Sandbox | `research/dittobench-datagen/genrun`, `cmd/challenge` | Package, build, and run a generator in the Docker sandbox (or locally); `rehearse` is the scorer path for miners |
| Executor | `services/dittobench-api/internal/challengerun`, `cmd/challenge-scorer` | Lease runs from Platform, evaluate the generator, run each pack against the panel with the production runner and grader, post the evaluation |
| Platform | `apps/platform` | Signed source uploads, archive checks, private storage, revisioned settings, secret seeds and probe epochs, scorer work and result intake, copy detection, Python scoring port, generator ledger |
| Validator | `ditto/validator` | `generator` track in `SHADOW` at 0 bps, folding the generator ledger |
| Backroom | `apps/backroom` | Read the competition, submissions, runs, and verdicts; set settings and rotate the probe with revision and confirmation |
| Miner | `ditto generator`, `miners/dittobench-generator-starter-kit` | Rehearse, package, sign, and upload generators; reference generator to fork |

## Rollout

1. **Shadow (this stack).**
   - Settings `mode=shadow`.
   - Every ledger entry is `weight_eligible: false` and the validator track is
     `SHADOW`, so nothing touches emissions.
   - The scorer host runs Docker and drives operator-provisioned panel
     harnesses.
2. **Panel automation.** The scorer launches panel agents from their screened
   images through the DittoBench sandbox and inference broker, the same path a
   validator scoring run uses.
3. **Enforce.**
   - Operators set a generator track share through governance.
   - Settings move to `mode=enforce`.
   - The ledger marks entries eligible.
   - An upload fee is added at the same time.
