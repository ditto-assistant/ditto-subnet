# Generator competition scorer

`cmd/challenge-scorer` evaluates generator-competition submissions. It builds a
generator's source in a no-network sandbox, runs it on seeds, and runs every
resulting challenge pack against a panel of agent harnesses.

- `genrun` (in `research/dittobench-datagen`) owns the generator half: build,
  run, validate, and check determinism.
- `internal/challengerun` executes one pack against one ready harness. It uses
  the scored path's runner (wave barrier, `SeedForVersion`,
  `RunCaseWithTelemetry`) and grades with `scorer.GradeMemory` at the pack's
  pinned grading bench version.

The contract, sandbox limits, validation rules, and scoring formula are in
`research/dittobench-datagen/docs/generator-competition.md`.

## Sandbox

`serve` uses `genrun.Docker`, so the scorer host needs a Docker daemon. Builds
run in the pinned `golang:1.27.3-bookworm` image. Generation runs in pinned
distroless `static`. Both run with no network, a read-only root filesystem, all
capabilities dropped, an unprivileged user, and memory, CPU, process, and
wall-time limits.

A timed-out container is killed by name, so no container outlives its run.
Docker CLI failures (exit 125–127, daemon errors) are sandbox failures. They are
never reported to Platform as the generator's fault: the scorer logs them and
lets the lease expire.

`-unsafe-local-sandbox` builds and runs on the host with no isolation. It
exists for local stacks only.

## Wire hygiene

Case, pair, session, and user IDs are replaced with per-run opaque IDs keyed by
a random nonce. A harness cannot recognize a generator's ID scheme. Two runs
look unrelated. A harness reused across packs answers each run from that run's
own memory graph.

A case whose `/run` fails is reported as `fault` and excluded from scoring. A
harness that never becomes healthy, or that rejects a seeding wave, faults
every case. Scorer-side failures therefore cannot produce "the champion failed
this case".

## Local rehearsal

```bash
go run ./cmd/challenge-scorer run -dir ../../miners/dittobench-generator-starter-kit \
  -hotkey 5Grw... -seeds 2 -cases 16 -sandbox docker \
  -agent mine=http://127.0.0.1:7070,owner=me \
  -agent baseline=http://127.0.0.1:7071,owner=ops,champion \
  -agent other=http://127.0.0.1:7072,owner=other \
  -generator-owner me -out run.json
```

Generators build and run in the Docker sandbox by default; `-sandbox local`
runs the generator's code directly on your machine, so use it only for your own
source. `run` rejects two `-agent` flags with the same id. It prints the run
score with the default parameters. It writes the full
verdict file: a `challenge.RunInput`, the params, and the `challenge.RunScore`.
Use `-archive` to rehearse an exact upload, or `-pack` to run a single pack file
instead of a generator.

## Shadow scorer

```bash
DITTO_GENERATOR_SCORER_TOKEN=... go run ./cmd/challenge-scorer serve \
  -platform https://platform.example -harness-map panel-harnesses.json
```

`-platform` must be an `https://` URL, because the scorer token is a bearer
credential. Use `-insecure-platform` only for a local stack. `-once` processes
at most one item and exits nonzero if that cycle failed.

The `serve` loop works through Platform one run at a time:

1. It leases a run from `GET /api/v1/generator-scorer/work`. The work carries
   the exact source archive, fresh secret seeds, the current probe seed, the
   case count, and a panel snapshot.
2. It refuses served bytes whose SHA-256 differs from the lease pin.
3. It evaluates the generator: build, each seed, the probe, and a determinism
   re-run.
4. If the generator is valid, it runs each seed's pack against each panel agent
   that has an entry in the harness map.
5. It posts the evaluation and per-agent results to
   `POST /api/v1/generator-scorer/results`.

Platform computes the score and runs the copy check. The token is read from the
environment, is sent only as a Bearer header, and is never logged.

In v1 an operator provisions the panel harnesses. A panel agent missing from
the harness map is skipped. If too few agents remain, the run scores as
`insufficient_panel` and is retried later on new seeds. Launching panel agents
from their screened images through the DittoBench sandbox and inference broker
is the next rollout step.
