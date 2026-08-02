# Hermes ↔ DittoBench adapter

This is a standalone, submission-shaped adapter that evaluates
[Nous Research Hermes Agent](https://github.com/NousResearch/hermes-agent)
through the public DittoBench harness contract:

- `GET /health`
- `POST /seed`
- `POST /run`

It does not add a Hermes-specific scoring path. `dittobench-api` still
generates the dataset, calls the normal harness endpoints, observes catalog
tool execution, and runs the normal deterministic grader from
`dittobench-datagen`.

## The adapter boundary

Hermes has two kinds of long-term state:

1. `MEMORY.md` + `USER.md`: roughly 1,300 tokens of agent-curated facts,
   injected into every session.
2. `state.db` + `session_search`: an FTS5 index over complete past
   conversations, searched on demand by the model.

DittoBench seeds hundreds of raw conversation pairs. Putting those pairs into
the small curated files would test an adapter-written summarizer and overflow
Hermes' documented limits. This adapter instead imports each benchmark
`session_id` as a native Hermes session and each pair as a user/assistant turn.
Hermes then recalls the data through its unmodified `session_search` engine.

The catalog's four memory-read names are aliases over that native engine:

- `search_memories`
- `fetch_memories`
- `search_subjects`
- `search_memories_in_subjects`

The alias changes only the wire name/schema visible to the model. It does not
add embeddings, a reranker, answer extraction, or benchmark-aware retrieval.
All non-memory catalog tools are forwarded to the request's
`tool_endpoint`, so the scorer observes the real trajectory.

## Isolation and contamination

Hermes' `X-Hermes-Session-Key` scopes external memory providers, but its native
`session_search` searches an entire SQLite database. The adapter therefore
uses one physical `state.db` per DittoBench `user_id`; sharing a database would
make cross-user leak tests invalid.

Benchmark question/answer turns are not written back into the seeded history.
Otherwise early evaluation questions could contaminate later cases. Staged
`/seed` waves are accumulated and rebuilt idempotently from `pair_id`.

## What remains deliberately unsupported

- Subjects and subject links are accepted and counted but do not change
  Hermes' FTS engine. This is intentional: implementing a new graph retriever
  in the adapter would no longer measure Hermes.
- Hermes' session history is append-only conversational recall. The adapter
  does not reinterpret `save_memory`, `update_memory`, or `delete_memory` as
  edits to imported history. DittoBench lifecycle cases should expose that
  product limitation instead of silently fixing it in glue code.
- `fetch_memories` is an alias over FTS search, not a new pair-ID lookup table.
  Native `session_search` already returns source message windows rather than
  two-stage summary IDs.
- The adapter measures Hermes Agent at the pinned upstream revision, not the
  Hermes model family by itself.

These limitations should be called out beside any score. They are useful
evidence for why a miner's purpose-built memory can outperform a general
personal agent, but they prevent claiming that every possible Hermes memory
provider was tested.

## Reproducibility

The Docker image pins Hermes to:

```text
e57918ac800121cf9c2956fe55e27df3ea80b562
```

The adapter reads the same runtime model lock variables as a miner harness:

- `DITTOBENCH_MODEL`
- `CHUTES_BASE_URL`
- `CHUTES_API_KEY`
- `DITTOBENCH_DB`

Under the scored sandbox, `dittobench-api` overwrites those values with the
validator's locked model relay. The adapter cannot choose a stronger model.

### Runtime profiles

The default `baseline` profile preserves the settings used for the first
published measurement: eight agent-loop iterations and five native
`session_search` results. A second, explicitly labeled `favorable` profile
gives Hermes its documented 90-iteration default, uses the native search
ceiling of 10, and adds generic recall guidance adapted to the benchmark's
memory-tool aliases:

```sh
HERMES_DITTOBENCH_PROFILE=favorable
```

The `native-session` profile is the least translated condition:

```sh
HERMES_DITTOBENCH_PROFILE=native-session
```

It removes all seven Ditto memory read/write names from the model's tool
surface, enables Hermes' own `session_search` tool with its upstream schema and
name, and keeps ordinary Hermes session persistence enabled. Seeded history is
still imported verbatim into native `SessionDB` sessions. The retained trace
therefore says `session_search`, not a canonical Ditto alias. This profile is
appropriate for memory-answer accuracy, but its Ditto tool-routing score and
composite are not comparable because the benchmark expects different memory
tool names.

The alias profiles use the same Hermes agent loop, validator tool catalog, native
SQLite FTS5 retrieval, model lock, and observed tool endpoint. The favorable
profile does not contain benchmark category names, expected answers, routing
examples, query rewriting, embeddings, reranking, or a benchmark-specific
skill. `HERMES_DITTOBENCH_MAX_ITERATIONS`,
`HERMES_DITTOBENCH_SEARCH_LIMIT`, and `HERMES_DITTOBENCH_MAX_TOKENS` remain
available as explicit positive-integer overrides for controlled experiments.
The output cap defaults to `8192`, matching the OpenClaw adapter and keeping
requests inside the pinned OpenRouter/Nebius context window.

Build and run locally:

```sh
docker build -t hermes-dittobench .
docker run --rm -p 8080:8080 \
  -e DITTOBENCH_MODEL=qwen/qwen3-32b \
  -e CHUTES_BASE_URL=http://host.docker.internal:11434 \
  -e CHUTES_API_KEY=relay \
  hermes-dittobench
```

Run unit tests without installing Hermes:

```sh
python -m unittest discover -s tests -v
```

For a local full-pipeline comparison, run the adapter on port 8080, start
`dittobench-api` with private harness URLs allowed, then submit the normal
`run_size` request against `http://127.0.0.1:8080`. Use the same pinned seed,
benchmark version, run size, and model relay for Hermes and the miner; compare
`memory_mean` and per-category results, not only the composite.

## Fair comparison checklist

Report all of the following:

- exact adapter commit and pinned Hermes SHA;
- exact `dittobench-api` and `dittobench-datagen` SHAs;
- benchmark version, run size, dataset seed/hash, and model lock;
- at least several fresh seeds, including failure/timeout counts;
- `memory_mean`, each memory category, canary integrity, metamorphic
  consistency, and cross-user isolation;
- the unsupported lifecycle/subject semantics above.

A single composite from one rotating dataset is a demo, not evidence that one
memory architecture is categorically better.
