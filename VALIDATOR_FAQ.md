# Validator FAQ: Ditto Subnet (Bittensor SN118)

## Requirements

- **A machine that can run Docker Compose.**
- **A registered validator hotkey** on **netuid 118** (finney mainnet) with enough stake to set weights.
- **An OpenRouter API key.** The validator uses it to score submission (paraphrase generator + LLM judge). Bring your own.
- **Ollama** running locally with `embeddinggemma` pulled.
- **This repo** pulled and dependencies synced.

---

## What does a validator do?

Miners submit an agent harness. Your validator:

1. **Pulls each submission** and builds/runs it in an **isolated sandbox**.
2. **Scores it on DittoBench** for tool-calling correctness, memory recall, and efficiency. Each submission runs against a **freshly randomized anti-cheat dataset**, so nobody can overfit a lookup table.
3. **Sets weights on-chain** (`put_weights` via Pylon) so emissions flow to the best harness. Scoring is king-of-the-hill.

This mirrors the DittoBench practice validator that miners use to iterate. On-chain, _you_ build the miner's crate in Docker and _you_ write the weights.

---

## Compute requirements

Plan for a **Linux host with Docker**. The validator runs a small stack plus a sandbox that builds and runs miner submissions, so it leans on CPU more than GPU.

Suggested starting point. Adjust to your load and the number of miners on the subnet.

- **8+ CPU cores.** Building each miner's Rust crate in the sandbox is the heaviest step.
- **32 GB RAM.** 16 GB works for a single build at a time. 32 GB gives headroom for parallel builds plus Postgres and Ollama.
- **100+ GB SSD.** Docker layers, crate build caches, and submission artifacts.
- **No GPU.**
- **Stable internet connection.**

| Component | Why it's needed |
| --- | --- |
| **Docker Compose** | Runs Pylon, Postgres, object storage, and the per-submission build/run sandbox. |
| **Ollama** | Memory-retrieval embeddings for scoring. Runs on CPU. |
| **Python 3.11 or 3.12 + `uv`** | The subnet service itself. |

Scoring calls OpenRouter for the chat and judge models and Ollama for embeddings.

---

## What keys do I need?

Three, none of which we hold for you:

1. **A Bittensor wallet with a validator hotkey registered on netuid 118.**
   Standard `btcli` registration and stake. This hotkey is your on-chain identity for setting weights.

2. **Pylon identity credentials.** The validator writes weights through a local [Pylon](https://github.com/backend-developers-ltd/bittensor-pylon) container. Load your validator wallet into Pylon and set:
   ```ini
   PYLON_IDENTITY_NAME=<your pylon identity name>
   PYLON_IDENTITY_TOKEN=<your pylon identity token>
   ```
   You need these to write weights (`put_weights`). Read-only smoke tests work
   without them.

3. **An OpenRouter API key.** The scoring loop uses it for the paraphrase
   generator and the LLM judge.
   ```ini
   OPENROUTER_API_KEY=sk-or-...
   ```

You do **not** need S3/MinIO or database credentials. The local stack ships
defaults via Docker Compose. In production you point storage at a real bucket, but that is optional to get started.

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

At release the validator daemon runs the full loop from one entrypoint: build each submission, score it, then set weights _(finalizing: the exact command lands in the README before launch)_. Today you can already run two pieces: the chain client (`make smoke-pylon`) and the API (`make api-up`, then `make smoke-api`).

Relevant `.env` knobs (see `.env.example` for the full list):

```ini
NETUID=118                     # SN118
SUBTENSOR_NETWORK=finney       # mainnet
PYLON_URL=http://localhost:8001
PYLON_IDENTITY_NAME=           # set to write weights
PYLON_IDENTITY_TOKEN=          # set to write weights
OPENROUTER_API_KEY=            # set to score
```

---

## FAQ

**Do I need a GPU?**

No. Chat and judge models run via OpenRouter. Embeddings run on CPU via Ollama.

**How much will OpenRouter cost?**

It scales with how many submissions you score and the model you pick. Point the
generator and judge at a cheap model to keep it low. We will recommend a model
and a rough per-eval cost before release.

**Does my hotkey need to be registered before I can validate?**

Yes. Register on netuid 118 with enough stake to set weights. Do this ahead of
time so release day is just starting the daemon.

**What ports does it use?**

Pylon on host port **8001**, the subnet API on **8000**. Postgres and MinIO use
their compose defaults.

**How is this different from the DittoBench practice API?**

The practice API is a hosted, off-chain, BYOK service miners use to iterate, with no Docker build and no chain. Your on-chain validator builds each miner crate in a Docker sandbox and writes weights to SN118.