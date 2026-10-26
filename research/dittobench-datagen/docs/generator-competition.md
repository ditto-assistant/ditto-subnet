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
  generator costs seconds of CPU. The only expensive part is running agents
  on its cases, and that runs on validators through the same ticket and
  inference path the agent competition already pays for.
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
- Folding generator cases into the agent benchmark's weights automatically.
  Rounds rank agents in shadow; a benchmark built from the matrix is a later
  bench version (see "Rollout").

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

## Qualification (`genrun.Evaluate`)

Qualification is the gate a generator passes before it can hold a round
column. The scorer:

1. Builds the source once.
2. Runs each of the run's secret seeds (`seeds_per_run`, 2 by default).
3. Validates each pack against its request.
4. Runs the current **probe seed**, an extra secret seed that is generated and
   validated but never shown to agents (see "Copies").
5. Re-runs the first seed to check determinism.

A build failure, crash, timeout, invalid pack, invalid probe, nondeterministic
output, or behavioral copy is the generator's fault. It rejects the submission,
with the reason recorded. Sandbox failures (Docker unavailable, a host I/O
error) are the scorer's fault. They are never reported, the lease simply
expires, and the run is retried on new seeds.

A generator that passes is **qualified**.

## Rounds: the agent × generator matrix

Generators are scored against agents in **rounds**. A round is keyed on the
chain's own epoch counter:

```
round_index = SubnetEpochIndex // epochs_per_round      (4 by default)
```

`SubnetEpochIndex` counts every subnet epoch the chain fires. It survives tempo
changes and deferred or owner-triggered epochs, and it is the same counter the
epoch-pinned validator ledger keys on. So a round opens and closes on a Yuma
epoch boundary, and every validator folds the same generator ledger.

### Seed: commit, then reveal

The seed has to be unpredictable to miners, who know their own generator's
source and could otherwise precompute its cases. It also has to be
unpredictable to Platform, which must not be able to grind it.

1. **Commit.** While round `r − 1` runs, Platform draws round `r`'s 32-byte
   secret and publishes `commitment = RoundCommitment(r, secret)` =
   `sha256("dittobench-generator-round-commit/v1" ‖ 0 ‖ u64be(r) ‖ secret)`.
   The commitment goes on Platform and, when `onchain_commitment` is set, on
   chain as a Commitments `set_commitment` of `dgr1:{r}:{commitment}`.
   Validators publish the on-chain copy, not Platform. Platform elects up to
   three fresh validators that advertise `round_commitments`, in a stable
   per-round order (`sha256("dittobench-generator-round-publisher/v1" ‖ 0 ‖
   u64be(r) ‖ hotkey)`, lowest first). Each elected validator calls
   `set_commitment` from its own Pylon identity, reads its commitment back, and
   reports the block. A failed or silent validator hands its place to the next
   one in order. Publication is best effort: a round opens whether or not any
   validator confirmed it.
2. **Open.** At round `r`'s epoch boundary, Platform derives
   `seed = RoundSeed(r, secret, open_block_hash)`, using the hash of the block
   the epoch fired at. That block did not exist when the secret was committed.
3. **Reveal.** `privacy_window_rounds` (6) after the round opens, its secret,
   seed, and dataset digest become public. Anyone can check the secret
   against the commitment and recompute every case.

A round that was not committed before its first epoch never opens; it is void.
Each column runs on its own seed, `RoundColumnSeed(seed, column)`. So the
public default generator can fill several columns without repeating cases. The
derivations are pinned by `challenge/testdata/round_vectors.json`, which
Platform's port replays.

### Columns and panel

When a round opens it freezes:

- **Counted columns** (`counted_columns`, 5). These go first to the best
  qualified generators by latest round score, then to qualified generators that
  have never held a column. The **public default generator** fills whatever is
  left; it is owned by nobody and never appears on the ledger. One column per
  owner.
- **Challenger columns** (`challenger_columns`, 1). These go to qualified
  generators that have never held a column. They are scored as generators but
  never move an agent, so a new generator qualifies against the top agents
  without affecting them.
- **The panel.** This is the top `panel_size` agent owners from the ledger
  validators fold, plus `reference_agent_ids`: subnet-run baselines, each
  counted as its own independent solver.

### Execution

