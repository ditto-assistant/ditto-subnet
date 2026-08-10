# dittobench-datagen

The deterministic dataset generator for [DittoBench](https://github.com/ditto-assistant/dittobench-starter-kit),
the benchmark for Bittensor Subnet 118. This is the exact code the validators use
to build the dataset every submission is scored against. It is published so the
benchmark is fully auditable.

## Why this is public

DittoBench grades AI-agent submissions against a freshly generated dataset. The
generator is fully deterministic and non-LLM. A given `(seed, bench_version)`
always produces the identical dataset, byte for byte. The seed for each scored
submission is derived from an on-chain block hash fixed after the miner commits
their submission, so it cannot be predicted or pre-computed against. That is what
makes it safe to open the generator without enabling overfitting.

With this repo you can:

- Regenerate any scored submission's dataset. Take the seed the platform
  published for a submission and reproduce the exact dataset, including answer
  keys, it was graded against.
- Re-grade independently, verifying a leaderboard score yourself instead of
  trusting the validators.
- Inspect the anti-overfit machinery: how cases, distractors, memory graphs, and
  tool fixtures are constructed.

Grading is deterministic and judge-free. The memory grader (`grade/`) is included
so you can re-score a published transcript yourself; the platform's
composite-scoring service and leaderboard are separate and not part of this repo.

## Install

```bash
go install github.com/ditto-assistant/dittobench-datagen/cmd/generate@latest
```

or build from source:

```bash
git clone https://github.com/ditto-assistant/dittobench-datagen
cd dittobench-datagen
go build ./cmd/generate
```

## Usage

```bash
# regenerate a submission's dataset from its published seed
generate -bench-version 3 -seed 123456789 -run-size full -out dataset.json

# print only the dataset_sha256 (the hash the platform pins and validators verify)
generate -bench-version 3 -seed 123456789 -run-size full -sha

# a fresh random seed (printed on stderr) for exploration
generate -bench-version 3 -run-size small
```

`generate` writes the canonical JSON artifact to stdout (or `-out`) and prints
`seed`, `run_size`, `bench_version`, and `dataset_sha256` to stderr. That
`dataset_sha256` is the same hash the platform commits to and a validator
re-derives. If it matches the one published for a submission, you are looking at
the identical dataset that submission was scored against.

### Review a generated dataset

Use the local dataset viewer to read the benchmark as a person rather than as a
JSON document:

```bash
go run ./cmd/datasetviewer -bench-version 8 -run-size full -seed 123456789
# open http://127.0.0.1:8787
```

The viewer serves the canonical artifact and its SHA-256, distinguishes
agent-visible text from reviewer-only oracle fields, and shows the exact planted
records for V8 shared-world questions. Cases and memory records can be flagged
with local notes and exported as JSON for generator follow-up. It binds only to
loopback; it is not a hosted benchmark or a source of harness-visible answer
metadata. V7 remains available for side-by-side historical inspection.

Run sizes: `small` (smoke), `medium`, `full` (the scored profile).

`-bench-version` is required. Use the version published with the score you are
auditing; never substitute the latest version. Explicit generation supports v2
through the pre-activation v9 contract. `CurrentBenchVersion` remains v8 while
the runtime advertises both executable contracts, v8 and v9; Platform rollout
state separately controls which one is active. For the public full-profile seed
`123456789`, the canonical SHA-256 vectors are:

| Version | Dataset epoch | SHA-256 |
| --- | --- | --- |
| 2 | `2026-01-01T00:00:00Z` | `dfb4fc243d7d3e84bb4e896d5873bbc9bda114e16f5215f913c13adbfbc4a7fe` |
| 3 | `2026-07-01T00:00:00Z` | `766183922b5a56725bdf44573fc31adf05355dc80fe9654d436935363fcdb3f2` |
| 4 | `2026-08-01T00:00:00Z` | `43e90780aa33505661047a2584381f6983875ac4a0eb85d46f83103389748b06` |
| 5 | `2026-09-01T00:00:00Z` | `ee70387b2470bb72a7ce457cd76187b9d89819016f3d58276f895a55b30a9f1c` |
| 6 | `2026-10-01T00:00:00Z` | `38a0df83a95bdad271f80a271d59d676509290e2fd762683abd960952ff84016` |
| 7 | `2026-11-01T00:00:00Z` | `f5f42f7a550e0bfef8ef2b14f810cbbd4b140ca5985e9f0cceaa509689d9e218` |
| 8 | `2026-12-01T00:00:00Z` | `6a09587706c95b5f61d3e65e0e34b317fc8ce24d0c927c66864d2869c8728e98` |
| 9 (pre-activation) | `2027-01-01T00:00:00Z` | `b12edd3649dece3af415ad289a24a1a8615b7d906773e718ee637da14cbd541f` |

Each is regenerated and asserted by CI (`TestV2KnownVector` and friends), so a
value here that disagrees with `cmd/generate` is a bug in this table, not in the
generator.

See [docs/bench-versions.md](docs/bench-versions.md) for what each contract is,
what changed in v4, and how module releases are versioned relative to it.

## Determinism guarantee

The same `(seed, bench_version)` always yields the same bytes. CI generates fixed
vectors twice and pins both the historical v2 bytes and the rotated v3 bytes. The
module has no external dependencies, only the Go standard library. Validators
must pin the exact immutable datagen module commit or release used by the scorer;
using `@latest` in production would make an old score impossible to audit.

The output is a function of `(seed, bench_version)`, not the seed alone. Each
version folds its number into the generation stream (`protocol.RotateSeed`), so
the rendered surface rotates when `bench_version` bumps. A harness that only
pattern-matches one version's rendered templates degrades on the next, while a
harness that actually reasons is unaffected. The rotation is public and
deterministic, so reproducibility is preserved: pin the seed and the version and
you get the same bytes every time.

## Layout

Every package is importable, so this module is the single source of truth for the
generator. The scoring and generation services depend on it rather
than keeping their own copies.

- `cmd/generate`: the CLI entry point.
- `cmd/generate-service`: the same generation behind HTTP
  (`POST /generate?seed=&run_size=&bench_version=2|3|4|5|6|7|8|9` → DatasetArtifact JSON +
  `X-Dataset-SHA256` and `X-Bench-Version` headers), with the
  `Dockerfile`/`cloudbuild.yaml` the SN118 platform deploys it
  from. The deployment is private (IAM-gated) so platform infrastructure cannot
  be farmed for generation, but there is no secret in the code — it computes
  exactly what `cmd/generate` computes for the same
  `(seed, run_size, bench_version)`.
  `bench_version` is mandatory for canonical calls. Its temporary omitted-value
  compatibility path emits v2 plus `Deprecation: true`, allowing an old platform
  deployment to coexist while the explicit version handshake rolls out.
- `cmd/graderaudit`: the grader false-negative audit. Given an artifact and a
  JSONL transcript dump it emits a labeling sheet of every memory case that
  survived the disqualifying scans but failed the typed answer check, plus
  per-answer-kind counts, so the grader's measured false-negative rate can be
  published per bench version.

- `cmd/gstudy`: the offline reliability analyzer. Given a JSONL of scored runs
  it reports a G-study variance decomposition (seed vs. item vs. residual) and
  per-category difficulty/discrimination estimates, flagging saturated and floor
  categories; pure analysis, no LLM.
- `cmd/vstudy`: deterministic multi-version generation and structural mix audit.
  `-bench-versions 8,9 -run-size full` compares the frozen v8 contract with the
  pre-activation v9 family mix and reports per-family minima, presence counts,
  means, variances, and distinct histograms. V9 strategy and G-study outputs are
  disabled until every emitted family has an explicit measured rate and source.
- `gen`: the generation pipeline (tool cases, memory suite, write-then-read
  lifecycle chains, isolation graphs, artifact assembly and hashing).
  `gen.GenerateDataset(seed, profile, benchVersion)` is the canonical entry
  point and rejects unsupported versions. The scorer must echo the selected
  version into its job/result and score-report details and must fail closed if
  the artifact version differs.
- `persona`, `datagen`: case content builders.
- `protocol`: the wire shapes, including the `DatasetArtifact` schema validators
  score.
- `toolexec`, `catalog`: the tool fixtures and tool catalog.

### Releasing the generation service

Every merge to `main` is interpreted from its conventional squash title by
the monorepo's semantic release. Release CI writes that version into the Go
provenance, creates the semver tag and GitHub release, verifies the exact tagged
source, then publishes the generator image with source tag/commit OCI labels.
Tags must not be created manually.

The same tagged source can be checked locally without publishing:

```bash
git fetch origin --tags
git switch --detach v0.11.2
scripts/verify-generate-service-release.sh
```

Release CI stages the immutable image on a zero-traffic Cloud Run revision,
performs an authenticated current-benchmark generation, and promotes only a
passing revision. Terraform owns the service shape but ignores later image
changes so an unrelated infrastructure apply cannot roll back a release.

The release job uses the standard organization release token. Image publication
uses a `prod` GitHub environment restricted to `main` and a dedicated WIF
identity that can write only to the datagen Artifact Registry repository and
the `ditto-datapipeline` Cloud Run service. Its `actAs` grant is scoped to that
service's one runtime identity.

## License

MIT. See [LICENSE](LICENSE).
