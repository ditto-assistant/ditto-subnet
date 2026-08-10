# LongMemEval adapter

This adapter evaluates any public DittoBench-compatible harness on the cleaned
LongMemEval-S condition. It exercises the submission's own `/seed` memory store
and `/run` agent path; it does not add a sidecar retriever, vector database,
reranker, or benchmark-specific skill.

## Fairness boundary

- Each question gets an isolated `user_id` and the complete timestamped history.
- History is loaded in chronological dataset order. Display timestamps are
  converted mechanically to RFC 3339 UTC-shaped values (the dataset encodes no
  timezone). Every user and assistant
  turn is preserved. Empty pair sides encode assistant-first, user-only, or
  same-role-adjacent turns without inventing dialogue.
- Only `role`, `content`, session ID, and timestamp cross `/seed`. The adapter
  strips `answer`, `answer_session_ids`, `has_answer`, and `question_type`.
- By default, `/run` receives the question, question date, a truthful generic
  `memory_lookup` case ID, and the exact four-tool memory-reading subset of the
  public DittoBench catalog: `search_memories`, `search_subjects`,
  `fetch_memories`, and `search_memories_in_subjects`. Names, descriptions,
  required fields, and array-valued argument schemas match the public catalog.
  The harness chooses its native retrieval and answer path.
- `--retrieval-mode no-tools` reproduces the first-pass ablation. It is retained
  for comparison but is not the preferred fair condition because a real
  DittoBench memory case advertises the memory tools.
- Hypotheses use the official `question_id` / `hypothesis` JSONL shape and are
  scored with LongMemEval's official evaluator separately.

LongMemEval is an end-to-end memory-system benchmark. A score therefore measures
the submitted memory harness plus the pinned answer model, not memory storage in
isolation.

## Run

Use the cleaned dataset revision and digest recorded in the evidence manifest:

```bash
python3 integrations/longmemeval/longmemeval_adapter.py \
  --dataset /path/to/longmemeval_s_cleaned.json \
  --dataset-sha256 <sha256> \
  --harness-url http://127.0.0.1:18081 \
  --agent-label agent-1 \
  --bench-version 9 \
  --retrieval-mode native-memory-tools \
  --answer-model openai/gpt-4.1 \
  --answer-model-profile longmemeval-openrouter-gpt41-reader-v1 \
  --output /private/results/agent-1.jsonl
```

Use `--limit` for a smoke run and `--resume` to continue an interrupted output.
If a harness stopped after only part of a question was seeded, add a fresh
`--user-id-namespace` so unfinished questions receive new isolated user and pair
IDs. Add `--rebalance-pending` to redistribute those unfinished questions across
all shards; completed hypotheses are still skipped.
The output manifest records the adapter condition and hashes the hypothesis file.
The adapter streams the top-level dataset array and retains selected questions
as compact JSON until a shard needs them. This keeps coordinator memory bounded
without changing the decoded question or history presented to a harness.

LongMemEval questions are mutually isolated. For throughput, run multiple fresh
harness containers and use a URL template; one worker is permanently assigned
to each container, so a harness never processes two histories concurrently:

```bash
python3 integrations/longmemeval/longmemeval_adapter.py \
  ... \
  --harness-url-template 'http://longmemeval-agent-1-shard-{shard}:8080' \
  --shards 5
```

`embedding_cache_proxy.py` may be shared across isolated harnesses. It caches
only deterministic `embeddinggemma` responses by the exact model/input hash and
coalesces identical in-flight requests. It never receives user IDs, questions,
answers, retrievals, or agent responses, so it changes compute cost but not any
memory contents or retrieval behavior.

### OpenRouter embedding condition

`cmd/openrouter-embedding-proxy` is the separately labeled hosted-embedding
condition. It leaves the miner-facing Ollama contract unchanged and translates
only `POST /api/embed` to OpenRouter's embeddings endpoint. The locked profile
is recorded in
`docs/longmemeval-benchmark/openrouter-embedding-profile.json`:

- `perplexity/pplx-embed-v1-0.6b`, the smallest currently listed long-context
  retrieval model in OpenRouter's embedding catalog;
- 768 output dimensions, matching the public harness database and MLP shape;
- Perplexity-only routing, fallbacks disabled, and provider data collection
  denied;
- float encoding and exact input order; and
- content-addressed caching over the complete profile/options/ordered-input
  tuple, with no raw memory text in cache filenames, health output, or logs.

The proxy rejects missing/reordered vectors, dimensions other than 768,
non-finite values, unreviewed models/options, and provider errors. `/health`
reports aggregate requests, cache behavior, latency, token usage, and estimated
cost. A cache write failure is counted but does not discard a valid provider
response.

