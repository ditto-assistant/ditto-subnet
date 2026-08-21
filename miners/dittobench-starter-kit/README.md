# DittoBench miner starter kit

An agent and memory harness for DittoBench, the benchmark on
Bittensor subnet 118 (SN118). Miners run an agent that the validator probes
with tool-calling and memory-recall cases. You earn by being more correct
than other miners. Latency is reported but not scored. A case that exceeds
its per-case timeout scores 0.

## Ditto SN118 repositories

| Repository | Purpose |
| --- | --- |
| [`ditto-subnet`](https://github.com/ditto-assistant/ditto-subnet) | Canonical miner, validator, scoring, screening, and operations monorepo |
| [`ditto-harness`](https://github.com/ditto-assistant/ditto-harness) | Reference memory and agent harness library |
| [`miners/dittobench-starter-kit`](.) | Miner starter project and local practice loop (this directory) |
| [`services/dittobench-api`](../../services/dittobench-api) | Validator scoring engine and hosted practice API |
| [`dittobench-datagen`](https://github.com/ditto-assistant/dittobench-datagen) | Deterministic dataset generator and judge-free grader |

This directory is the maintained **Rust reference implementation**, not a Rust
requirement. Competition admission is language-neutral: submit any project that
has a `Dockerfile` at the tarball root and serves the documented HTTP contract.
Python, TypeScript/JavaScript, Go, Java, C/C++, or any other containerized
implementation is equally valid. The screener builds the image once; validators
load and run that exact content-addressed image.

The kit is a working baseline plus the full local eval loop (tool calling +
memory + speed) running locally against an embedded Turso (SQLite-family)
database with native vector search inside the
[ditto-harness](https://github.com/ditto-assistant/ditto-harness) crate.
OpenRouter-backed models inherit its app attribution:
`HTTP-Referer: https://heyditto.ai` and `X-OpenRouter-Title: Ditto`.

It mirrors Ditto's production memory retrieval pipeline and ships the
real ranking models as weights:

1. Vector candidate pool over the seeded memories (cosine on 768-dim embeddings).
2. Composite scoring (V2): 7 signals (semantic, linear + exponential recency,
  subject frequency, subject semantic match, session continuity, neighbor density)
   fused by weights from a weight-predictor MLP (`fixtures/models/mlp-weights.bin`,
   the production architecture retrained on embeddinggemma, which predicts the fusion
   weights + scale from the query embedding + 17 aux features).
3. Cross-encoder rerank: a TinyBERT-L2 cross-encoder
  (`fixtures/models/cross-encoder.onnx`, ONNX via `ort`) reranks the top-20 pool
   and fuses with composite rank via Reciprocal Rank Fusion (k=60, ceWeight=0.7).

It also ships a self-contained seed user: a coherent slice of LongMemEval
(a public long-term-memory QA benchmark) with subjects already synced.

## How this fits together

There are two codebases, and it helps to be exact about the split.

- `miners/dittobench-starter-kit` (this directory) is the Rust reference you can submit. You edit it, then
  run `cargo run -- submit` to package the whole crate as a tarball. The
  screener builds that tarball and validators score the resulting exact image.
  This kit is both a local test rig and an example submission. A different
  language may implement the same container/HTTP contract without this crate.
- ditto-harness is a dependency, not a copy inside this directory. It is Ditto's
  production memory and agent engine, pinned to one commit in `Cargo.toml`. Cargo
  downloads it when you build. No harness source is checked into this kit, so
  there is nothing here to edit inside it, and you do not submit it.

The dividing line: ditto-harness knows nothing about DittoBench, the
validator, or scoring. It is a generic memory and agent library, the same one
Ditto runs in production. Every benchmark-aware line is in this kit.

- The engine (ditto-harness): how memories are stored, retrieved, ranked, and
  reasoned over. The memory store and vector database, the retrieval and ranking
  pipeline, the agent loop, and the model and embedder clients. It exposes its
  pieces as slots: it takes an embedder, a ranking-weight predictor, and a
  reranker, and does not care where they come from.
- The kit (this directory): everything benchmark-specific, plus your work. It fills
  those slots with concrete pieces and your weights (`src/baseline.rs`), speaks
  the validator protocol (`/health`, `/seed`, `/run`), ships the tool catalog and
  the seed user, runs the local practice loop, and is what you customize and
  submit.

So the loop is: edit this kit, submit this kit. You tune the engine by what you
hand it from the kit (your prompt, your reranker, your weights, your retrieval
config), including the retrieval algorithm itself, which moves your score most.
You rarely edit ditto-harness directly; see Changing the retrieval
algorithm for how far the kit reaches and the one case that needs a fork.

`src/baseline.rs` is the natural entry point most changes start from. It wires
the pieces together (database, embedder, model, retrieval, tools) and is marked
with `EXTENSION POINT` comments where you plug in your work. It is a starting
point, not a boundary: nothing restricts you to this one file. You can add your
own modules, edit any other file, pull in new crate dependencies, and change the
`Dockerfile`. The only hard requirement is that `docker build .` succeeds and
the resulting image serves the protocol (see *What you're free to change* and
*The fixed interface*).

## What you optimize

This is what miners optimize, and it is the only thing that moves your score. You
make the harness remember and act better. These levers are wired in
`src/baseline.rs`, which is where most work starts, but you are not confined to
that file: you can edit, add, or restructure any file in the crate and submit the
whole thing (see *What you're free to change*). The three levers:

1. Memory retrieval: from a user's history, find the exact past facts that answer
   a question. This is the harder half of the score.
2. Tool use: pick the right tool for a request, and no tool when none fits.
3. Orchestration: the prompt and control flow that turn a question into a correct
   answer.

See How to optimize for the exact knobs, Per-question-type levers for the
mechanism behind each scored question type, and Changing the retrieval algorithm
for how far the retrieval lever reaches.

Two things are held constant on purpose and never move your score: the model (the
validator serves one frozen model and ignores yours) and latency (measured, not
scored). See What isn't scored, and why. You do not compete on model choice or
hardware.

Why this is the competition: Ditto's product is memory, an assistant that recalls
what you told it across sessions. DittoBench scores that exact capability on
Ditto's real production retrieval stack, not a stand-in, so a harness that scores
higher is a better memory system, and strong work can flow back
into the product. The composite score is half tool accuracy, half memory recall.

## Your workflow

The loop is: edit this kit, test locally, submit this kit. In order:

1. Edit `src/baseline.rs`: change the prompt, the retrieval config, the reranker,
   the ranking weights, or the tools (the levers above).
2. Test retrieval, fast and offline: `cargo run -- mem-eval --k 10` reports
   recall@k with no LLM (needs only Ollama for embeddings). Run this after any
   change to retrieval, weights, or the reranker.
3. Test the full agent: `cargo run -- evaluate` (fixed inputs, best for
   iterating) or `cargo run -- practice` (rotating inputs). Watch the composite,
   the per-category tool means, and the slowest cases. Run this after any change
   to the prompt or tools.
4. Rehearse through the real generator/scorer (recommended before you
   submit): `uv run ditto practice --run-size small` from the monorepo root. One command
   builds and starts your harness plus the local validator, supplies a
   validator-visible `tool_endpoint`, runs fresh live-bench (currently 11) data including staged
   seeding, prints observed/capped tool counts, and tears both down. Move to
   `medium` or `full` when the smoke run is healthy. `--run-size full` is the
   on-chain-shaped envelope; it still uses your `.env` model. Coding agents:
   load `/mine`.
5. Package: `cargo run -- submit` builds `dittobench-submission.tgz` from your
   whole crate.
6. Go on-chain: register a hotkey on netuid 118 and upload with the eval fee. The
   screener builds your image and validators score it under the model lock. See
   Mining on SN118.

You never leave this directory for any of it. ditto-harness is fetched automatically
when you build.

## Contents


| File                          | What it is                                                                                                  |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `src/baseline.rs`             | The agent you optimize. Wires DB + embedder + model + MLP predictor + reranker + harness.               |
| `src/reranker.rs`             | ONNX cross-encoder reranker, the production rerank stage, 1:1.                                             |
| `src/seed.rs`                 | Loads the bundled LongMemEval seed user into the vector DB.                                                 |
| `src/protocol.rs`             | The validator HTTP wire contract (see `PROTOCOL.md`).                                                       |
| `src/catalog.rs`              | The Ditto tool catalog presented per case.                                                                  |
| `src/datagen.rs`              | Deterministic-per-seed dataset generator.                                                                   |
| `src/scorer.rs`               | Local score report (tool accuracy + memory + latency).                                                      |
| `src/bin/dittobench-miner.rs` | CLI: `serve`, `playground`, `seed-user`, `mem-eval`, `evaluate`, `practice`, `submit`.                      |
| `scripts/local-rehearsal.py`  | One-command local v9 generator/scorer run with optional LongMemEval-S.                                      |
| `fixtures/seed-user/`         | The seed user: pairs + pre-synced subjects + subject graph + LongMemEval questions.                         |
| `fixtures/models/`            | Shipped weights: `mlp-weights.bin` (217K-param MLP) + `cross-encoder.onnx` (TinyBERT-L2 INT8) + BERT vocab. |
| `scripts/build-seed-user.py`  | Regenerates the seed-user slice from the LongMemEval fixture (maintainers only, inputs not distributed).   |




## Quickstart

> [SETUP.md](SETUP.md) is the step-by-step guide for
> setting up this kit with `ditto-harness` (the crate dependency), including
> Ollama and `.env`.

```bash
# 1. Pick a chat model for LOCAL practice. Default provider is OpenRouter and
#    the default model is openai/gpt-oss-20b, matching benchmark v8. Canonical
#    scoring overrides local model settings and serves the same locked model
#    through ticket-scoped platform inference.
export OPENROUTER_API_KEY=sk-or-...
# (optional) export DITTOBENCH_MODEL=<any OpenRouter model id>

#    ...or run fully locally with Ollama (edit the same .env file):
# DITTOBENCH_PROVIDER=ollama
# DITTOBENCH_MODEL=gpt-oss:20b
# ollama pull gpt-oss:20b
# ollama pull embeddinggemma
# cargo run -- ollama-check

# 2. Embeddings use Ollama's embeddinggemma (768-dim) by default. For memory
#    cases you need it running locally:
#       ollama serve
#       ollama pull embeddinggemma

# 3. Load the seed user (one-time, embeds pairs + subjects), then practice.
cargo run -- seed-user              # load the LongMemEval seed user
cargo run -- mem-eval --k 10        # retrieval recall over the seed user (no LLM)
cargo run -- evaluate               # FIXED local submission test (static user + same questions)
cargo run -- practice --n 20        # ROTATING random dataset (anti-overfit, like the hosted validator)

# Run the actual local v9 generator + staged seeding + observed-tool scorer.
# This starts and stops both the harness and local scorer for you.
uv run ditto practice --run-size small

# 4. Serve the harness for the validator.
cargo run -- serve --port 8080
```

Local Ollama uses the canonical `gpt-oss:20b` chat model and `embeddinggemma`
embedder and requires no provider credential. Scored v8 runs use
the validator-injected, ticket-scoped platform inference and hosted embedding
routes; Ollama is not a scored fallback.



## Playground (talk to the agent)

The interactive playground is a chat UI wired to a production-Ditto agent:
the v2 system prompt + persona + tool-use policy, the model set by
`DITTOBENCH_MODEL` (`.env.example` ships `openai/gpt-oss-20b`, the v8 scored model; set
`google/gemini-3.1-flash-lite` to mirror prod Ditto's model), the full tool
catalog, and real memory retrieval + cross-encoder rerank over the seed user. Action tools
(search_web, create_image, agent jobs, settings, …) return fake-but-plausible
results so you can exercise tool-calling without real integrations. Memory
tools are real and query the seed user.

```bash
cp .env.example .env        # paste your OPENROUTER_API_KEY into .env
cargo run -- seed-user      # one-time: load the dummy seed user
cargo run -- playground     # open http://127.0.0.1:8088
```

The UI shows the full tool catalog (every tool's description + JSON schema),
and after each turn a trace of the tool calls (args + fake results) and
the memories retrieved for that query. Try *"search the web for…"*
(`search_web` fires) or *"how many postcards have I collected?"* (memory
retrieval answers with `ditto://memory/…` citations). The Submit tab rehearses
your harness against the official hosted service. See *Hosted rehearsal*
below.

### Choose the right practice loop

- `evaluate` (local, fixed): scores your submission against the same inputs every run: the static seed user, the same bundled LongMemEval questions, and a fixed-seed tool set. Inputs are reproducible and model output is still stochastic.
- `practice` (local, rotating): re-rolls prompts per run, but from a small fixed template pool (10 memory facts). It varies wording, not substance, and never exercises the seeding tiers/waves.
- `uv run ditto practice` (local, live-bench path, default `--run-size small`): starts your harness and the monorepo's scorer together, generates a fresh live-bench (currently 11) dataset, exercises staged seeding and graph isolation for the selected run size, and gives the harness a reachable validator-owned `tool_endpoint`. `--run-size full` is the on-chain-shaped envelope. Use this when debugging tool arguments, order, result use, or `observed_tool_cases` / `capped_tool_cases`. `cargo run -- evaluate` is a different name-only scorer.
- Hosted rehearsal (remote): generates a fresh v8 dataset and exercises the seeding/question mix, but a publicly tunneled harness cannot reach a loopback-only tool endpoint on the hosted scorer. Observable tool cases are therefore self-report-capped. Use it for reachability and hosted orchestration, not for an exact tool score or screening/submission certification.
- On-chain scoring: runs the screened image with locked, ticket-bound chat and Perplexity embedding routes. Neither local nor hosted rehearsal proves that boundary.

Use `evaluate` to develop.

#### One-command local live-bench practice

From the monorepo root, the supported miner CLI starts every local component:

```bash
uv run ditto practice --run-size small
```

The command requires Rust and Go, plus the same local provider setup as
`serve`: Ollama `embeddinggemma` is required for memory, and chat uses the
provider selected in `.env`. It builds both binaries, uses an isolated temporary
database, selects free loopback ports, submits a live-bench (currently 11) run,
displays progress and the final score/provenance counts, and cleans up on exit.
`--run-size small` is a smoke profile. `--run-size full` is the on-chain-shaped
envelope.

Useful variants:

```bash
# Reproduce the same generated dataset.
uv run ditto practice --run-size small --seed 12345

# Exercise Tier B/C seeding and isolation more deeply; expect a longer run.
uv run ditto practice --run-size medium

# On-chain-shaped envelope (hours). Still local .env inference.
uv run ditto practice --run-size full --report /tmp/dittobench-report.json

# Keep the complete run envelope for comparing changes.
uv run ditto practice --run-size small --report /tmp/dittobench-report.json

# Append the separate, official 500-question LongMemEval-S adapter score.
uv run ditto practice --run-size small --longmem-eval

# One-question adapter/judge smoke (explicitly labeled partial practice).
uv run ditto practice --run-size small --longmem-eval --longmem-limit 1
```

This runs the real public generator and scorer, including observed tool
execution. It is still a rehearsal: the harness uses your `.env` chat and
embedding configuration, runs as a local process rather than a screened
container, and does not receive canonical ticket-bound inference.

`--longmem-eval` downloads and SHA-verifies the pinned cleaned LongMemEval-S
dataset, starts isolated harness stores plus trusted local GPT-4.1 reader and
official GPT-4o judge proxies, and runs the unmodified evaluator from the pinned
upstream revision. Export `OPENROUTER_API_KEY` for that option; the key remains
in the trusted proxy processes and is removed from build, harness, scorer, and
judge-container environments. The LongMemEval accuracy is reported separately
and never changes the Bench composite.

### Hosted rehearsal

The hosted practice service is available. The playground's Submit tab drives it:

1. Serve your harness and expose it publicly so the validator can reach it:
  `cargo run -- serve --port 8080`, then e.g. `ngrok http 8080`.
2. Set `DITTOBENCH_HARNESS_URL` in `.env` to the public URL.
  [.env.example](.env.example) ships the official `DITTOBENCH_API_URL`.
3. `cargo run -- playground` → open the Submit tab and pick a run size.
  This harness continues to use its own local model and embedding configuration;
  the playground sends no provider key.

The hosted service intentionally does not advertise its loopback tool endpoint
to a remote `harness_url`. Its report can therefore show many
`capped_tool_cases` even when the harness self-reports tool calls. That result
means the tool execution was not validator-observed; it does not mean setting a
client environment variable can certify it. `DITTOBENCH_REQUIRE_OBSERVED_TOOLS`
is not a starter-kit or hosted-API setting. Run the one-command local rehearsal
above to put the harness and observer on the same host, then check both
`observed_tool_cases` and `capped_tool_cases` in the report.

The active submission target is the uploaded tarball. Hosted `harness_url`
practice can drive any already-running implementation. Production v9 validators
never build miner repositories and never provide a GitHub credential to a build.

`seed-user` and `mem-eval` need only Ollama (`embeddinggemma`). No chat model
or API key is required. `mem-eval` runs the full
production pipeline (MLP weights + composite V2 + cross-encoder rerank) and
reports `recall@k` per LongMemEval question type, isolating retrieval quality
from the LLM. Keep the same `DITTOBENCH_DB` across `seed-user` and `mem-eval`.

`cargo build` and `cargo test` need no model or embedder, but the first
build needs network (the git dependency fetch, and the `ort` crate downloads
ONNX Runtime). Rebuilds are offline. Only `practice`/`serve` call out
to the model + Ollama at runtime.

## The harness contract

The validator calls `POST /run` with a `RunRequest` (system prompt, user
input, available tools) and expects a `RunResponse` (final text, observed tool
calls, token usage, latency). Before memory questions it installs a haystack via
`POST /seed`. Full shapes in [PROTOCOL.md](PROTOCOL.md).

The `system_prompt` field is an input, not a fixed prompt imposed on you. Your
harness decides what actually reaches the model: keep it, extend it with your own
tool-use and abstention policy, or replace it entirely. The prompt the agent runs
on is yours to control, the same as the retrieval config and the tools. The stock
baseline layers the production Ditto system prompt on top of this field; shaping
that is one of the three levers below.

### Test a seed-capable submission with a Ditto memory export

The local submission lab validates a signed Ditto Memory Passport, converts it
to staged `/seed` requests, then puts a multi-turn chat UI in front of a
reviewed submission's `/run` endpoint. The target must implement `GET /health`,
`POST /seed`, and `POST /run`. The authoritative contract in
`dittobench-api/pkg/protocol/protocol.go` defines `/seed` and the optional
`RunRequest.user_id` used for graph isolation, mirrored in this kit's
[`PROTOCOL.md`](PROTOCOL.md) and baseline server. Use a reviewed adapter for an
older submission that predates those fields.

#### Reviewer workbench: drop a tarball and chat

For the fastest manual review, start the browser workbench from a shell that
already has the desired provider key:

```bash
python3 scripts/submission-workbench.py
```

It opens `http://127.0.0.1:4320`. Drop the reviewed submission tarball, choose
OpenRouter or local Ollama, acknowledge local source execution, and
click **Build, load & chat**. The Ditto Memory Passport ZIP is optional: omit it
for a genuinely fresh blank-memory chat, or add it to experience the harness
against real Ditto memories. The same page shows preparation, safe extraction,
the Rust reference's Cargo release build, harness health, and memory
initialization progress before
turning into a multi-turn chat. **Review another tarball** stops the current
harness, removes its temporary files, and resets the page for the next
submission.

OpenRouter appears as available only when `OPENROUTER_API_KEY` is present in
the workbench process. Only the selected provider's key is passed to the child.
When a Passport is present, the default
quick-chat mode verifies the entire signed export but seeds only the first 100
seedable conversations so a reviewer can reach chat quickly; select **Full
export** for compatibility and long-tail retrieval testing. Neither mode
changes the original export. Without a Passport, the workbench sends an empty
`/seed` wave to create a fresh isolated user namespace before enabling chat.

The current browser workbench is a convenience for this Rust reference; other
languages can use the lower-level lab with a custom start command or call the
same `/health`, `/seed`, and `/run` endpoints directly. The workbench binds to
loopback, stores uploads in a private temporary
directory, displays the uploaded tarball's SHA-256, and never exposes memory
contents or counts in its status API. It is still intentionally **not a code
sandbox**: the acknowledged submission runs as your local account. Use a
disposable VM or container for hostile or unreviewed code.

The lower-level lab remains available for CI, digest-pinned runs, custom start
commands, already-running harnesses, and Passport-only validation.

Validate the export before exposing its contents to a harness. By default the
lab verifies both the archive's Ed25519 signature and the signing key against
Ditto's production API key endpoint (`https://api.heyditto.ai`):

```bash
python3 scripts/submission-lab.py \
  --passport ~/Downloads/ditto-passport.zip \
  --check-only
```

Then point it at an already-running, reviewed local harness. A base URL or its
`/run` URL is accepted:

```bash
python3 scripts/submission-lab.py \
  --passport ~/Downloads/ditto-passport.zip \
  --agent-url http://127.0.0.1:8080/run \
  --port 4320
```

The original cloud user ID is not reused: every run seeds a fresh random local
user unless `--user-id` is supplied. The lab advertises the current read-only
memory tool catalog and carries the last 20 messages (up to 10 user/assistant
exchanges) in `system_prompt`; a submission that ignores that field may still
behave as a single-turn agent.
The UI starts as soon as validation and harness health checks pass, reports a
`seeding`, `ready`, or `error` state, and rejects chat requests until every
staged seed wave has completed. Seed failures expose only a non-private error
category in `/api/meta`; memory IDs and backend response details stay out of the
browser surface.

For a faster subjective smoke test, pass `--max-pairs N`. The lab still verifies
the complete Passport signature, manifest, counts, graph references, and issuer
authority before selecting the first N seedable memories in signed export order.
Only subjects and links referenced by that sample are retained. The local status
reports `memory_scope: bounded_sample` without exposing a count or content. This
mode is useful for quickly experiencing a harness, but it is not a full-export
replay and must not be treated as a complete compatibility or quality verdict.
Omitting `--max-pairs` always seeds the full validated export.

For an offline real export, save a trusted base64url Ed25519 public key in a
file and pass `--verification-key PATH`. `--trust-embedded-key` proves only
archive integrity, not Ditto provenance, and is intended solely for synthetic
fixtures or explicit offline experiments. Staging and local backend exports can
use `--verification-base-url https://staging-api.heyditto.ai` (or a loopback
backend origin); the signed issuer remains `https://heyditto.ai`.

To start an audited submission tarball, pin the exact digest reviewed in
Backroom and pass only the provider secret it actually needs:

```bash
python3 scripts/submission-lab.py \
  --passport ~/Downloads/ditto-passport.zip \
  --submission ~/Downloads/reviewed-submission.tar.gz \
  --submission-sha256 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --allow-run-untrusted \
  --pass-env OPENROUTER_API_KEY \
  --seed-max-request-bytes 25165824 \
  --agent-port 8081 \
  --port 4321
```

Important security boundaries:

- Passport files and responses are size-bounded; signed file coverage, counts,
  graph references, duplicate IDs, timestamps, and the issuer key are checked
  before seeding. Signed historical rows with neither prompt nor response are
  validated but omitted because the retrieval seed contract requires text.
- The lab accepts loopback harness URLs by default. `--allow-remote-agent`
  means the real memory export will be uploaded to that remote service and
  requires HTTPS for the non-loopback URL.
- Seed waves are limited by both pair count and serialized JSON size. Lower
  `--seed-max-request-bytes` when an adapter has a smaller request-body limit.
  A single pair and its linked subjects are never split across requests.
- A child process gets a small build/runtime environment and no provider keys
  unless named with `--pass-env`. The selected model provider can still receive
  prompts and retrieved memory content during normal inference.
- Digest pinning and safe extraction do not make miner code safe. The source
  process is **not container-sandboxed**: it runs as your local account and can
  access files and the network available to that account. Review it first and
  use a disposable VM/container for hostile or uncertain submissions.
- Gzip, bzip2, xz, and uncompressed tarballs are detected by their magic bytes
  and read as a stream. One strict decompressed-byte limit covers tar headers,
  padding, PAX/GNU metadata, file bodies, and trailing data; member-count and
  declared file-size limits are also enforced before each body is extracted.
  Conventional `.` and `./` root layouts are normalized, while traversal,
  links, special files, duplicate names, case collisions, and file/directory
  collisions are rejected.
- Default Cargo startup uses the extraction root when it contains `Cargo.toml`,
  or one non-hidden top-level wrapper directory containing `Cargo.toml`; missing
  or ambiguous layouts fail closed. An explicit `--start-command` instead runs
  from the extraction root so the lab never guesses a custom project's layout.
- A source directory is mutable and cannot be pinned by this script. Prefer the
  exact reviewed tarball plus `--submission-sha256` for reproducible tests.

### DittoBench v9 scoring

The starter harness defaults practice to benchmark version 9 while retaining
version 8 compatibility during the fleet transition.
Every submission gets a
fresh procedural persona universe, and the composite is
`0.5 × tool + 0.5 × memory`. The wire contract lives in
[PROTOCOL.md](PROTOCOL.md); the public scorer owns the versioned dataset and
grading contract.
[BASELINES.md](BASELINES.md) reports what the stock kit scores under the locked
model (the target to beat) and its weakest categories.

Memory is seeded in three tiers (see [PROTOCOL.md](PROTOCOL.md) `POST /seed`):
A prepared subjects, B raw pairs only (build your own subject index),
C staged waves interleaved with runs (upsert each
wave). The bundled harness reuses the production `save_memory` path in
`seed.rs`. Extending it to construct subjects when none are provided (Tier B) is
the highest-value change you can make.

## How to optimize

The three levers from What you optimize, in detail. Each is wired in
`src/baseline.rs`, marked `EXTENSION POINT`, and free to spill into your own
modules. The chat model is locked (see What isn't scored, and why), so the levers
are retrieval, the prompt, and tools:

1. Retrieval / memory: the production stack is wired and active, including the
  weight-predictor MLP, composite V2, and the cross-encoder reranker
   (`open_store`). Tune it by retraining/swapping `fixtures/models/mlp-weights.bin`,
   swapping the cross-encoder ONNX, adjusting the RRF `k`/`ceWeight` in
   `reranker.rs`, or changing `candidate_pool_size`/`variant`/limits. Measure
   with `mem-eval` (`recall@k`). Memory is the harder half of the composite and
   retrieval recall is the main bottleneck, so this is the highest-value scored lever.
2. System prompt: you own the prompt the agent runs on. The `system_prompt` in
   each `RunRequest` is an input you can extend or replace; layer on a tool-use
   policy and abstention rules so the agent picks the right tool (and *no* tool
   when it shouldn't).
3. Tools: the baseline registers the per-case tool catalog and executes each
   non-memory call through the validator-provided endpoint. The returned value
   is fed back to the model and the validator grades the observed trajectory.

The stock harness allows 24 model turns so composed tasks can include retries,
dependent calls, and useful exploration. This is a starter implementation
guardrail, not a scoring cap: a correct trajectory is not penalized merely for
using more than 15 tool calls, and miners may tune the harness turn bound.

### What isn't scored, and why

Model identity and latency do not affect your score. Bench v9 does intentionally
let the harness choose `low`, `medium`, or `high` reasoning effort; omission
defaults to `medium`, so that budget choice is a miner strategy.

Model. Every miner is scored on the same frozen model. A scored crate run builds
your image and serves it under the lock: the validator's relay overrides
`DITTOBENCH_MODEL` and serves GPT-OSS-20B through the platform inference relay regardless of what `baseline.rs`
sets (see *Mining on SN118*), so swapping the model changes only local practice
speed and cost. The benchmark measures the harness (memory, retrieval, agent
orchestration, tool selection), not the model. If model choice were scored, the
board would rank who can afford the strongest frontier model, not who built the
best agent, turning an open-source harness competition into a spend race. One
frozen open-weight model holds that variable constant, so score gaps reflect
harness quality and every miner is scored under identical, attestable conditions:
the ticket-bound route proves the locked model actually ran, so no one can
quietly swap in a stronger one. Keep the kit default
(`openai/gpt-oss-20b`) so practice tracks scoring.

Latency. Reported as `median_ms` on the leaderboard, never scored. It measures
hardware and model-provider speed, not harness quality, and it varies with
sandbox load, so two validators would compute different numbers, and the weight
fold requires every validator to derive the same score from the same public
ledger. Speed is bounded instead of ranked: a case that misses its per-case
timeout scores 0, and the tool-efficiency factor penalizes over-calling, so the
efficiency that reflects harness quality is captured without tying the score to
raw hardware speed.

Put your effort into the three levers above.

Run `mem-eval` after retrieval changes (recall@k, no LLM) and `practice` after
agent/tool changes (watch `composite`, per-category tool means, slowest cases).

### Per-question-type levers (versioned memory suite)

Every memory question type maps to a concrete mechanism in this kit (or one you
can add). Nothing is scored that lacks a lever; the lever is the capability
being measured:

| Question type | Miner lever |
|---|---|
| single/multi-session, preference, knowledge-update | retrieval quality: composite signals, the subject index, recency handling (latest value wins) |
| temporal, trajectory, duration | timestamp arithmetic over the seeded pairs' timestamps. Order and elapsed time come from the transcript, not the model's guess |
| assistant-recall | store and index the ASSISTANT turns, not just user turns (the answer only ever appeared in an assistant reply) |
| aggregation, computed | mention counting across sessions; deliberately punishes naive dedup collapse of repeated topics |
| canary | a lexical/exact-match index. Embeddings represent random tokens (`VK-…` codes) poorly, so semantic-only retrieval misses them: a concrete, winnable gap the stock kit does not attempt |
| injection-resistance | a system-prompt guard: the frozen harness model complies with embedded overrides unless YOUR harness defends; a scored, discriminative surface the stock kit does not attempt |
| isolation | honor `user_id` scoping (already wired in the kit's store) |
| abstention, DRM lure (a related decoy that tempts a false recall) | confidence gating + the `abstain` wire flag. Decline when retrieval finds nothing (or finds only someone ELSE's value) instead of fabricating |
| contradiction | read the LATEST stance from memory: some opinions were reversed ("no longer do it") and some were not ("still love it"). Both answers occur under the same question surface, so the signal must come from retrieval |

The two rows that most separate a naive submission from a competitive one are
canary (needs a lexical index) and injection (needs a prompt guard):
both are scored on every run and the stock kit leaves them on the table.

### Embedder note

The kit defaults to local Ollama `embeddinggemma` (768-dim) for a free,
self-contained loop. To make the ranker work in that space, the shipped MLP is
retrained on embeddinggemma (via the production training pipeline, on
LongMemEval).
On the bundled seed user this lifts retrieval from hit@10 0.90 → 0.96 vs the
Vertex-trained weights. The cross-encoder rerank is embedder-independent (it
scores raw text), so it is identical to production.

DittoBench v9 keeps this exact 768-dimensional `OllamaEmbedder` interface, but
the trusted validator gateway serves it with the reviewed OpenRouter profile
`dittobench-v7-openrouter-pplx-embed-v1-0.6b-768-v1`
(`perplexity/pplx-embed-v1-0.6b`, Perplexity only, no fallback). The harness
does not receive an OpenRouter key and cannot select a sibling model or
provider. Local practice and historical benchmark versions continue to use
`embeddinggemma`.

The v9 contract intentionally retains this embedding profile (whose
immutable revision name includes `v7`) and the existing MLP weights. A model or
dimension change requires a new profile and calibration.

If you switch `build_embedder` to a different embedder, retrain the MLP for
that space. The production training pipeline is not distributed, but the
artifact format is documented ([fixtures/models/README.md](fixtures/models/README.md)
and the harness's `mlp.rs`), so you can train your own with any pipeline and
drop it in. To run the exact production stack, use Vertex `text-embedding-005`

- the production `model.bin`.

## Changing the retrieval algorithm

Retrieval is the main lever on your score, so this is worth being clear about:
you can change the ranking algorithm itself, and for the most part you do it from
this kit, not by editing ditto-harness. The harness exposes the pipeline as
seams that `baseline.rs` already wires up:

- Reranker: it is a trait. `baseline.rs` builds an `Arc<dyn Reranker>` and injects
  it. Implement your own reranker in your `src/` and pass it in, or swap the model
  in `fixtures/models/cross-encoder.onnx`.
- Fusion weights: the weight predictor loads your bytes
  (`MlpPredictor::load_from_reader`), so retrain and drop in your own
  `fixtures/models/mlp-weights.bin`.
- Raw retrieval: `store.db()` exposes the database directly (candidate search,
  subjects, raw rows). You can bypass the built-in ranker entirely and score
  candidates with your own algorithm in your crate.
- Candidate pool and variant: `CompositeSearchRequest` exposes
  `candidate_pool_size`, `variant`, and limits.
- New retrieval capabilities (a lexical index for canary codes, a subject index,
  indexing assistant turns): add them in your `src/` on top of the store API.

You only fork ditto-harness to edit its built-in composite scorer in place (its
internal signal math and candidate queries), which you can otherwise override or
bypass with the seams above. To fork: point the `ditto-harness` `git` URL and
`rev` in `Cargo.toml` at your fork. Editing a local clone has no effect on your
submission unless you repoint the dependency, because the build uses the pinned
commit. Any dependency needed by the submission build must be public or
vendored into the bounded artifact. Screeners deliberately provide no GitHub
credential or other build secret to submission-controlled Dockerfiles.

## Submit

```bash
cargo run -- submit   # packages dittobench-submission.tgz + prints next steps
```



### What you submit: the whole crate, not one file

`submit` runs `tar -czf dittobench-submission.tgz .` (excluding `target/`,
`.git`, `*.tgz`, `*.db`, `*.db-*`, `.env`, `.env.*`, and it prints the exclusion
list). Never commit or package your `.env`. It may hold your local-practice
`OPENROUTER_API_KEY`, and the tarball is uploaded to the platform. You submit
the entire buildable project, with the `Dockerfile` at the tarball root:

- `Dockerfile`, `Cargo.toml`, `Cargo.lock` (this Rust reference)
- `src/`, including your edited `baseline.rs` and the `dittobench-miner` server
- `fixtures/`, the ONNX models + seed data your harness loads at runtime

An alternate implementation instead packages its own normal project files—for
example `Dockerfile` + `pyproject.toml`, `package.json`, `go.mod`, or any other
build inputs. Cargo files and a `src/*.rs` layout are not part of admission.

The tarball is capped at 20 MiB. The shipped `fixtures/models/` fit well under
that; if you bundle larger weights, keep the packaged tarball within the cap.

You are not submitting `src/baseline.rs` on its own, and you are not
submitting `ditto-harness`. `ditto-harness` is a pinned public git dependency
of your crate. The Docker build fetches it.

### The fixed interface

The screener builds your tarball in a credential-free rootless Docker boundary.
Validators load the exact verified image, run it, then score it. A submission is
only valid if it keeps this contract intact:


| Must hold                                                              | Why                                                                              |
| ---------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| A `Dockerfile` at the tarball root                                     | It's the validator's Docker build context.                                       |
| `docker build` succeeds                                                | A pre-screen gate rejects submissions that don't build.                          |
| The image serves `GET /health`, `POST /seed`, `POST /run` on :8080 | The validator drives your harness over these (see [PROTOCOL.md](PROTOCOL.md)). |
| `POST /run` returns a well-formed `RunResponse`                        | The scorer grades `tool_calls` + `final_text`. A malformed body scores 0.        |




### What you're free to change

Everything else is yours: `baseline.rs` holds the scored levers from *How to
optimize* (system prompt, retrieval knobs, tools) plus the model, which only
affects local practice; then any other `src/` file, added crate dependencies,
your own `fixtures/models/` weights, even the `Dockerfile` build steps.
Restructure the crate however you like, as long as `docker build .` still
produces a container serving that protocol on :8080.

## Mining on SN118

> Status: the hosted practice validator, on-chain submission path (`ditto upload`,
> eval fee, scoring, weights), and the [SN118 leaderboard](https://platform-api.heyditto.ai/)
> are live today.
> Benchmark v8 is the only active scoring contract. It uses locked
> `openai/gpt-oss-20b` inference and validator-observed tool execution.

1. Registration. You need a hotkey registered on subnet netuid 118 and TAO for
the registration cost plus per-submission eval fees. `ditto upload` offers to
register for you when its pre-check finds the hotkey unregistered, showing the
live cost before it recycles anything; to do it separately, run
`btcli subnets register --netuid 118`. See the
[miner guide](../../docs/MINER.md#prepare-for-mainnet).

2. Submission + fee. This kit's `submit` only packages the tarball. The
on-chain upload happens through `ditto upload` (the miner CLI from the
`ditto-subnet` repository root), with
your registered hotkey. The Backroom-controlled fee is denominated in TAO and
is currently `0.04 TAO` (`40,000,000 rao`). The CLI shows the exact TAO amount
before you confirm. TAO/USD conversion is reporting-only and does not affect
the fee, admission, or payment validation.

The CLI saves a finalized payment receipt before upload so a failed upload can
recover it for 24 hours without paying again. An abandoned unpaid reservation
can be replaced after 15 minutes. Follow the
[ditto-subnet miner guide](../../docs/MINER.md#verify-and-submit)
for the canonical upload, retry, and receipt-recovery commands.

3. The runtime contract. Scoring is one-shot from your uploaded
tarball. You do not keep a server running. A screener builds your `Dockerfile`
once; the scorer loads that exact image, starts it, and injects runtime model configuration
for a trusted local broker plus a fresh `DITTOBENCH_DB` path. No validator or
provider credential is placed in the harness container. The broker supplies
only ticket-scoped v8 inference and locks `DITTOBENCH_MODEL` to GPT-OSS-20B.
The Docker host gateway is mapped so the default `OLLAMA_BASE_URL` resolves to
the scoring host's Ollama, which serves the reference `embeddinggemma`
embedder. If you use a different local embedder, bundle it in your image. The
container has network egress to model providers (hardened deployments may
restrict egress to an allowlist). Your harness must read all model config from
env. `Baseline::from_env` already does this, so keep that property if you rewrite it.

4. Timeouts. 10 s for `/health` to come up, 60 s per `/run` call (a case
that misses it scores 0), 5 minutes per `/seed` wave. The table is in
[PROTOCOL.md](PROTOCOL.md).

5. Run shape. An on-chain run is `run_size=full`: on the order of 50
memory cases + 60 tool cases, with 2 staged seeding waves, a substantial
Tier-B raw-pairs share, and a handful of isolation cases across
separate user graphs. Exact counts can change with `bench_version`.

6. Economics (king-of-the-hill). Competitive weight is distributed
`65% / 14% / 10% / 7% / 4%` to the champion and the next four distinct miners;
everyone else earns nothing. The competitive vector takes 100% of the available
miner emission, so nothing is burned while eligible miners exist (with no
eligible miners, 100% is burned). A challenger dethrones the incumbent only after
clearing the greater of a fixed `0.007` composite-point hysteresis and the
statistical error band. From `bench_version` 6 on, that band shrinks smoothly
once the incumbent exceeds `0.60`, keeping the crown contestable as scores
approach the ceiling; a near-miss is settled by re-scoring both agents on shared
seeds rather than dataset luck. Weights are recomputed from the public score
ledger on every validator sweep.

7. bench_version. Version 8 is activated and is the only version accepted by
this starter harness. Every `/run` request must include `"bench_version": 8`.

8. Lifecycle. After upload your agent goes `uploaded → evaluating → scored`, or `screening_failed` if the Docker build or `/health` fails (fix
and resubmit). Scores land on the public score ledger and the
[SN118 leaderboard](https://platform-api.heyditto.ai/), with median latency,
`bench_version`, and per-category stats.

9. Anti-gaming. The dataset is procedurally regenerated per run from a
fresh seed. Grading is deterministic (no judge to prompt-inject): emitting an
embedded injection payload, surfacing another user's value, or naming a
distractor value zeroes the case, and those events land in the run's public
details. Malformed responses, timeouts, and build failures score 0. Observed tool
execution (the validator runs your tool calls against its own mock endpoint)
is mandatory on scored v8 tool cases; an unobserved observable case scores 0. Beyond
per-case grading, the composite carries bounded integrity multipliers: a per-run
canary nonce (bounded penalty for an honest miss, hard cap for leaking the
decoy), a metamorphic-consistency factor over invariance families, and the
tool-efficiency factor. All three are detailed in
[PROTOCOL.md](PROTOCOL.md) and are pure functions of the published run details.

A fraction of every scored run's cases is also re-asked under a
**reproduce-under-transform audit**. Those cases are ordinary graded cases: some
are the same question in different wording, and some shift what is being asked
(for example asking what a value was *before* the most recent change) so that
the correct answer differs from the base case's. Which cases are audited and how
they are transformed both derive from the dataset seed, and that seed comes from
a block hash fixed *after* you submit, so neither is predictable at commit time.

Practically: answer the question that is actually in front of you. A harness
that dispatches on a question's exact surface form (a template fingerprint, a
lookup keyed to the original phrasing, or an answer precomputed for it) will
answer the base case and miss its transformed sibling, and that split is
reported as `transform_robustness` in the run details. A harness that genuinely
reads the conversation and recomputes its answer scores the same on both, so
there is nothing here to defend against beyond not hard-coding phrasings. The
run also publishes `audit_case_count` so you can see how many pairs the number
rests on.

10. Originality (duplicate detection). Before scoring, the platform compares
your uploaded harness against every other miner's eligible submission across
several dimensions: exact bytes, normalized source (comments, whitespace, and
formatting stripped, so a reformat/recomment/file-rename does not hide a copy),
lexical and language-aware structural fingerprints, the prompt/strategy text, and a
semantic code-embedding vector. A runtime behavioral signal (your observed
tool-call trajectory on a shared dataset seed) is not folded into this comparison
today. An exact or trivially repackaged copy of another miner's agent is held
for manual review and excluded from the ledger (zero weight) while held. The
earlier upload wins by first-seen. In the softer "similar but not identical"
band a hold requires agreement across multiple independent signals, so
legitimate convergence is not penalized: building on this starter kit and the
shared `ditto-harness` dependency is expected, and two miners independently
arriving at similar prompts or structure is fine. Detection targets copying
another miner's submission; shared use of the public baseline is expected.
First-seen protects the original author over a later uploader. Forking a leaked
crate and renaming its symbols does not earn. Differentiate on substance: change
the prompt, retrieval, or tools (see *How to optimize*). The model is not a
differentiator, since scoring locks every miner to the same frozen model.

11. Hardware. The reference stack runs on CPU: Ollama `embeddinggemma`
(~1 GB), the TinyBERT ONNX reranker, embedded SQLite. 8 GB RAM is
sufficient. No GPU is required unless you bundle a local LLM.

12. Support. Open a GitHub issue on `ditto-assistant/ditto-subnet`.

## Pitfalls

- Do not overfit the local scorer. The local dataset generator is a
simplified pool while the validator's persona universe rotates every run. See
the blockquote in [PROTOCOL.md](PROTOCOL.md).
- Arguments weigh as much as selection on-chain. The on-chain
deterministic tool grade is `0.4 name F1 + 0.4 argument F1 + 0.2 trajectory`
(see [PROTOCOL.md](PROTOCOL.md)). Only the *local* scorer is name-centric.
- Latency is not scored (only reported as `median_ms`); a case that misses its
per-case timeout scores 0, and the tool-efficiency multiplier bounds over-calling
on observed runs. The rationale is in *What isn't scored, and why*. Measure with
`practice`.
- Do not key answers to a question's exact wording. A fraction of every scored
run is re-asked under an unpredictable rephrasing (and some transforms change
what is being asked, so the base case's answer becomes wrong). Surface-form
dispatch answers one and fails the other; see `transform_robustness` in the run
details.
- Memory needs the seed user loaded + Ollama embeddings. Run `seed-user`
first. If `mem-eval` reports `recall@k: 0.000`, see
[SETUP.md](SETUP.md) → *Troubleshooting*.
- Never hold a lock across an `.await` that does network I/O. The bundled
store owns one connection and rejects overlapping operations, so it is tempting
to serialize all store access behind one gate. That is fine — but keep embedding
and model round-trips *outside* the critical section and hold the gate only for
the store call itself. A scored run executes several cases concurrently; a gate
that spans a network call turns them back into a single queue, and the run misses
its deadline even though every individual case was well inside its own budget.
This is the most expensive mistake we have seen in practice, and it fails as a
whole-run timeout with no per-case error to point at.
- Keep CPU-bound work off the async workers. Cross-encoder inference,
tokenization, and any index you build yourself are synchronous CPU work. Hand
them to `tokio::task::spawn_blocking` — `reranker.rs` shows the pattern — so they
cannot occupy a Tokio worker that other cases need for I/O.
- Re-check what your `bench_version` gates actually enable. A branch written as
a rare fallback on one version can become the unconditional hot path on the next
one. Diff the per-request work your agent does across versions before you submit,
not after a run times out.



## License

MIT ([LICENSE](LICENSE)). The `ditto-harness` dependency is also MIT-licensed.
