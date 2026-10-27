# DittoBench generator starter kit

This is the reference miner for the **DittoBench generator competition**, the
counterbalance to the agent competition. Agent miners are paid for harnesses
that answer DittoBench cases. Generator miners are paid for **generators**:
CPU-only programs whose cases the best agents still get wrong. Those cases must
be valid, solvable, and discriminating, on seeds nobody sees in advance.

The generator competition is **paid**. Since epoch 25962 (2026-10-26) the
generator track earns a governed share of emission: 15% of total emission,
next to 35% for the agent competition and 50% burn. Qualified generators' ledger
entries are `weight_eligible=true`, and validators split the generator share by
rank on the latest round score (40/25/15/12/8%, renormalized when fewer than
five generators qualify). The live split is in Platform's track-share settings
and may change by governance.

The full design is in
[`research/dittobench-datagen/docs/generator-competition.md`](../../research/dittobench-datagen/docs/generator-competition.md).

## How it works

1. **You submit source.** This kit is a deterministic Go program. It reads a
   `GenerateRequest` (a secret seed, your hotkey, and a case count) on stdin
   and writes a **challenge pack** to stdout. A pack is a memory haystack
   (conversation pairs across seeding waves) plus questions with deterministic
   gold answers. You upload the module source, and Platform keeps it private.
2. **The scorer qualifies it, offline.** The scorer builds your module with a
   pinned Go toolchain and runs it with:
   - no network and no model
   - 1 CPU and 1 GiB of memory
   - 60 seconds per seed

   It does this on fresh secret seeds plus a shared secret probe seed, then
   re-runs one seed to check that the output is byte-identical. Generation is
   pure CPU, so it is cheap.
3. **Validation.** Every pack must pass `challenge.ValidateGenerated`, which
   you can run locally:
   - The pack echoes the request's seed and hotkey and has exactly the
     requested number of cases: the competition's `cases_per_seed`, which
     Platform can set anywhere from 8 to 80 and may raise between rounds.
   - Every accepted answer appears in the case's named evidence pairs.
   - No answer appears in the question.
   - The production v13 memory grader gives the gold answer full credit.
   - The grader rejects an empty answer, an abstention, an echo of the
     question, every distractor, and a dump of the user's entire memory.

   One failed seed, a crash, a timeout, or nondeterministic output rejects the
   upload. A generator that passes is qualified. A qualified generator that
   later fails its round seed is rejected too, including when it cannot fill a
   raised `cases_per_seed`, so make sure yours fills 80 cases on every seed.
4. **Rounds.** Qualified generators compete in rounds on the chain's epoch
   counter (`epochs_per_round` subnet epochs per round; one epoch, about 72
   minutes, today). Each round:
   - opens on a seed nobody could know in advance. It comes from a secret
     Platform committed a round earlier, on Platform and on chain, and the
     block the round opened at.
   - freezes up to five counted generator columns (the best qualified
     generators, filled by a public default generator) plus a challenger
     column for new generators.
   - runs every column's cases, each column on its own seed, against the top
     agents on validators.

   The round's secret and seed are revealed six rounds later, so anyone can
   recompute every case.
5. **Score.** Scoring is per case:
   - **Discriminating** cases earn credit: solved by at least two independent
     owners, but not by everyone.
   - **Trivial** cases (everyone solves) earn nothing.
   - **Unsolved** cases earn nothing and also cut your column's credit by a
     squared factor.
   - **Duplicate** cases earn nothing. A case an earlier round already scored
     belongs to that round.
   - Cases the champion fails earn a 1.5× bonus.

   Your own agents never count on your column. Agents are ranked
   pairwise-neutral: two agents are compared only on columns neither owner
   owns, so your generator can never move your own agent. Your ledger score is
   your latest round column score from the last 7 days. As agents improve,
   your families go trivial and you lose your column to a better generator, so
   keep improving it.

Copying does not pay:
- An identical source tree is refused at upload.
- A generator that emits another owner's cases on the secret probe seed is
  rejected as a behavioral copy.
- An unmodified starter kit is accepted once, and its families are ones the top
  agents mostly solve.

## Quick start

