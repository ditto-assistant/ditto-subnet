# Reference baseline

What the stock kit (unmodified `baseline.rs`) scores under the locked model. This
is the target to beat: a competitive submission has to clear the composite below,
and the weakest categories are where the reference harness leaves the most on the
table.

## bench_version 6 (2026-07-21)

- Harness: stock reference (this kit, unmodified `baseline.rs`).
- Model: Qwen3-32B via OpenRouter `qwen/qwen3-32b` (the locked scored model).
- Method: 6 distinct seeds at `run_size=full`, each a fresh anti-cheat dataset,
  scored through the released v6 pipeline (`bench_version=6`, datagen v0.11.0),
  isolated harness store per run.

| metric | mean | SE | 95% CI |
| --- | --- | --- | --- |
| composite | 0.242 | 0.020 | [0.202, 0.282] |
| tool_mean | 0.761 | 0.015 | [0.731, 0.791] |
| memory_mean | 0.200 | 0.013 | [0.174, 0.226] |

v6 keeps the v5 suite and adds four complexity classes (memory-as-data /
stored-instruction injection, multi-query fan-out, non-verbatim computed answers,
and passive cross-session consolidation). The tool half holds at its ceiling
(0.761); the composite moved down slightly from v5 (0.263) because the new memory
classes are genuinely hard for a simple retrieval kit.

## New v6 complexity classes (your biggest levers)

The stock kit scores near zero on every new class: each rewards a distinct
aligned capability the parser/simple-retrieval baseline cannot fake:

| category | stock mean | what it rewards |
| --- | --- | --- |
| nonverbatim-computed | 0.00 | converting a stored unit (the answer is in no message; grep fails) |
| passive-consolidation | 0.00 | retrieving the earliest fact of a topic accrued across sessions |
| stored-instruction-benign | 0.00 | applying a legitimately-stored preference (not blanket-refusing) |
| injection-stored-instruction | 0.04 | treating a stored override note as data, not a command |
| multi-query-recall | 0.08 | fanning out two focused queries and intersecting the results |

These are the highest-upside categories: a harness that does real retrieval,
reranking, and grounding clears them where a grep parser scores zero.

## bench_version 5 (2026-07-21)

- Harness: stock reference (this kit, unmodified `baseline.rs`) at `main`
  (`106076a`).
- Model: Qwen3-32B. Backend: OpenRouter `qwen/qwen3-32b`, the locked scored
  model; the on-chain scored backend is Chutes `Qwen/Qwen3-32B-TEE` (same weights,
  may differ slightly).
- Method: 10 distinct seeds at `run_size=full`, each a fresh anti-cheat dataset,
  scored through the released v5 pipeline (`bench_version=5`, datagen `v0.10.0`).
  Each run used an isolated harness with its own store (the kit's `/seed` appends,
  so runs must not share a DB). SE is the standard error of the mean over seeds.

| metric | mean | SE | 95% CI |
| --- | --- | --- | --- |
| composite | 0.263 | 0.012 | [0.239, 0.287] |
| tool_mean | 0.763 | 0.007 | [0.748, 0.777] |
| memory_mean | 0.226 | 0.012 | [0.202, 0.250] |

Gates: conversational-sanity 0.200, metamorphic-consistency 0.889,
tool-efficiency 1.000. Token-efficiency is neutral (1.000) here: this is a
direct-provider reference run, not one metered at the validator model-proxy, so
no relay-token budget applies. Median latency about 13 s per case (reported, not
scored).

v5 is a harder contract than v2 (composite 0.492), by construction. The **tool
half stays near its v2 ceiling** (0.763 vs 0.793): tool selection is not where
the competition is. Everything moved to the **memory half (0.226)** and the new
gates:

- **Memory is retrieval-recall-bound.** v5 scales the store (164 seeded pairs, 28
  subjects, 4 waves) and coins per-seed values, so simple top-k retrieval misses
  far more than it did in v2. This is a recall gap, not an unwinnable task: an
  ideal full-context reader of the same datasets (every memory in context, no
  retrieval step) scores ~0.85 overall and 1.00 on single-session-recall, versus
  the stock kit's 0.08: the answers are there, the stock retriever just doesn't
  surface them. Better retrieval is the first lever.
- **Conversational-sanity (0.200) is a real weak spot for the stock kit**, not a
  strength: it is a retrieval kit, not conversationally grounded, so it leaks on
  greetings and misses no-save-verb declaratives. This is exactly the dimension
  that separates a grounded harness from a v4-style phrase-list router
  (`dittobench-api/docs/V5-HARNESS-REPLAY.md`).

## Weakest categories (your levers)

The reference harness fails these most often, so they carry the most upside:

| category | stock mean |
| --- | --- |
| calendar_create | 0.00 |
| multi-hop-relational | 0.00 |
| temporal-depth | 0.00 |
| point-in-time | 0.00 |
| canary | 0.00 |
| injection-resistance | 0.02 |
| assistant-recall | 0.03 |
| multi-session | 0.03 |

`calendar_create` is 0.00 for the same reason as v2 (the stock model never selects
the calendar-create tool though it is offered every run; a tool-use prompt fixes
it). `multi-hop-relational` and `temporal-depth` are the new v5 capability
dimensions (cross-session KG-join and second-most-recent-value): the stock kit has
no join or temporal-depth logic, so it scores 0.00: the largest structured
levers, and winnable (a strong retrieval harness clears them). The remaining rows
are retrieval-recall and reasoning gaps.

Reproduce or extend this run with the harness pointed at the locked model over
`run_size=full` seeds through a `bench_version=5` scorer.

## bench_version 2 (2026-07-12)

- Harness: stock reference (this kit, unmodified).
- Model: Qwen3-32B. Backend: OpenRouter `qwen/qwen3-32b`, the same weights the
  scored Chutes `Qwen/Qwen3-32B-TEE` serves. The TEE is the exact scored backend
  and may differ slightly.
- Method: 24 distinct seeds at `run_size=full`, so the spread is real run-to-run
  variance (fresh dataset per run plus model noise). SE is the standard error of
  the mean.

| metric | mean | SE | 95% CI |
| --- | --- | --- | --- |
| composite | 0.492 | 0.013 | [0.467, 0.517] |
| tool_mean | 0.793 | 0.007 | [0.779, 0.806] |
| memory_mean | 0.419 | 0.019 | [0.382, 0.456] |

Gates: canary pass rate 0.333, metamorphic consistency 0.347. Median latency
about 14 s per case (reported, not scored).

The tool half is near the ceiling; the memory half (0.419) is where the composite
and the competition live, which is why retrieval is the first lever in
[README](README.md) *How to optimize*.

## Weakest categories (your levers)

The reference harness fails these most often, so they carry the most upside:

| category | stock mean |
| --- | --- |
| calendar_create | 0.00 |
| injection-resistance | 0.07 |
| computed-answer | 0.08 |
| knowledge-update | 0.18 |
| preference-application | 0.18 |
| temporal-reasoning | 0.22 |
| assistant-recall | 0.32 |

`calendar_create` is 0.00 because the stock model never selects the calendar-create
tool though it is offered every run; a tool-use prompt fixes it. The memory rows
are retrieval and reasoning gaps (see the per-question-type levers in the README).

Reproduce or extend this run with the harness pointed at the locked model over
`run_size=full` seeds.
