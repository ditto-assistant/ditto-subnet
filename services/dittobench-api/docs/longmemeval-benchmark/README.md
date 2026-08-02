# Top five DittoBench miners on LongMemEval-S

This directory records three LongMemEval-S measurements of the five
highest-ranked finalized DittoBench v6 submissions in the public leaderboard
snapshot at `2026-07-23T05:37:19.615917Z`:

- the preferred fair condition is frozen in
  [`results/2026-07-23-top-five-longmemeval-s-native-tools-gpt41.json`](results/2026-07-23-top-five-longmemeval-s-native-tools-gpt41.json);
- the controlled OpenRouter-embedding condition is frozen separately in
  [`results/2026-07-23-top-five-longmemeval-s-native-tools-gpt41-openrouter-pplx-embed.json`](results/2026-07-23-top-five-longmemeval-s-native-tools-gpt41-openrouter-pplx-embed.json);
- the first-pass Qwen/no-tool ablation remains frozen in
  [`results/2026-07-23-top-five-longmemeval-s.json`](results/2026-07-23-top-five-longmemeval-s.json).

These are third-party research measurements, not DittoBench submissions,
validator scores, KOTH candidates, weights, or payout inputs.

## Results

| DittoBench v6 rank | Agent | First pass | Fair condition | Change | Fair empty answers |
| ---: | --- | ---: | ---: | ---: | ---: |
| 1 | whitycatboss v1 | 101/500 (0.202) | 166/500 (0.332) | +0.130 | 0 |
| 2 | killer-4 v2 | 265/500 (0.530) | 338/500 (0.676) | +0.146 | 0 |
| 3 | ditto-max-v1 v1 | 288/500 (0.576) | **350/500 (0.700)** | +0.124 | 0 |
| 4 | avadakedavra v1 | 31/500 (0.062) | 347/500 (0.694) | **+0.632** | 2 |
| 5 | zeus_v12 v1 | 178/500 (0.356) | 188/500 (0.376) | +0.020 | 0 |

The preferred condition raises every submission's score. Most importantly, the
rank-4 submission's 476 empty first-pass answers fall to two. The rank-3
submission still leads, but rank 4 is now within 0.006 rather than appearing to
have almost no memory capability.

LongMemEval is an end-to-end memory-system benchmark. Each value measures the
submission's memory implementation, native retrieval policy, and shared reader
model together. The improvement cannot be attributed causally to either the
tool catalog or reader change alone because the fair condition intentionally
fixes both first-pass disadvantages.

## Hosted embedding comparison

The follow-up changes only the embedding model and transport. It keeps the fair
adapter, native memory tools, GPT-4.1 reader, official judge, frozen agent
images, dataset, and 768-dimensional harness contract unchanged.

| DittoBench v6 rank | Agent | Local `embeddinggemma` | OpenRouter `pplx-embed-v1-0.6b` | Change |
| ---: | --- | ---: | ---: | ---: |
| 1 | whitycatboss v1 | 166/500 (0.332) | 172/500 (0.344) | +0.012 |
| 2 | killer-4 v2 | 338/500 (0.676) | 337/500 (0.674) | -0.002 |
| 3 | ditto-max-v1 v1 | 350/500 (0.700) | 347/500 (0.694) | -0.006 |
| 4 | avadakedavra v1 | 347/500 (0.694) | **359/500 (0.718)** | +0.024 |
| 5 | zeus_v12 v1 | 188/500 (0.376) | 192/500 (0.384) | +0.008 |

The mean agent accuracy moves from 0.5556 to 0.5628 (+0.0072). Individual
changes are small and mixed, so this run does not support the claim that hosted
embeddings mechanically inflate every score. It does show that the native
harnesses can seed, retrieve, and complete all 500 questions through the hosted
route without a sidecar memory system. This is a separate experimental
condition and does not replace the local native baseline.

## Why the first pass was too harsh

The first pass sent `tools: []`, although a real DittoBench memory case
advertises four memory-reading tools. Some harnesses perform retrieval
internally anyway, but the empty catalog can suppress a submission's intended
agent loop.

The shared Qwen route also imposed a reader/context failure on the rank-4
submission. Its native retrieval path assembled at least 26,215 prompt tokens
while its harness reserved 14,746 output tokens. The combined 40,961-token
request exceeded that route's 40,960-token limit by one token, and the harness
returned an empty answer. A canary with the native tool catalog but the same
Qwen reader reproduced the failure. A GPT-4.1 canary produced five non-empty
answers for every submission, so the full fair rerun used that reader uniformly.

The first-pass aggregate is retained as an ablation and evidence of this failure
mode; it is no longer presented as the best estimate of native memory quality.

## Fair adapter boundary

The adapter uses the public DittoBench harness protocol without modifying a
submission:

- each LongMemEval question gets a fresh isolated `user_id` and its complete
  timestamped conversation history through `/seed`;
