# Setup: the Rust reference starter kit

This guide covers setup from a fresh clone to talking to the agent and scoring your harness locally.
You only work in the starter kit; it pulls the harness crate automatically.

These Rust prerequisites apply only to this maintained reference
implementation. DittoBench accepts any language through the same Docker and
HTTP contract; see README, *The fixed interface*.

| Repo | What it is | You need it for |
| --- | --- | --- |
| [`miners/dittobench-starter-kit`](.) | The miner harness you build and optimize in the `ditto-subnet` monorepo. Agent + memory + tools + playground + local scoring. | Always. |
| [`ditto-harness`](https://github.com/ditto-assistant/ditto-harness) | The shared Ditto agent + memory crate the kit depends on (Rust, pinned to a known-good `rev` in `Cargo.toml`). | Pulled automatically as a git dependency. |

```
miners/dittobench-starter-kit  ──depends on──►  ditto-harness
   (your Rust harness)                    (Rust crate, pinned rev)
```

You iterate with the kit's built-in `evaluate` (fixed benchmark), then run the
monorepo's real v9 generator/scorer locally with one command. The hosted service
is a useful reachability rehearsal, but remote harness tool calls cannot be
observed through its loopback tool endpoint; see §2.

---

## 0. Prerequisites

- Rust (latest stable; this reference needs >= 1.85). Install via [rustup](https://rustup.rs).
- Go >= 1.23 for the one-command local v9 practice, which builds the
  monorepo's scorer. The standalone harness commands do not need Go.
- Ollama, for memory embeddings (`embeddinggemma`, 768-dim):
  ```bash
  ollama serve &
  ollama pull embeddinggemma          # needs Ollama >= 0.11.10
  ```
- An OpenRouter API key, for the chat model (free local Ollama also works; see below).

Hosted DittoBench v9 scoring supplies both chat and embeddings through the
validator's ticket-bound gateway. These local Ollama and OpenRouter-key steps
remain the practice setup; scored containers receive neither provider key.

---

## 1. Starter kit: talk to the agent

```bash
git clone https://github.com/ditto-assistant/ditto-subnet
cd ditto-subnet/miners/dittobench-starter-kit

cp .env.example .env
#   edit .env, paste your key:   OPENROUTER_API_KEY=sk-or-v1-...
#   (chat model defaults to openai/gpt-oss-20b, the benchmark v9 scored model;
#    canonical scoring serves it through ticket-scoped platform inference.
#    Embeddings use Ollama.)

cargo run -- seed-user      # one-time: load the dummy LongMemEval seed user (embeds pairs + subjects; ~2 min)
cargo run -- playground     # open http://127.0.0.1:8088 and chat
```

In the playground: ask a memory question (*"how many postcards have I collected?"*)
to watch retrieval, or *"search the web for…"* to watch tool calling. The right
panel shows every tool's definition and a per-turn trace of tool calls + retrieved
memories.

### The other kit commands

```bash
cargo run -- mem-eval --k 10     # retrieval recall@k over the seed user (no LLM, free)
cargo run -- evaluate            # FIXED local submission test: static user + same questions, every run
cargo run -- practice --n 20     # ROTATING random dataset (anti-overfit), like the hosted validator
cargo run -- serve --port 8080   # expose GET /health, POST /run, POST /seed for the validator

# Recommended before submission: full local v9 path with observed tools.
cd ../.. && uv run ditto practice --run-size small
```

> `evaluate` is fixed; the local v9 and hosted rehearsal paths generate a fresh
> dataset per submission. See the README's *Choose the right practice loop*
> section.

### `.env` reference

```ini
OPENROUTER_API_KEY=sk-or-v1-...          # chat model key
DITTOBENCH_PROVIDER=openrouter           # or `ollama` locally (free)
DITTOBENCH_MODEL=openai/gpt-oss-20b      # benchmark v9 scored model
OLLAMA_BASE_URL=http://localhost:11434   # embeddings (and ollama chat) endpoint
DITTOBENCH_DB=./dittobench.db            # local Turso DB; keep the same path across seed-user + commands
```

Local embeddings are intentionally narrower than chat configuration. The
reference kit always calls Ollama `embeddinggemma` through `OLLAMA_BASE_URL` and
expects 768 dimensions. It does not read `DITTOBENCH_EMBED_PROVIDER`,
`DITTOBENCH_EMBED_MODEL`, or `DITTOBENCH_EMBED_BASE_URL`.

Canonical v9 scoring is different: the validator replaces `OLLAMA_BASE_URL`
with a ticket-bound Ollama-compatible gateway that locks profile
`dittobench-v7-openrouter-pplx-embed-v1-0.6b-768-v1`, backed only by
`perplexity/pplx-embed-v1-0.6b`. The harness receives neither an OpenRouter key
nor a provider/model selector. This is why putting a Perplexity model name next
to `DITTOBENCH_EMBED_PROVIDER=ollama` in a local `.env` has no effect.

Fully local development and practice (no API key):

```bash
cp .env.example .env
# In .env, set DITTOBENCH_PROVIDER=ollama and DITTOBENCH_MODEL=gpt-oss:20b.
ollama pull gpt-oss:20b
ollama pull embeddinggemma
cargo run -- ollama-check
cargo run -- seed-user
cargo run -- evaluate
```

This uses Ollama `gpt-oss:20b` for chat and `embeddinggemma` for 768-dimensional
embeddings. Official scoring still injects the ticket-bound platform provider;
Ollama is not a scored fallback.

---

## 2. Rehearsing the v8 path

Use the local path when you need validator-observed tool scoring:

```bash
python3 scripts/local-rehearsal.py --run-size small
```

It builds and starts the harness plus `services/dittobench-api`, uses an
isolated temporary database, supplies a reachable `tool_endpoint`, runs the
fresh v8 generator and staged seeding path, prints the report, then cleans up.
It uses the chat and embedding providers in your `.env`, so it is rehearsal,
not screening/submission certification.

The playground Submit tab still drives the hosted service against a fresh
rotating dataset. A remote public `harness_url` cannot reach the hosted scorer's
loopback tool endpoint, so observable cases are capped there. Use that path for
public reachability and hosted orchestration checks, not an exact tool score.
Full steps: README, *Hosted rehearsal*.

---

## 3. How the harness stays in sync

- The kit pins `ditto-harness` to a known-good commit `rev` in `Cargo.toml` for reproducible builds.
- To pick up a newer harness: bump `rev` deliberately, run `cargo update -p ditto-harness`, then run the full suite.
- The hosted and on-chain validators don't pin a harness ref at all; they build your submitted crate, whose `Cargo.toml` pins the harness. Practice and on-chain runs build the same crate you submitted, so practice scores transfer.

## Troubleshooting

- `mem-eval` reports `recall@k: 0.000`: run `seed-user` first, and confirm `ollama serve` + `ollama pull embeddinggemma`, and that `DITTOBENCH_DB` matches what you seeded.
- `feature edition2024 is required`: update Rust (`rustup update`); the harness needs >= 1.85.
- Playground reply is empty or over-calls a tool: if `DITTOBENCH_MODEL` is a lite model (e.g. `gemini-3.1-flash-lite`), set a stronger one in `.env`.

### Existing Docker volumes after the non-root migration

The maintained image runs as `dittobench` (UID/GID 65532). A volume created by
an older root-running image may therefore reject SQLite writes. Back it up,
stop the old container, and change the mounted data ownership once before
starting the new image:

```bash
# Named volume example; replace dittobench-data with your volume name.
docker run --rm --user 0 \
  --mount type=volume,src=dittobench-data,dst=/app \
  debian:trixie-slim chown -R 65532:65532 /app
```

For a bind mount, change ownership of the specific host data directory to
`65532:65532` instead. Do not recursively change a parent or repository tree.
