# OpenClaw on DittoBench v6

This directory records two full DittoBench v6 measurements of OpenClaw's
native file-backed memory on the same seed and generated dataset:

- the native profile in
  [`results/2026-07-22-openclaw-openrouter-v6.json`](results/2026-07-22-openclaw-openrouter-v6.json);
- a separately labeled favorable native-recall replay in
  [`results/2026-07-22-openclaw-favorable-openrouter-v6.json`](results/2026-07-22-openclaw-favorable-openrouter-v6.json).

The adapter is under [`integrations/openclaw`](../../integrations/openclaw/README.md).
These are reference-only practice results. They are not miner submissions,
leaderboard entries, KOTH candidates, validator weights, or payout inputs.

The later same-model, pinned-provider v2-v6 matrix is recorded separately in
[`docs/third-party-benchmark-timeline`](../third-party-benchmark-timeline/README.md).
It preserves both v6 results above and adds historical-contract measurements
rather than replacing the original evidence.

## Paired result

| Metric | Native (10 results) | Favorable (20 results) | Delta |
| --- | ---: | ---: | ---: |
| Composite | **0.280702** | 0.277023 | -0.003679 |
| Tool mean | **0.879394** | 0.857273 | -0.022121 |
| Memory mean | 0.385417 | **0.427083** | +0.041667 |
| Correct memory cases | 37/96 | **41/96** | +4 |
| Composite standard error | **0.012289** | 0.012324 | +0.000035 |
| Median case latency | **7,341 ms** | 10,447 ms | +3,106 ms |
| Metamorphic consistency | **0.909091** | 0.727273 | -0.181818 |
| Tool efficiency | **1.0** | 0.999513 | -0.000487 |
| Provider failures / no-responses | 0 / 0 | 0 / 0 | — |

Both runs used all 206 cases generated from seed `3058240546919425205`.
Their dataset SHA-256 is identical. Both were routed automatically by
OpenRouter to DeepInfra, so the paired memory delta is not confounded by a
serving-provider change.

The favorable replay improved seven memory cases and regressed three, a net
gain of four. It improved native-memory writes, declarative write/read,
multi-query recall, and two abstentions. It regressed one single-session
recall, one memory write/read, and the canary from a correct answer to an honest
miss. Neither run emitted the canary's forbidden bait, so no integrity penalty
applied.

The tool and composite movement should not be attributed to the memory result
window: those cases do not consume seeded memory and Qwen sampling remained
stochastic. More paired seeds would be required to isolate a stable tool delta.
The platform comparison intentionally uses the favorable `0.427083` because it
is a memory-only chart and the stated goal is to give OpenClaw its stronger
native condition while preserving both runs here.

## What “native” means

OpenClaw documents Markdown files in `MEMORY.md` and `memory/*.md` as its
durable memory substrate, with the default `memory-core` plugin indexing those
files in SQLite. The adapter therefore:

- creates one physically isolated OpenClaw workspace and agent directory per
  benchmark `user_id`;
- imports every seeded conversation verbatim into deterministic
  `memory/pair-*.md` files, preserving pair ID, session ID, timestamp, user
  text, and assistant text;
- stores supplied subject/link records verbatim as Markdown-embedded JSON;
- synchronously refreshes OpenClaw's active `memory-core` index after every
  seed wave and native write;
- asks each question through OpenClaw's ordinary embedded agent loop and
  records the plugin tool trace selected by Qwen.

There is no adapter-written summary, vector database, reranker, query
rewriter, expected-answer access, case/category router, or benchmark-specific
skill. Evaluation transcripts live outside the indexed memory roots and are
cleared per case, preventing earlier questions from becoming later memories.

The measurement deliberately sets OpenClaw's documented
`memorySearch.provider: "none"`, which retains native SQLite FTS5 recall without
adding another credential or retrieval product. OpenClaw also supports hybrid
search when a separate embedding provider/model is configured. That would be a
valid future profile, but it would test an additional dependency and is not
silently folded into this native no-external-memory result.

## Tool boundary

DittoBench and OpenClaw use different public names for similar operations. A
normal OpenClaw plugin registers the exact validator-supplied catalog for each
case:

- `search_memories`, `search_subjects`, `fetch_memories`, and
  `search_memories_in_subjects` call the active native `memory-core` manager;
- `save_memory`, `update_memory`, and `delete_memory` mutate native Markdown
  files and refresh that same index;
