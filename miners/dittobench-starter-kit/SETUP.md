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

You score yourself locally with the kit's built-in `evaluate` (fixed benchmark),
and against the hosted validator (fresh dataset per submission, as on-chain)
via the playground's Submit tab; see §2.

---

## 0. Prerequisites

- Rust (latest stable; this reference needs >= 1.85). Install via [rustup](https://rustup.rs).
- Ollama, for memory embeddings (`embeddinggemma`, 768-dim):
  ```bash
  ollama serve &
  ollama pull embeddinggemma          # needs Ollama >= 0.11.10
  ```
- An OpenRouter API key, for the chat model (free local Ollama also works; see below).

Hosted DittoBench v7 scoring supplies both chat and embeddings through the
validator's ticket-bound gateway. These local Ollama and OpenRouter-key steps
remain the practice setup; scored containers receive neither provider key.

---

## 1. Starter kit: talk to the agent

```bash
git clone https://github.com/ditto-assistant/ditto-subnet
cd ditto-subnet/miners/dittobench-starter-kit

cp .env.example .env
#   edit .env, paste your key:   OPENROUTER_API_KEY=sk-or-v1-...
#   (chat model defaults to openai/gpt-oss-20b, the benchmark v7 scored model;
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
```

> `evaluate` is fixed; the hosted validator generates a fresh
> dataset per submission. See the README's *Local practice vs. the hosted
> validator* section.

### `.env` reference

```ini
OPENROUTER_API_KEY=sk-or-v1-...          # chat model key
DITTOBENCH_PROVIDER=openrouter           # or `ollama` locally (free)
DITTOBENCH_MODEL=openai/gpt-oss-20b      # benchmark v7 scored model
OLLAMA_BASE_URL=http://localhost:11434   # embeddings (and ollama chat) endpoint
DITTOBENCH_DB=./dittobench.db            # local Turso DB; keep the same path across seed-user + commands
```

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

## 2. Scoring like the subnet: hosted practice

The hosted validator is available; the playground's Submit tab drives it
against a fresh rotating dataset. Full steps: README, *Hosted practice*.

---

## 3. How the harness stays in sync

- The kit pins `ditto-harness` to a known-good commit `rev` in `Cargo.toml` for reproducible builds.
- To pick up a newer harness: bump `rev` deliberately, run `cargo update -p ditto-harness`, then run the full suite.
- The hosted and on-chain validators don't pin a harness ref at all; they build your submitted crate, whose `Cargo.toml` pins the harness. Practice and on-chain runs build the same crate you submitted, so practice scores transfer.

## Troubleshooting

- `mem-eval` reports `recall@k: 0.000`: run `seed-user` first, and confirm `ollama serve` + `ollama pull embeddinggemma`, and that `DITTOBENCH_DB` matches what you seeded.
- `feature edition2024 is required`: update Rust (`rustup update`); the harness needs >= 1.85.
- Playground reply is empty or over-calls a tool: if `DITTOBENCH_MODEL` is a lite model (e.g. `gemini-3.1-flash-lite`), set a stronger one in `.env`.
