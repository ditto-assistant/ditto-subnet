# Generator competition scorer

`cmd/challenge-scorer` runs generator-competition challenge packs against a
panel of agent harnesses. `internal/challengerun` executes one pack against one
ready harness. It uses the scored path's runner (wave barrier,
`SeedForVersion`, `RunCaseWithTelemetry`) and grades with `scorer.GradeMemory`
at the pack's pinned grading bench version. The contract, validation rules, and
scoring formula are in
`research/dittobench-datagen/docs/generator-competition.md`.

## Wire hygiene

Case, pair, session, and user IDs are replaced with per-run opaque IDs keyed by
a random nonce. A harness cannot recognize a generator's ID scheme. Two runs of
the same pack look unrelated. A harness reused across packs answers each run
from that run's own memory graph.

A case whose `/run` fails is reported as `fault` and excluded from scoring. A
harness that never becomes healthy, or that rejects a seeding wave, faults
every case. Scorer-side failures therefore cannot produce "the champion failed
this case".

## Local rehearsal

```bash
go run ./cmd/challenge-scorer run -pack pack.json \
  -agent mine=http://127.0.0.1:7070,owner=me \
  -agent baseline=http://127.0.0.1:7071,owner=ops,champion \
  -agent other=http://127.0.0.1:7072,owner=other \
  -generator-owner me -out panel.json
cd ../../research/dittobench-datagen
go run ./cmd/challenge score -pack pack.json -panel panel.json
```

## Shadow scorer

```bash
DITTO_GENERATOR_SCORER_TOKEN=... go run ./cmd/challenge-scorer serve \
  -platform https://platform.example -harness-map panel-harnesses.json
```

The `serve` loop works through Platform one pack at a time:

1. It leases a pack from `GET /api/v1/generator-scorer/work`.
2. It refuses served bytes whose SHA-256 differs from the lease pin, then
   re-validates the pack.
3. It runs each panel agent that has an entry in the harness map.
4. It posts the validation report and per-agent results to
   `POST /api/v1/generator-scorer/results`.

Platform computes the score. The token is read from the environment, is sent
only as a Bearer header, and is never logged.

In v1 an operator provisions the panel harnesses. A panel agent missing from
the harness map is skipped. If too few agents remain, the pack scores as
`insufficient_panel` and is retried later. Launching panel agents from their
screened images through the DittoBench sandbox and inference broker is the next
rollout step.