- all non-memory tools post to the validator-owned `tool_endpoint`.

The model sees each tool's public schema and chooses every name and argument in
the native OpenClaw agent loop. The adapter does not call a tool because a case
expects it. Generic recall guidance is the name-adapted equivalent of
OpenClaw's own mandatory `memory_search` guidance, needed because the
DittoBench aliases replace the built-in name on the visible tool surface.

The favorable profile changes only `OPENCLAW_DITTOBENCH_SEARCH_LIMIT` from 10
to 20. It does not introduce embeddings, graph reconstruction, summaries,
answer extraction, or a different agent loop.

## Exact contract

Shared contract:

- DittoBench API evaluation engine: `8df6ba37a3c4e46ede9db1cb6014551d49ac6ba8`
- DittoBench datagen: `55343ff961e36ebaa1c7db378dd7e5e0663ff5f3`
- OpenClaw: `2d2ddc43d0dcf71f31283d780f9fe9ff4cc04fe4`
  (`2026.7.1`)
- benchmark: v6, `full`, 206 cases (110 tool and 96 memory)
- seed: `3058240546919425205`
- dataset SHA-256: `f9b8e6eea10f20316a6fc82c2f6b0af0f0cf0bc3f263cf96a760e68cdaa3ea2d`
- model: `qwen/qwen3-32b`, thinking disabled, non-streaming
- route: OpenRouter automatic routing; both retained runs used DeepInfra
- adapter source-tree SHA-256: `d7078fbc13fbd8172b847d0b6f78a3d8e70ab5dcc03bd24bc4237fc9d943e8bc`
- container image ID: `sha256:07a81912e66c1082edc3d305f8e0c37420f47b6b93a27ddf31dff181a6326add`

Native profile:

- run: `849ced7c-80fa-4eb3-a461-33cab373615f`
- transcript SHA-256: `a89495c29bcf04cddf75017ab00c0d9079abfa0ff520f80887e2c190af344aa5`
- relay: 402 requests, 1,801,631 tokens, no upstream errors, $0.147241
- elapsed: 6m42s

Favorable profile:

- run: `391f157e-85a8-4132-90f7-9f1930fddb6f`
- transcript SHA-256: `2d3a4dac96178dcd397371597aca3888e4dc5f3abc2d659eeb8ef7076e8c46a8`
- relay: 403 requests, 1,818,562 tokens, no upstream errors, $0.148592
- elapsed: 9m30s

The host-side relay kept the OpenRouter key out of the adapter container.
Practice scope cannot bind trusted relay accounting into the score, so the
token-efficiency multiplier remained neutral. Memory accuracy is
deterministically graded and unaffected by that neutral multiplier.

## Miner comparison and claim boundary

A live platform snapshot at `2026-07-22T19:06:16Z` contained 36 finalized v6
miners. The top five memory medians ranged from `0.884211` to `0.947917`; the
leader was about 2.2 times the favorable OpenClaw result. The dashboard reads
these miner bars live rather than freezing this snapshot.

That comparison is intentionally limited: miner values are 3-validator
production medians using independent seeds and the production model route,
while each OpenClaw value here is one OpenRouter practice run. The evidence
supports the narrower observation that the top miners scored much higher on
this contract, not a universal claim about every OpenClaw deployment or every
optional memory plugin.

OpenClaw did substantially better than the recorded favorable Hermes native
SessionDB result (`0.260417`). It still scored zero on temporal depth,
multi-session recall, point-in-time recall, preference application, and
defensive injection resistance in the favorable run. Those gaps, plus the
single-seed design, should remain visible alongside the headline comparison.

## Validation and excluded attempt

Validation included six adapter unit tests, all `dittobench-api` Go tests, a
native FTS seed/search/final-answer smoke, an unprivileged read-only Docker
smoke, per-user physical isolation, and two complete 206-case replays. The
container ran as UID 65532 with a read-only root filesystem, no-exec ephemeral
`/tmp`, all capabilities dropped, no-new-privileges, and CPU/memory/PID limits.

One earlier attempt (`e0546989-1e72-47f3-9a02-908efc8c018e`) is excluded. A
shared host relay owned by another task exited after 102 scored cases; the run
was cancelled at 106/206 after four infrastructure no-responses. No partial
score from that attempt is published or used in the comparison.

Raw generated datasets and question/answer transcripts remain local. The
committed aggregates and SHA-256 digests bind the retained evidence without
publishing fresh evaluation material.