```bash
cd research/dittobench-datagen
# Build and run exactly as the scorer does: three seeds, validation, determinism.
go run ./cmd/challenge rehearse -dir ../../miners/dittobench-generator-starter-kit \
  -hotkey "$MY_HOTKEY" -seeds 3 -cases 80
# The scorer's actual sandbox: pinned images, no network, the same limits (needs Docker).
go run ./cmd/challenge rehearse -dir ../../miners/dittobench-generator-starter-kit \
  -hotkey "$MY_HOTKEY" -sandbox docker -out-dir /tmp/packs

# Upload the source, signed by your hotkey. This packages the module with its
# dependencies vendored, without touching your checkout.
cd ../..
uv run ditto generator upload --dir miners/dittobench-generator-starter-kit \
  --name "my-generator" --wallet.name <coldkey> --wallet.hotkey <hotkey>
uv run ditto generator status --wallet.name <coldkey> --wallet.hotkey <hotkey>
uv run ditto generator board
```

`uv run ditto generator rehearse DIR` and `uv run ditto generator package DIR`
wrap the same tool.

`--name` is optional. It labels the generator on
[dittobench.ai/generators](https://dittobench.ai/generators), next to your
miner profile picture (the one set with `ditto avatar set`, shown on the agent
leaderboard too). Names follow the agent-name rules: 1-64 characters, no
control characters, and no handle another miner family has reserved. The
upload checks the name before paying anything. An unnamed generator shows a
short submission id.

When the operator requires an upload fee, `ditto generator upload` charges the
generator competition's own TAO fee (the agent-upload fee unless the operator
sets a separate generator price). It prints the generator submission terms
(archive limits, cooldown, fee), shows the amount and asks before paying
(`-y` skips the prompt), then attaches the payment proof. If the upload fails
after payment, it prints `--payment-block-*` flags. Rerun with them within 24
hours to reuse the payment without paying again. See "Upload fee" in
[generator-competition.md](../../research/dittobench-datagen/docs/generator-competition.md).

## Grading policies (v13 and v14)

Every pack declares the memory grading policy its cases are graded under in
`grading_bench_version`. A v13 round's request accepts only 13 and is
byte-identical to the original contract. A round frozen at v14 adds
`"grading_bench_versions": [13, 14]`, and the pack may declare either. Each
column is graded at the policy its pack declares, so v13 and v14 columns run
side by side in a v14 round.

The v14 policy reads the agent's reply as rendered text: markdown emphasis,
code, links, headings, bullets and table pipes are stripped before matching,
numbers end at sentence punctuation ("4471," names 4471), and "15%" reads as
"15 percent" for percentage quantities. A v14 pack must also pass the v14
answer-text rule (`v14_rendered_text`): state every answer, alternative,
distractor and dump-guard entry as plain text, never as `**bold**`,
`` `code` `` or a link.

Kit version 4 declares the newest policy the request accepts
(`req.NewestAccepted(GradingBenchVersions)`). Asked for v13 it emits exactly the
version 3 pack apart from the version string. Rehearse a v14 round with
`-round-bench 14`:

```bash
go run ./cmd/challenge rehearse -dir ../../miners/dittobench-generator-starter-kit \
  -hotkey "$MY_HOTKEY" -seeds 3 -cases 80 -round-bench 14
```

### Migrating a fork to v14

You do not have to migrate. A generator built before v14 ignores the new
request field, emits v13, and keeps its columns in v14 rounds, graded at v13.
If its decoder rejects unknown fields, or its v14 path fails, the scorer
retries that seed once on the original v13 request (recorded as
`legacy_retry`) instead of rejecting the generator. To declare v14:

1. Pull this repository and rebuild against the current
   `research/dittobench-datagen` (the upload vendors it), so your copy of the
   `challenge` package knows the v14 policy and rule.
2. Set the pack's `grading_bench_version` to
   `req.NewestAccepted(yourSupportedPolicies)` after building it, as `main.go`
   does. Keep calling `req.Validate()`.
3. Self-check with `challenge.ValidateGenerated(raw, req)` (already in
   `run`), then rehearse with `-round-bench 13` and `-round-bench 14`. Both
   must pass, and the v13 output should not change.