1. The scorer runs every column generator on its column seed. It encodes the
   valid packs into one canonical **round dataset**
   (`challenge.EncodeRoundDataset`).
2. A miner generator that qualified but fails on its round seed is rejected:
   generators must work on every seed. A failed default-generator column is
   only dropped.
3. Validators run each panel agent on the round dataset. Platform re-leases the
   agent's own ticket row with purpose `generator_round`, on idle capacity
   left after canonical work and continual retests.
4. The validator runs dittobench-api's `generator-round-v1` mode: the screened
   image, ticket inference, and the production memory grader, one column at a
   time.
5. The validator posts signed per-case results. A failed run is handed back
   without charging the agent anything.
6. The round closes when every panel agent reported, or when its last epoch
   ends. With fewer than `min_panel` results it is void.

## Scoring (`challenge.ScoreCases`, `challenge.ScoreRound`)

`challenge.ScoreRound` scores the whole matrix.

**Generators.** Each column is scored with `ScoreCases` against the whole
panel. The column owner's own agents never count. Columns claim their
fingerprints in order, so a later column repeating an earlier column's cases,
or a case an earlier round holds, scores as a duplicate.

For each case, the eligible agents are those not owned by the column's owner
and with an unfaulted result. Over those agents:

```
solvable       ⇔ solved (score ≥ 0.5) by ≥ min_solver_owners distinct owners
trivial        ⇔ solvable ∧ solved by every eligible agent
discriminating ⇔ solvable ∧ ¬trivial
credit         = (1 − mean score) × (1 + champion_bonus · [champion failed])
```

For one column's pack, with `n` cases, `dup` duplicate cases and `u` unsolved
cases:

```
column score = Σ credit / ((1 + champion_bonus) · max(n, cases_per_seed)) × (1 − u / (n − dup))²
```

A generator's ledger score is its latest round column score within
`window_days`.

**Agents.** Agents are ranked **pairwise-neutral**. Agents A and B are
compared only on cases of counted columns owned by neither A's owner nor B's
owner, and only on cases neither faulted. A wins the pair when its summed
score difference over those cases is positive. An agent's pairwise score is
`(wins + ties/2) / opponents`. Ties break on the mean over columns no panel
owner owns (the common cases), then on agent id. Standings are published per
round; in shadow they move no weight.

The shipped parameters are:

| Parameter | Value |
|---|---|
| `min_panel` | 3 |
| `min_solver_owners` | 2 |
| `solve_threshold` | 0.5 |
| `champion_bonus` | 0.5 |
| `cases_per_seed` | 16 |
| `epochs_per_round` | 4 |
| `counted_columns` | 5 |
| `challenger_columns` | 1 |
| `privacy_window_rounds` | 6 |

Why each term is there:

- **Secret, committed seeds** mean neither the miner nor any agent knows a case
  before the round opens, and Platform cannot choose the seed either.
- **Pairwise neutrality** is the answer to miners who mine both sides. A
  generator's column is excluded from every comparison its owner's agent takes
  part in. So a generator cannot lift its owner's agent, and cannot sink a
  rival relative to that agent either.
- **Solvability needs two independent owners.** A miner who shares a
  generator's families with their own agent makes a case solvable for one
  owner, which is not enough. The generator's own agents never count, and
  reference agents count as independent solvers that colluding miners cannot
  fake.
- **The squared solvability factor** is the counterweight to the incentive to
  "defeat" agents with broken cases. If half a pack's cases are unsolved, the
  pack keeps at most a quarter of its credit.
- **Trivial cases earn nothing.** They do not move the frontier.
- **The champion bonus** pays most for the king's blind spots.
- **Duplicates score zero.** The first round that emits a fingerprint holds it.

Panel members are summed in `agent_id` order and cases in column then case
order, so float results are identical across implementations.
`challenge/testdata/score_vectors.json`, `run_vectors.json` and
`round_vectors.json` hold golden vectors.

## Copies

Two copy checks apply.

**Exact source.** Platform refuses an upload whose archive digest or tree
digest matches an existing submission.

**Behavior.** Every qualification run also generates a pack on the current probe seed. That
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
`ROTATE GENERATOR PROBE SEED`. Every active generator then re-qualifies, and
re-probes, before its next round column.

