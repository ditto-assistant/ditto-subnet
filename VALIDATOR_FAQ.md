# Validator FAQ: Ditto Subnet (Bittensor SN118)

**Status: pre-release prep.** This is what to expect and what to have ready so
you can stand up a validator quickly at release. We are still pinning down some
daemon entrypoints and the final compute sizing. Items marked _(finalizing)_ may
shift slightly before launch. If anything here is unclear, ping the team in the
validator channel.

---

## TL;DR: what you need before release day

- [ ] **A machine that can run Docker** (Linux host, Docker + Compose installed).
- [ ] **A registered validator hotkey** on **netuid 118** (finney mainnet) with
      enough stake to set weights.
- [ ] **An OpenRouter API key.** The validator uses it to score submissions
      (paraphrase generator + LLM judge). Bring your own; we store nothing for you.
- [ ] **Ollama** running locally with the `embeddinggemma` model pulled (memory
      embeddings, 768-dim).
- [ ] **The repo pulled and dependencies synced** (`git clone` +
      [`uv sync`](https://github.com/astral-sh/uv)).

---

## What does a validator do?

Miners submit an agent + memory **harness**. Your validator:

1. **Pulls each submission** and builds/runs it in an **isolated Docker sandbox**.
2. **Scores it on DittoBench** across tool-calling correctness, memory recall,
   and tool efficiency. It runs against a **freshly randomized anti-cheat
   dataset** per submission, so nobody can overfit a lookup table. The validator
   also measures latency.
3. **Sets weights on-chain** (`put_weights` via Pylon) so emissions flow to the
   best harness. Scoring is effectively winner-take-most.

The scoring loop mirrors the hosted DittoBench practice validator that miners
already use to iterate. The difference on-chain is that _you_ build the miner's
crate in Docker and _you_ write weights to the chain.

---

## Compute requirements

Plan for a **Linux host with Docker**. The validator runs a small stack plus a
sandbox that builds and executes miner submissions, so it leans more on build /
CPU than on a GPU.

| Component | Why it's needed |
| --- | --- |
| **Docker + Docker Compose** | Runs Pylon, Postgres, object storage, and the per-submission build/run sandbox. |
| **Ollama** (`embeddinggemma`) | Memory-retrieval embeddings for scoring. Runs fine on CPU. |
| **Python 3.11 or 3.12 + `uv`** | The subnet service itself. |
| **Rust toolchain** _(finalizing)_ | Miner harnesses are Rust crates; the sandbox compiles them. Shipped inside the sandbox image where possible. |
| **Disk** | Compiling crates + caching Docker layers and submission artifacts. Give it headroom (tens of GB). |

**You do not need a dedicated GPU.** Scoring calls OpenRouter for the chat and
judge models and Ollama for embeddings (CPU-friendly). We are still validating
concrete CPU/RAM/disk minimums on the reference host and will post them here
before release _(finalizing)_. As a starting point, size for comfortably
building Rust crates in parallel (think a solid multi-core VM with 16 GB+ RAM),
and we'll confirm the real numbers together.

---

## What keys / credentials will I need?

Three things, none of which we hold for you:

1. **A Bittensor wallet with a validator hotkey registered on netuid 118.**
   Standard `btcli` registration + stake. This hotkey is your on-chain identity
   for setting weights.

2. **Pylon identity credentials.** The validator writes weights through a local
   [Pylon](https://github.com/backend-developers-ltd/bittensor-pylon) container.
   You load your validator wallet into Pylon and set:
   ```ini
   PYLON_IDENTITY_NAME=<your pylon identity name>
   PYLON_IDENTITY_TOKEN=<your pylon identity token>
   ```
   You need these for the write path (`put_weights`); read-only smoke tests work
   without them.

3. **An OpenRouter API key.** The scoring loop uses it for the paraphrase
   generator and the LLM judge (the same BYOK model the practice validator uses).
   ```ini
   OPENROUTER_API_KEY=sk-or-...
   ```

You do **not** need to bring S3/MinIO or database credentials; the local stack
ships defaults via Docker Compose. In production you'd point storage at a real
bucket, but that's optional for getting started.

---

## Getting set up (dry run)

```sh
# 1. Pull the repo
git clone https://github.com/ditto-assistant/ditto-subnet
cd ditto-subnet

# 2. Config
cp .env.example .env
#    Fill in: PYLON_IDENTITY_NAME / PYLON_IDENTITY_TOKEN (your wallet),
#             OPENROUTER_API_KEY, and confirm NETUID=118 / SUBTENSOR_NETWORK=finney.

# 3. Dependencies + local stack
uv sync
make stack-up        # postgres + pylon (Docker), blocks until healthy
make migrate         # apply DB migrations

# 4. Prove the chain path works end-to-end (read-only)
make smoke-pylon     # exercises the chain client against finney via Pylon

# 5. Embeddings (separate shell)
ollama serve &
ollama pull embeddinggemma
```

At release the validator daemon has a single entrypoint that runs the full loop
continuously: build each submission, score it, then set weights _(finalizing;
the exact command lands in the README before launch)_. Today you can already
exercise two pieces: the chain client (`make smoke-pylon`) and the API
(`make api-up`, then `make smoke-api`).

Relevant `.env` knobs (see `.env.example` for the full list and comments):

```ini
NETUID=118                     # SN118
SUBTENSOR_NETWORK=finney       # mainnet
PYLON_URL=http://localhost:8001
PYLON_IDENTITY_NAME=           # set this to set weights
PYLON_IDENTITY_TOKEN=          # set this to set weights
OPENROUTER_API_KEY=            # set this for scoring
```

---

## FAQ

**Do I need a GPU?**
No. Chat + judge models run via OpenRouter; embeddings run on CPU via Ollama.

**How much will OpenRouter cost me?**
It scales with how many submissions you score and the model you pick. You can
point the generator/judge at a cheap model (the practice service defaults to a
`gemini-*-flash-lite`-class model). We'll recommend a specific model plus a rough
per-eval cost before release.

**Does my hotkey need to be registered before I can validate?**
Yes. Register on netuid 118 and have enough stake to set weights. Do this ahead
of time so release day is just "start the daemon."

**Where's the port layout?**
Pylon is on host port **8001**; the subnet API owns **8000**. Postgres/MinIO use
their compose defaults.

**What's the difference between this and the DittoBench practice API?**
The practice API is a hosted, off-chain, BYOK service miners use to iterate, with
no Docker build and no chain. Your on-chain validator does the real thing: it
builds each miner crate in a Docker sandbox and writes weights to SN118.

**What if something in this doc is out of date?**
This is a living pre-release doc. Items marked _(finalizing)_ are the ones most
likely to change; we'll keep this file current through launch. Ask in the
validator channel if in doubt.
