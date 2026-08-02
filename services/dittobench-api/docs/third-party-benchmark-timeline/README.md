# Native Hermes and OpenClaw across DittoBench v2-v6

This directory records one full, same-seed practice run of each immutable
DittoBench contract from v2 through v6 for two off-network reference harnesses:

- [Hermes Agent 0.19.0 native SessionDB](results/2026-07-23-hermes-openrouter-nebius-v2-v6.json)
- [OpenClaw 2026.7.1 native memory-core](results/2026-07-23-openclaw-openrouter-nebius-v2-v6.json)

These are reference measurements, not miner submissions. They never enter the
leaderboard, KOTH, validator weights, emissions, or payouts.

## Result

| Contract | Release | Cases (tool + memory) | Hermes memory | OpenClaw memory |
| --- | --- | ---: | ---: | ---: |
| v2 | 2026-07-07 | 114 (60 + 54) | 0.111111 · 6/54 fully correct | 0.333333 · 18/54 fully correct |
| v3 | 2026-07-18 | 119 (60 + 59) | 0.127966 · 6/59 fully correct | 0.377119 · 22/59 fully correct |
| v4 | 2026-07-19 | 119 (60 + 59) | 0.177966 · 10/59 fully correct | 0.355932 · 21/59 fully correct |
| v5 | 2026-07-21 | 206 (110 + 96) | 0.169071 · 16/96 fully correct | **0.421875 · 40/96 fully correct** |
| v6 | 2026-07-21 | 206 (110 + 96) | **0.197917 · 19/96 fully correct** | 0.333333 · 32/96 fully correct |

The requested monotonic-decline hypothesis is **not supported** by this sample.
Hermes rises from 0.111111 on v2 to 0.197917 on v6, with only a small v4-to-v5
dip. OpenClaw rises through v3, dips on v4, peaks on v5, and returns to its v2
score on v6. That does not mean the contracts are equally difficult: their case
mix and grader semantics change, v5/v6 administer substantially more cases, and
v4 is a false-positive correction rather than a simple difficulty increase. It
does mean the plotted reference lines must show the measured non-monotonic
values instead of being shaped to a preferred narrative.

This is one stochastic model sample on one shared seed, not a confidence
interval. The useful comparison is the size of the live miner/reference gap on
the memory subscore, with the production miner line separately identified as a
three-validator median.

## Locked comparison contract

Both matrices used:

- seed `3058240546919425205` and the same generated dataset hash within each
  benchmark version;
- `run_size=full` and the public API's normal case concurrency of eight;
- `qwen/qwen3-32b`, thinking disabled, non-streaming;
- OpenRouter with Nebius required, fallbacks disabled, and relay profile
  `openrouter-nebius-qwen3-32b-no-think-v1`;
- the regular deterministic generator and grader for each historical contract;
- an 8,192-token output cap in both adapters;
- no expected-answer access, category router, benchmark-specific skill,
  reranker, or adapter-supplied external memory system.

All ten retained runs completed with zero relay infrastructure failures, zero
caller cancellations, and zero timeout/no-response cases. The JSON records bind
every run to its run ID, dataset SHA-256, transcript SHA-256, aggregate result,
native retrieval trace count, latency, and per-run relay accounting. Raw fresh
datasets and answer-bearing transcripts remain local.

## What “native” means here

Hermes imports each seeded conversation into its native per-user SessionDB and
lets the ordinary Hermes agent loop decide whether and how to call upstream
`session_search`. No Ditto memory aliases are exposed. The retained runs used
`session_search` in 26/54, 18/59, 26/59, 35/96, and 40/96 memory cases from v2
through v6.

OpenClaw imports the same conversations verbatim into per-user workspace
Markdown, refreshes the active native `memory-core` SQLite FTS index, and asks
questions through the ordinary embedded OpenClaw agent loop. The favorable
condition uses a 20-result native recall window and
`memorySearch.provider="none"`, so it adds no embedding provider. OpenClaw selected its native-backed
memory tools in 44/54, 42/59, 46/59, 72/96, and 67/96 memory cases.

Hermes' native tool name does not match DittoBench's public memory aliases, so
its tool-use subscore and composite are not a clean cross-harness comparison.
The public timeline therefore plots only the deterministically graded memory
subscore for both references.

## Hermes output-cap correction and exclusions

Hermes upstream defaults `max_tokens` to 65,536. The pinned Nebius route has a
40,960-token context window, so the unmodified default produced avoidable HTTP
400 responses before ordinary prompt and tool-schema tokens were counted. The
adapter now defaults to 8,192, matching OpenClaw while leaving ample room for
benchmark responses and tool continuations.

Three pre-correction setup runs are excluded and identified in the Hermes JSON.
The first corrected v2 attempt is also excluded because its relay recorded two
infrastructure failures and eight timeout/no-response cases. The published v2
point is a same-seed clean replay after v6; its 212 relay requests all
succeeded. No attempt was silently replaced: every exclusion and reason is in
the aggregate record.

## Source boundary

- DittoBench API base: `444422cb970f949912eabeacfea3133790023759`
- DittoBench datagen: `55343ff961e36ebaa1c7db378dd7e5e0663ff5f3`
- locked relay: `d15b62a00f41fb9e71b71019698b65c13becb2e4`
- Hermes upstream: `e57918ac800121cf9c2956fe55e27df3ea80b562`
- OpenClaw upstream: `2d2ddc43d0dcf71f31283d780f9fe9ff4cc04fe4`

The adapter READMEs document the exact ingestion, isolation, lifecycle, and tool
boundaries. The earlier v6 experiments remain unchanged in their original
directories; this matrix is an additional controlled observation.
