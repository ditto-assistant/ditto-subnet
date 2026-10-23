# DittoBench generator starter kit

This is the reference miner for the **DittoBench generator competition**, the
counterbalance to the agent competition. Agent miners are paid for harnesses
that answer DittoBench cases. Generator miners are paid for **cases that the
best agents still get wrong**: valid, solvable, and discriminating.

The generator competition is **shadow-only** for now. Every ledger entry is
`weight_eligible=false`, and the validator registers the `generator` track at
0 bps. Packs are scored and published, but they do not move weights yet.

The full design is in
[`research/dittobench-datagen/docs/generator-competition.md`](../../research/dittobench-datagen/docs/generator-competition.md).

## How it works

1. **Your program, your machine.** This kit is a deterministic Go program. For
   a seed and your hotkey it emits a **challenge pack**: a memory haystack
   (conversation pairs across seeding waves) plus 8–64 questions with
   deterministic gold answers. Only the pack, inert JSON, is uploaded. Your
   generator code stays private.
2. **Validation.** Before any agent sees the pack, the scorer runs the same
   `challenge.Validate` you can run locally. Each case must pass every check:
   - Every accepted answer appears in the case's named evidence pairs.
   - No answer appears in the question.
   - The production v13 memory grader gives the gold answer full credit.
   - The grader rejects an empty answer, an abstention, an echo of the
     question, every distractor, and a dump of the user's entire memory.
3. **Panel.** The scorer runs the pack against the top agents, one per owner,
   and grades every answer with the production grader. Your own agents never
   count.
4. **Score.** Scoring is per case:
   - **Discriminating** cases earn credit: solved by at least two independent
     owners, but not by everyone.
   - **Trivial** cases (everyone solves) earn nothing.
   - **Unsolved** cases earn nothing and also cut the whole pack's credit by a
     squared factor.
   - **Duplicate** cases earn nothing. A case another pack already scored
     belongs to that pack.
   - Cases the champion fails earn a 1.5× bonus.

   Your ledger score is your best pack scored in the last 7 days. As agents
   improve, old cases go trivial, so keep generating.

## Quick start

```bash
cd miners/dittobench-generator-starter-kit
go run . -seed 42 -hotkey "$MY_HOTKEY" -out pack.json

# validate exactly as the scorer will
(cd ../../research/dittobench-datagen && go run ./cmd/challenge validate "$OLDPWD/pack.json")

# upload, signed by your hotkey
uv run ditto generator upload pack.json --wallet.name <coldkey> --wallet.hotkey <hotkey>
uv run ditto generator status --wallet.name <coldkey> --wallet.hotkey <hotkey>
uv run ditto generator board
```

## Rehearse against agents locally

Run three or more agent harnesses, for example the
[agent starter kit](../dittobench-starter-kit/) with different models or
settings, and score your pack against them:

```bash
cd ../../services/dittobench-api
go run ./cmd/challenge-scorer run -pack ../../miners/dittobench-generator-starter-kit/pack.json \
  -agent a=http://127.0.0.1:7070,owner=alice,champion \
  -agent b=http://127.0.0.1:7071,owner=bob \
  -agent c=http://127.0.0.1:7072,owner=carol \
  -out panel.json
cd ../../research/dittobench-datagen
go run ./cmd/challenge score -pack ../../miners/dittobench-generator-starter-kit/pack.json \
  -panel ../../services/dittobench-api/panel.json
```

`challenge score` prints every case's status, so you can see which families
discriminate and which are trivial.

## Layout

| Path | What it is |
|---|---|
| `main.go` | Builds a pack from the family mix and refuses to write an invalid one |
| `families/builder.go` | Haystack builder: waves, pair IDs, timestamps, collision-free value picks |
| `families/families.go` | The case families and the `Default` mix |
| `families/pools.go` | Value pools |

The four starter families are:

- **`UpdateChain`**: a fact, a decoy, and a later correction. Current agents
  mostly solve it. It is a calibration control, not a scorer.
- **`TwoHop`**: the question names a role. The agent must first resolve who
  holds the role, then retrieve that person's city from a different session.
- **`RetractedPreference`**: a habit is retracted in a paraphrase with no
  cessation keyword.
- **`ScatteredList`**: list members are spread across waves, with a decoy
  member that belongs to someone else.

## Writing a family that earns

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
- **Measure, then keep what splits.** Rehearse locally, read `challenge score`,
  and drop families that come back all trivial or all unsolved.

## Validation

```bash
go vet ./... && go test ./...
```

`TestEverySeedValidates` checks 40 seeds at several pack sizes against the
contract validator. `TestQuestionsAreUnique` checks that no question repeats
within a pack.