- every distinct non-empty user and assistant turn is retained in chronological
  dataset order, including assistant-first, user-only, and same-role-adjacent
  turns; reused pair identities resolve deterministically to their final
  occurrence, matching the public harness's upsert semantics;
- answer labels, answer-session IDs, question types, and `has_answer` flags never
  cross the harness boundary;
- `/run` receives the question, question date, and the exact memory-reading
  subset of the public catalog: `search_memories`, `search_subjects`,
  `fetch_memories`, and `search_memories_in_subjects`; descriptions, required
  fields, and array-valued argument schemas match the pinned public catalog;
- the submission chooses its own native storage, embeddings, retrieval, tool
  loop, and final answer path;
- no sidecar retriever, external vector database, reranker, query rewriter,
  answer labels, or benchmark-specific skill is added.

Some submissions execute native retrieval inside the harness rather than
returning it as outer-protocol `tool_calls`. Therefore a zero outer call count is
not evidence that the memory path was unused; the scored output is the complete
native harness response.

All five images used `openai/gpt-4.1` through the same model-pinning OpenRouter
proxy. It permits only OpenRouter's OpenAI provider, disables fallbacks and
provider data collection, and rejects any other model or endpoint. Only the
public LongMemEval dataset was sent to this reader.

The preferred fair condition retains each harness's native 768-dimensional
`embeddinggemma` path. The hosted experiment preserves the harness-facing
`embeddinggemma` `/api/embed` operation but translates it in a trusted proxy to
OpenRouter model `perplexity/pplx-embed-v1-0.6b`, pinned to Perplexity-only
routing, fallbacks disabled, provider data collection denied, 768 float
dimensions, and ordered inputs. The proxy does not change storage, query
construction, retrieval, reranking, or answer generation.

The hosted run observed 383,627 proxy requests, of which 283,035 were cache
hits and 100,592 reached OpenRouter. Those process-lifetime totals include
interrupted and resumed work, so the recorded $0.367306688 is an observed
transport total rather than a minimal per-case estimate. Eight embedding and
48 reader attempts failed transiently; retry and resume completed exactly 500
unique hypotheses per agent before judging. All 2,500 official judge requests
succeeded. Only aggregate counters are committed.

## Official evaluation

Hypotheses use LongMemEval's official `question_id` / `hypothesis` JSONL shape.
The unmodified official evaluator grades all 500 answers with its frozen
`gpt-4o-2024-08-06` prompt and label logic. A narrow proxy maps only that model
identifier to `openai/gpt-4o-2024-08-06` on OpenRouter; it rejects other models
and endpoints.

The committed JSON files contain only aggregates, public agent names, immutable
artifact/image hashes, and hashes of the retained local hypothesis and judge
files. Raw miner source, generated answers, reference labels, and evaluator
transcripts remain local.

## Exact contract

- dataset: cleaned LongMemEval-S, 500 questions
- dataset SHA-256:
  `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`
- Hugging Face dataset revision:
  `98d7416c24c778c2fee6e6f3006e7a073259d48f`
- LongMemEval source revision:
  `9e0b455f4ef0e2ab8f2e582289761153549043fc`
- public DittoBench harness revision:
  `c3caa8e2c19f8a41a0610b9f7db774f97643dd9c`
- public tool-catalog dependency revision: `ef3af0387b46`
- preferred condition: `longmemeval-s-cleaned-native-memory-tools-v2`
- preferred answer model profile: `longmemeval-openrouter-gpt41-reader-v1`
- hosted-embedding condition:
  `longmemeval-s-cleaned-native-memory-tools-v2-openrouter-pplx-embed-v1`
- hosted-embedding profile:
  `longmemeval-openrouter-pplx-embed-v1-0.6b-768-v1`
- first-pass condition: `longmemeval-s-cleaned-native-dittobench-memory-v1`
- first-pass answer model profile: `openrouter-nebius-qwen3-32b-no-think-v1`
- official judge profile: `longmemeval-official-gpt4o-openrouter-v1`

## Validation and interruption handling

Validation covers irregular-role conversion, removal of contentless records,
last-write-wins normalization for reused pair identities, reference-label
exclusion, isolated resume namespaces, pair-ID namespacing, exact public tool
schemas, reader and judge model locking, exact 500-ID cardinality, duplicate-ID
rejection, and category aggregation.

The adapter streams the 265 MB dataset and keeps selected rows compact until a
worker needs them. A local full-selection probe reduced peak coordinator memory
from about 2.93 GB to 0.92 GB without changing decoded entries. Completed
hypotheses were never rerun. After an interruption, only unfinished questions
were reseeded under fresh user and pair-ID namespaces, then mechanically
redistributed across otherwise identical frozen-image shards. Scheduling and
container-memory changes cannot alter a question's history, model, tools, or
scored answer contract.

No partial aggregate is published. The result generator rejects missing,
duplicate, unexpected, or non-officially-judged question records. Native empty
answers are reported explicitly; they are not retried or relabeled into a more
favorable result.
