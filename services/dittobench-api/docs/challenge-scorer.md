# Generator competition scorer

`cmd/challenge-scorer` does the generator competition's CPU-only work. It
builds generator source in a no-network sandbox and runs it on seeds. It never
talks to a model, and in Platform mode it never runs an agent: validators run
round datasets on screened agent images through the ordinary ticket and
inference path (`generator-round-v1` on `/v2/score`, see
`cmd/dittobench-api/generator_round.go`).

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
  -hotkey 5Grw... -seeds 2 -cases 80 -sandbox docker \
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
  -platform https://platform.example
```

`-platform` must be an `https://` URL, because the scorer token is a bearer
credential. Use `-insecure-platform` only for a local stack. `-once` processes
at most one item of each kind and exits nonzero if either cycle failed.

Each `serve` cycle does at most one round work item, then at most one
qualification. Round work goes first because it gates the validators and the
ledger.

**Rounds** (`GET /api/v1/generator-scorer/round-work`):

- `kind=generate`: the work carries the round seed, the case count, and every
  frozen column's exact source.
  1. The scorer refuses any source whose SHA-256 differs from the column pin.
  2. It builds and runs each column generator on
     `challenge.RoundColumnSeed(seed, column)` and validates the pack.
  3. It encodes the valid packs with `challenge.EncodeRoundDataset`.
  4. It posts the column results and the dataset to `POST /round-results`.
- `kind=score`: the work carries the round matrix. That is each column's case
  refs and owner, every panel agent's validator results, and the fingerprints
  earlier rounds claimed. The scorer applies `challenge.ScoreRound` and posts
  column scores and pairwise-neutral agent standings to `POST /round-score`.

**Qualification** (`GET /api/v1/generator-scorer/work`):

1. The work carries a new submission's exact archive, fresh secret seeds, the
   current probe seed, and the case count.
2. The scorer builds the generator.
3. It runs each seed and the probe, then re-runs the first seed to check
   determinism.
4. It posts the evaluation to `POST /results`. Platform marks the generator
   qualified or rejects it, which includes the copy check.

The token is read from the environment, is sent only as a Bearer header, and is
never logged.

## Production deployment

The scorer runs on its own private VM, `ditto-generator-scorer-prod`, and
nowhere else, because it executes untrusted miner code. The Platform,
screener, and validator hosts never build a generator.

1. Protected Terraform apply with `enable_generator_scorer = true`
   (`infra/terraform/stacks/gcp-platform/generator-scorer.tf`). This creates
   the VM, its identity, and the `generator-scorer-token-prod` secret (which
   exists even with the flag off, since Platform names it). Both
   the scorer identity and the Platform identity can read the secret.
2. Add one secret version without printing it:
   `openssl rand -hex 32 | gcloud secrets versions add generator-scorer-token-prod --data-file=- --project=ditto-app-dev`.
3. Converge Platform (`infra/ansible/playbooks/gcp-platform-app.yml`) so that
   `DITTO_GENERATOR_SCORER_TOKEN` is rendered, then restart the API.
4. Converge the scorer at the released commit:
   `infra/ansible/playbooks/gcp-generator-scorer.yml -e generator_scorer_revision=<sha>`.
   This builds `challenge-scorer` with the base role's pinned Go toolchain,
   pre-pulls the pinned sandbox images, and runs `challenge-scorer serve`
   under systemd. The unit gets Docker access and only its own bearer.

The unit sets `TMPDIR` under `/var/lib/ditto-generator-scorer`, because the
sandbox bind-mounts its build directories. `PrivateTmp` would hide them from
dockerd.
