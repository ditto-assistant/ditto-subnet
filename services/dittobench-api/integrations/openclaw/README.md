# OpenClaw ↔ DittoBench adapter

This standalone adapter measures [OpenClaw](https://github.com/openclaw/openclaw)
through the public DittoBench harness contract:

- `GET /health`
- `POST /seed`
- `POST /run`

It uses OpenClaw's ordinary embedded agent runtime and its built-in
`memory-core` index. DittoBench continues to generate the dataset, observe tool
execution, and grade every response through the same deterministic path used
for miner harnesses.

## Native-memory boundary

OpenClaw's documented memory format is Markdown in `MEMORY.md` and
`memory/*.md`. The detailed working layer belongs under `memory/*.md` and is
retrieved on demand rather than injected into every prompt. The adapter
therefore:

1. creates a physically separate OpenClaw agent workspace for each benchmark
   `user_id`;
2. writes every seed pair verbatim to a deterministic `memory/pair-*.md` file,
   preserving pair ID, session ID, timestamp, user text, and assistant text;
3. writes the supplied subject/link records verbatim as Markdown-embedded JSON;
4. forces a synchronous native index refresh at every seed-wave barrier;
5. runs each question through OpenClaw's unmodified embedded agent loop.

There is no adapter-written summary, vector database, reranker, answer
extraction, expected-answer access, case router, or benchmark-specific skill.
Evaluation questions are retained only in isolated OpenClaw session transcripts
and are excluded from the memory index, so earlier cases cannot contaminate
later recall.

The measurement deliberately configures OpenClaw's built-in SQLite engine in
documented FTS-only mode (`memorySearch.provider: "none"`). That avoids adding
an embedding credential, local model, QMD sidecar, or third-party memory plugin
that would turn this into a test of a different retrieval product.

## Tool adapter

DittoBench uses stable public memory names while OpenClaw's built-in names are
`memory_search`, `memory_get`, and workspace file operations. A normal OpenClaw
plugin translates only this boundary:

- `search_memories`, `search_subjects`, `fetch_memories`, and
  `search_memories_in_subjects` call the active `memory-core` manager;
- `save_memory`, `update_memory`, and `delete_memory` mutate native Markdown
  files and synchronously refresh that same manager;
- every non-memory tool is posted to the validator-supplied `tool_endpoint`.

The validator receives the real name, arguments, order, and hop for every call.
Qwen chooses each tool and argument through OpenClaw; the adapter never calls a
tool merely because a benchmark category expects it. The generic recall rule
added to the system prompt is a name-adapted copy of OpenClaw's own mandatory
`memory_search` guidance, needed because the benchmark aliases replace that
built-in name on the visible surface.

## Reproducibility

The lockfile pins OpenClaw `2026.7.1`, whose upstream tag resolves to:

```text
2d2ddc43d0dcf71f31283d780f9fe9ff4cc04fe4
```

The runtime reads the validator's normal model-lock variables:

- `DITTOBENCH_MODEL`
- `CHUTES_BASE_URL` / `CHUTES_API_KEY`
- `OPENAI_BASE_URL` / `OPENAI_API_KEY`
- `DITTOBENCH_DB`
- `OPENCLAW_DITTOBENCH_SEARCH_LIMIT` (default 10; positive integer)

Build and run locally:

```sh
docker build -t openclaw-dittobench .
docker run --rm -p 8080:8080 \
  -e DITTOBENCH_MODEL=qwen/qwen3-32b \
  -e CHUTES_BASE_URL=http://host.docker.internal:11434/v1 \
  -e CHUTES_API_KEY=relay \
  openclaw-dittobench
```

Run adapter tests:

```sh
npm ci --ignore-scripts
npm test
```

## Recorded v6 result

The paired OpenRouter report is in
[`docs/openclaw-benchmark`](../../docs/openclaw-benchmark/README.md). On the
shared 206-case dataset, the native 10-result profile scored `0.385417` on
memory and the generous 20-result replay scored `0.427083`. Both retained runs
completed without a provider error or harness no-response.

## Claim boundary

Report this subject as “OpenClaw 2026.7.1 built-in `memory-core`, FTS-only,
through the DittoBench adapter.” It is not a result for every optional OpenClaw
memory plugin. Two paired runs on one seed are illustrative evidence, not a
multi-seed confidence interval, and production miner medians use independent
seeds and a different deployment route.