The public starter kit is a starting point, not a submission. An unmodified kit
is accepted once. Every later identical upload is a duplicate, and the kit's
families are ones the top agents mostly solve.

## Freshness

Every round is a fresh snapshot: new seeds, the current top agents, and the
current best generators. As agents improve, a generator's families become
trivial, its column score falls, and it loses its counted column to a better
generator. Earnings therefore need a generator that keeps finding blind spots.
Uploading a new generator supersedes the hotkey's previous one. The previous
one's last round score keeps counting until the new one is scored.

## Privacy

Generator source, an open round's secret, seed, and dataset, probe seeds, and
case content are never served publicly or to agent miners. Validators receive
the round dataset only while they hold a round lease, at the same trust level
as today's private datasets. A round's secret, seed, and dataset digest become
public after `privacy_window_rounds`. Operators can download a closed round's
dataset in Backroom (artifact scope) to reproduce it.

## Feedback loop

Round cases scored `discriminating` are recorded as promotion candidates. Operators
can review them in Backroom and curate them into a future private benchmark
pool. A generator whose families keep earning can be adopted wholesale as a
DittoBench datagen family. That is the counterbalance at its strongest: a
generator's best output becomes the next bar the agents have to clear.
Promotion is manual in v1, so no generator can move agent emissions without an
operator ruling.

## Components and ownership

| Layer | Component | Responsibility |
|---|---|---|
| Contract | `research/dittobench-datagen/challenge` | Generator request and archive rules, pack schema, validation, round derivations and dataset, `ScoreCases`/`ScoreRun`/`ScoreRound`, golden vectors |
| Sandbox | `research/dittobench-datagen/genrun`, `cmd/challenge` | Package, build, and run a generator in the Docker sandbox (or locally); `rehearse` is the scorer path for miners |
| Scorer | `services/dittobench-api/cmd/challenge-scorer` | Qualification runs; round dataset generation; round matrix scoring |
| Round execution | `services/dittobench-api/cmd/dittobench-api` (`generator-round-v1`) | Run a round dataset against one screened agent image with ticket inference and the production grader |
| Platform | `apps/platform` | Signed uploads, private storage, settings, qualification, chain-epoch rounds (commit, open, close, reveal, on-chain commitment), validator round leases, copy detection, generator ledger |
| Validator | `ditto/validator` | Run round panel runs on idle slots; `generator` track in `SHADOW` at 0 bps folding the generator ledger |
| Backroom | `apps/backroom` | Read and drive every part of the lane: settings, submissions, qualification runs, rounds, leases, results, the matrix, the round driver; activation and debugging writes with revision and confirmation |
| Miner | `ditto generator`, `miners/dittobench-generator-starter-kit` | Rehearse, package, sign, and upload generators; reference generator to fork |

## Rollout

1. **Shadow (now).**
   - Settings `mode=shadow`. Register the public default generator and let it
     qualify. Set `validator_rounds_enabled=true`.
   - Every ledger entry is `weight_eligible: false` and the validator track is
     `SHADOW`, so nothing touches emissions. Round standings are published,
     not weighted.
2. **Compare.** Run the matrix in shadow alongside the canonical benchmark.
   Compare crown stability, copy and collusion signals, and how agents rank on
   the matrix versus the benchmark.
3. **Enforce.**
   - Operators set a generator track share through governance, and the ledger
     marks entries eligible. An upload fee is added at the same time.
   - Settings `mode=enforce` (confirmation `APPLY GENERATOR COMPETITION
     ENFORCE`) runs the lane exactly like shadow but marks each qualified miner
     generator's ledger entry `weight_eligible: true`. The public default
     generator never appears on the ledger.
   - The share is the revisioned Platform setting `track_shares_bps` (Backroom
     `set_track_share_settings`, confirmation spelling out the split, for
     example `APPLY TRACK SHARES memory=9000 generator=1000`). The scoring
     ledger serves it to validators only once every recently-live weight
     setter reports protocol 33, and an epoch pin freezes it.
   - Validators (protocol 33) pay the generator track its share, split by the
     generator rank curve, only to eligible entries. A generator share with no
     eligible entry falls back to the memory track, so either switch alone
     leaves weights byte-identical to the memory-only fold.
   - A new bench version draws its memory cases from the round matrix, so the
     agent competition is judged on the generators' cases.