4. If you grade your own cases locally, use `Pack.GradingCase(i)` (or
   `Case.GradingCaseAt(pack.GradingBenchVersion)`); `Case.GradingCase()` is
   always the v13 baseline.
5. Re-upload. Declaring v14 is worth it only where the v14 policy changes
   what your cases measure; it never earns credit by itself.

### Support window

A round accepts packs at its own bench version and the one before it. v14
rounds accept v13 and v14 packs; v15 rounds will accept v14 and v15 only, so a
generator that still emits v13 loses its v15 columns (announced at least 14
days before v15 activates). Rebuilding against the current
`research/dittobench-datagen` is enough: from this release on, the `challenge`
package ignores grading policies it does not know yet, so a rebuilt generator
keeps working when a newer round offers one, as long as the request still
accepts a policy it supports. A generator built before this release must
rebuild before v15 rounds.

### How this kit improves

Every starter kit is improved continuously. An improvement learned from a
miner's submission ships here only after that submission's source has been
made public under the subnet's source-disclosure policy (Backroom
`get_source_release_policy`). Generator source has no public release path yet,
so nothing derived from a miner generator ships in this kit until it does.

## Rehearse against agents locally

Run three or more agent harnesses, for example the
[agent starter kit](../dittobench-starter-kit/) with different models or
settings, and score your generator against them:

```bash
cd services/dittobench-api
go run ./cmd/challenge-scorer run -dir ../../miners/dittobench-generator-starter-kit \
  -hotkey "$MY_HOTKEY" -seeds 2 -cases 80 \
  -agent a=http://127.0.0.1:7070,owner=alice,champion \
  -agent b=http://127.0.0.1:7071,owner=bob \
  -agent c=http://127.0.0.1:7072,owner=carol \
  -out run.json
```

`run.json` holds every case's status per seed, so you can see which families
discriminate and which are trivial.

## Layout

| Path | What it is |
|---|---|
| `main.go` | Reads the request, builds the pack from the family mix, and refuses to write an invalid one |
| `families/builder.go` | Haystack builder: waves, pair IDs, timestamps, collision-free value picks, all seeded by the request |
| `families/families.go` | The case families and the `Default` mix |
| `families/pools.go` | Value pools (sized so `MaxCases` reaches the contract's 80) |

The four starter families are:

- **`UpdateChain`**: a fact, a decoy, and a later correction. Current agents
  mostly solve it. It is a calibration control, not a scorer.
- **`TwoHop`**: the question names a role. The agent must first resolve who
  holds the role, then retrieve that person's city from a different session.
- **`RetractedPreference`**: a habit is retracted in a paraphrase with no
  cessation keyword.
- **`ScatteredList`**: list members are spread across waves, with a decoy
  member that belongs to someone else.

## Writing a generator that earns

- **Use only the request seed.** Derive all randomness from
  `req.Rand()` or `req.Seed`. Never read the clock or the environment, and never
  range over a Go map into your output. Any of these breaks the determinism
  check.
- **Work on every seed.** The scorer picks seeds you never saw. Test many seeds:
  `TestEverySizeFills` here fills every pack size from 8 to 80 on 16 seeds,
  and `TestEverySeedValidates` validates the sizes up to the cap.
- **Ground everything.** In v1 every accepted answer form must appear in the
  evidence text. Hard cases come from resolution, joins, supersession,
  aggregation, and interference, not from answers the haystack never states.
  Computed answers such as sums and date differences are a planned v2
  contract extension.
- **Plant interference.** Every case needs a decoy that is itself in memory: a
  same-type value belonging to another entity, an earlier superseded value,
  or a near-miss. Without one, the "dump all of memory" probe passes and the
  validator rejects the case.
- **Never be ambiguous.** Two cases must not ask the same question about the
  same memory, and a question must have exactly one right answer given the
  whole haystack. The validator catches identical questions. Only the panel
  catches subtler ambiguity, as cases no two owners solve, and those cost you
  the squared penalty.
- **Fill a wide space.** A family that can only produce a few distinct cases
  repeats itself across seeds and rounds, and repeats are duplicates.
- **Measure, then keep what splits.** Rehearse against agents, read the
  verdicts, and drop families that come back all trivial or all unsolved.

## Validation

```bash
go vet ./... && go test ./...
```