```bash
go run ./cmd/openrouter-embedding-proxy
```

Set `OPENROUTER_API_KEY`, then point otherwise-unmodified harnesses at the
proxy with `OLLAMA_BASE_URL=http://<trusted-proxy>:11434`. The key remains in
the trusted proxy process and never enters a miner container.

## Shared reader model

`openrouter_reader_proxy.py` pins the answering model to `openai/gpt-4.1`,
requires OpenRouter's OpenAI provider with fallbacks disabled, disables provider
data collection, and rejects any other model or endpoint. This is the shared
reader used by each otherwise-unmodified submitted harness. GPT-4.1's larger
context window avoids treating a harness's long native retrieval context as an
empty memory answer, which occurred with the original Qwen reader condition.
The proxy changes no prompts, memories, retrieval results, or tool calls.

`cmd/longmemeval-openrouter-proxy` provides the same locked reader and judge
profiles as a static Go binary for scratch or minimal trusted containers. Set
`LONGMEMEVAL_PROXY_PROFILE=reader` (default port 18437) or `judge` (default
port 18436); both modes accept `OPENROUTER_API_KEY` and expose only aggregate
health/provenance counters.

## V8 agents and configurable reader models

The adapter itself speaks only the public `/health`, `/seed`, and `/run`
contract, so it can evaluate a v8-compatible harness without asking the
production Platform for a scoring ticket. The trusted reader proxy can map the
model identity a v8 harness was built to request (`openai/gpt-oss-20b`) to any
explicit OpenRouter chat model. The model, route, fallback policy, and custom
profile revision stay outside the miner container and are recorded alongside
the hypotheses:

```bash
export LONGMEMEVAL_PROXY_PROFILE=reader
export LONGMEMEVAL_ACCEPTED_MODEL=openai/gpt-oss-20b
export LONGMEMEVAL_UPSTREAM_MODEL='<openrouter-model-id>'
export LONGMEMEVAL_PROVIDER_ORDER=auto
export LONGMEMEVAL_ALLOW_FALLBACKS=true
export LONGMEMEVAL_READER_REVISION='<audited-condition-name>-v1'
export OPENROUTER_API_KEY='<trusted-host-secret>'
go run ./cmd/longmemeval-openrouter-proxy
```

`LONGMEMEVAL_PROVIDER_ORDER` also accepts a comma-separated OpenRouter provider
order. Set `LONGMEMEVAL_ALLOW_FALLBACKS=false` for a single-route experiment.
Any change from the historical GPT-4.1 profile requires a distinct revision;
the proxy refuses to start rather than mislabel a new model condition as the
old result.

Launch an already-screened v8 image with the reader and embedding endpoints as
trusted host services. The placeholder keys below never leave the local Docker
network and are not provider credentials:

```bash
docker run --rm --read-only --user 65532:65532 \
  --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,size=4g \
  --add-host host.docker.internal:host-gateway \
  -p 127.0.0.1:18081:8080 \
  -e DITTOBENCH_PROVIDER=chutes \
  -e DITTOBENCH_MODEL=openai/gpt-oss-20b \
  -e CHUTES_BASE_URL=http://host.docker.internal:18437/v1 \
  -e CHUTES_API_KEY=longmemeval \
  -e DITTOBENCH_INFERENCE_BASE_URL=http://host.docker.internal:18437/v1 \
  -e OLLAMA_BASE_URL=http://host.docker.internal:11434 \
  -e DITTOBENCH_DB=/tmp/dittobench.db \
  '<screened-v8-image>'
```

Then run the adapter with matching `--bench-version 8`, `--answer-model`, and
`--answer-model-profile` values. This produces an offline research comparison,
not a canonical DittoBench score, KOTH candidate, or payout input. Compare
agents only within an identical recorded reader/embedding condition. The local
recipe is for already-screened images; use the production sandbox boundary for
untrusted source or unadmitted images.

## Official judging

Build the evaluator from the pinned LongMemEval checkout using
`Dockerfile.judge`, then run its unmodified `evaluate_qa.py gpt-4o` command.
The image adds only an `httpx==0.27.2` compatibility pin required by the
official repository's `openai==1.35.1` dependency.
`openrouter_judge_proxy.py` is a narrow compatibility boundary: it accepts only
the evaluator's frozen `gpt-4o-2024-08-06` model, rewrites that identifier to
OpenRouter's `openai/gpt-4o-2024-08-06`, and rejects any other model or endpoint.
This keeps the official prompt and output-label logic unchanged while using the
configured OpenRouter account.

## Test

```bash
cd integrations/longmemeval
python3 -m unittest -v
```
