# DittoBench generator starter kit

This is the reference miner for the **DittoBench generator competition**, the
counterbalance to the agent competition. Agent miners are paid for harnesses
that answer DittoBench cases. Generator miners are paid for **generators**:
CPU-only programs whose cases the best agents still get wrong. Those cases must
be valid, solvable, and discriminating, on seeds nobody sees in advance.

The generator competition is **shadow-only** for now. Every ledger entry is
`weight_eligible=false`, and the validator registers the `generator` track at
0 bps. Generators are scored and published, but they do not move weights yet.

The full design is in
[`research/dittobench-datagen/docs/generator-competition.md`](../../research/dittobench-datagen/docs/generator-competition.md).

## How it works

1. **You submit source.** This kit is a deterministic Go program. It reads a
   `GenerateRequest` (a secret seed, your hotkey, and a case count) on stdin
   and writes a **challenge pack** to stdout. A pack is a memory haystack
   (conversation pairs across seeding waves) plus questions with deterministic
   gold answers. You upload the module source, and Platform keeps it private.
2. **The scorer runs it, offline.** The scorer builds your module with a pinned
   Go toolchain and runs it with:
   - no network and no model
   - 1 CPU and 1 GiB of memory
   - 60 seconds per seed

   It does this on fresh secret seeds plus a shared secret probe seed, then
   re-runs one seed to check that the output is byte-identical. Generation is
   pure CPU, so it is cheap.
3. **Validation.** Every pack must pass `challenge.ValidateGenerated`, which
   you can run locally:
   - The pack echoes the request's seed and hotkey and has exactly the
     requested number of cases.
   - Every accepted answer appears in the case's named evidence pairs.
   - No answer appears in the question.
   - The production v13 memory grader gives the gold answer full credit.
   - The grader rejects an empty answer, an abstention, an echo of the
     question, every distractor, and a dump of the user's entire memory.

   One failed seed, a crash, a timeout, or nondeterministic output rejects the
   upload.
4. **Panel.** The scorer runs each pack against the top agents, one per owner,
   and grades every answer with the production grader. Your own agents never
   count.
5. **Score.** Scoring is per case:
   - **Discriminating** cases earn credit: solved by at least two independent
     owners, but not by everyone.
   - **Trivial** cases (everyone solves) earn nothing.
   - **Unsolved** cases earn nothing and also cut that pack's credit by a
     squared factor.
   - **Duplicate** cases earn nothing. A case any earlier run already scored
     belongs to that run, and repeating a seed's cases on a later seed counts
     as a duplicate too.
   - Cases the champion fails earn a 1.5× bonus.

   The run score is the mean over seeds. Your generator is re-run on new seeds
   against the current top agents every 24 hours. Your ledger score is your
   latest scored run from the last 7 days. As agents improve, your families go
   trivial, so keep improving the generator.

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
  -hotkey "$MY_HOTKEY" -seeds 3 -cases 16
# The scorer's actual sandbox: pinned images, no network, the same limits (needs Docker).
go run ./cmd/challenge rehearse -dir ../../miners/dittobench-generator-starter-kit \
  -hotkey "$MY_HOTKEY" -sandbox docker -out-dir /tmp/packs

# Upload the source, signed by your hotkey. This packages the module with its
# dependencies vendored, without touching your checkout.
cd ../..
uv run ditto generator upload --dir miners/dittobench-generator-starter-kit \
  --wallet.name <coldkey> --wallet.hotkey <hotkey>
uv run ditto generator status --wallet.name <coldkey> --wallet.hotkey <hotkey>
uv run ditto generator board
```

`uv run ditto generator rehearse DIR` and `uv run ditto generator package DIR`
wrap the same tool.

## Rehearse against agents locally

Run three or more agent harnesses, for example the
[agent starter kit](../dittobench-starter-kit/) with different models or
settings, and score your generator against them:

```bash
cd services/dittobench-api
go run ./cmd/challenge-scorer run -dir ../../miners/dittobench-generator-starter-kit \
  -hotkey "$MY_HOTKEY" -seeds 2 -cases 16 \
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
| `families/pools.go` | Value pools (their sizes cap `MaxCases` at 32) |

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
  `TestEverySeedValidates` here checks 16 seeds at several pack sizes.
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
  repeats itself across seeds and reruns, and repeats are duplicates.
- **Measure, then keep what splits.** Rehearse against agents, read the
  verdicts, and drop families that come back all trivial or all unsolved.

## Validation

```bash
go vet ./... && go test ./...
```
