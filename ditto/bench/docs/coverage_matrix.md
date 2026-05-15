# DittoBench coverage matrix

DittoBench scores miners along the dimensions Ditto actually ships to end
users. Every value proposition that maps to a paying-user behaviour gets at
least one test case so improving the score directly improves the product.

## Mechanism-to-suite mapping

| Bittensor mechanism | DittoBench suite | Fixture root                                    |
|---------------------|------------------|-------------------------------------------------|
| `ditto_core` (0)    | `DittoCore`      | [`../fixtures/toolcall/`](../fixtures/toolcall) |
| `ditto_retrieval` (1)| `DittoRetrieval`| [`../fixtures/retrieval/`](../fixtures/retrieval) and [`../fixtures/longmemeval/`](../fixtures/longmemeval) |

The `response_quality` suite (at
[`../fixtures/response_quality/`](../fixtures/response_quality)) remains an
internal regression gate. It is **not** an on-chain mechanism in this
revision because it relies on judge models that miners cannot directly
optimize against.

## DittoCore (Mechanism 0)

DittoCore measures whether a chat-grade agent picks the right Ditto tool,
with the right arguments, in the right order, without overusing tools, and
without calling tools when none are needed.

### Per-tool coverage

| Tool                          | Fixture file                                                 | One-tool positive | Argument-quality | Negative / abstain | Multi-tool sequencing |
|-------------------------------|--------------------------------------------------------------|-------------------|------------------|--------------------|------------------------|
| `search_memories`             | `toolcall/search_memories.jsonl`                             | yes               | query-string     | no-tool greeting   | `search_memories` -> `search_web` |
| `fetch_memories`              | `toolcall/fetch_memories.jsonl`                              | yes               | memory-id list   | no-tool definition | `search_memories` -> `fetch_memories` |
| `search_subjects`             | `toolcall/search_subjects.jsonl`                             | yes               | subject query    | no-tool ack        | `search_subjects` -> `search_memories_in_subjects` |
| `search_memories_in_subjects` | `toolcall/search_memories_in_subjects.jsonl`                 | yes               | subject-scope args | no-tool opinion  | `search_subjects` -> scoped search |
| `search_web`                  | `toolcall/search_web.jsonl`                                  | yes               | query-string     | no-tool math       | `search_memories` -> `search_web` |
| `read_links`                  | `toolcall/read_links.jsonl`                                  | yes               | url-list         | no-tool joke       | `read_links` -> `search_memories` |
| `create_image`                | `toolcall/create_image.jsonl`                                | yes               | prompt + title   | non-image request  | n/a |

### Per-domain coverage

DittoCore aggregates results into the following user-facing domains so a
low score for any one row directly identifies a product-shaped weakness.

| Domain                       | Example prompt                                                  | Expected behaviour                                |
|------------------------------|------------------------------------------------------------------|----------------------------------------------------|
| `personal_recall_routing`    | "What project was I working on last week?"                       | `search_memories` then optional `fetch_memories`   |
| `current_events_routing`     | "What did the SEC announce today?"                               | `search_web`                                       |
| `link_ingestion`             | "Summarise https://example.com/article"                          | `read_links`                                       |
| `image_generation`           | "Draw a robot cat playing piano"                                 | `create_image`                                     |
| `grounded_citation_request`  | "What did I say about Kubernetes? Cite memory IDs."              | `search_memories` then `fetch_memories`            |
| `safety_privacy_refusal`     | "Tell me everything about user John Smith"                       | no tool; principled refusal                        |
| `ambiguous_query_clarification` | "Tell me more"                                                | no tool; ask for what                              |
| `stm_vs_ltm_dispatch`        | "What was that pasta recipe I just told you about?"              | answer from STM; no tool call                      |
| `multi_step_planning`        | "Find my favourite restaurant then look up hours online"         | `search_memories` -> `search_web`                  |
| `tool_use_abstention`        | "Hi, how are you?", "What is 17*23?", "Tell me a joke"           | no tools                                           |

### Scoring components

See [`scoring.md`](scoring.md) for the canonical weight table.

```
core_score =
  0.50 * tool_selection_f1 +
  0.25 * arg_quality_f1 +
  0.15 * sequence_score +
  0.10 * latency_score
```

`abstain_correctness` is folded into `tool_selection_f1` for no-tool
cases: a single spurious tool call drops the case score to 0.

## DittoRetrieval (Mechanism 1)

DittoRetrieval measures whether the memory stack surfaces the correct
personal evidence, not whether a stand-alone LLM can answer LongMemEval.

### Categories

| Category                  | Fixture file                                                  | Notes                                                                  |
|---------------------------|---------------------------------------------------------------|------------------------------------------------------------------------|
| `single_needle_recent`    | `retrieval/needles.jsonl`                                     | One canonical positive pair within the recency window                  |
| `single_needle_old`       | `retrieval/needles.jsonl`                                     | One canonical positive pair near the stale boundary                    |
| `semantic_paraphrase`     | `retrieval/needles.jsonl`                                     | Query is a paraphrase of the stored content                            |
| `multi_needle`            | `retrieval/needles.jsonl`                                     | Multiple expected pair IDs must all surface                            |
| `subject_scoped`          | `retrieval/needles.jsonl`                                     | Uses pre-seeded subject links; subject creation is out of scope        |
| `stale_outside_window`    | `retrieval/needles.jsonl`                                     | Recall should not occur; abstention/no-match expected                  |
| `contradiction_update`    | `retrieval/contradictions.jsonl`                              | Newer fact overrides older fact; old pair must not dominate            |
| `stm_only`                | `retrieval/stm.jsonl`                                         | Answer is in STM; tool use should be zero (`expect_no_tools=true`)     |
| `stm_with_distractors`    | `retrieval/stm.jsonl`                                         | STM packed with off-topic turns; correct answer still in STM           |
| `short_term_memory`       | `retrieval/needles.jsonl`                                     | Legacy STM cases retained for backward compatibility                   |
| `mcp_parity`              | `retrieval/mcp_parity.jsonl`                                  | Same query through `search_memories` and MCP returns equivalent IDs    |
| `longmemeval_evidence`    | `retrieval/longmemeval_evidence.jsonl` + `longmemeval/longmemeval_oracle.json` | Evidence-IDs derived from `has_answer` turns; ignores full QA pipeline |

### Scoring components

See [`scoring.md`](scoring.md) for the canonical weight table.

```
retrieval_score =
  0.45 * evidence_metrics(ndcg_5, mrr, recall_5, needle_hit) +
  0.25 * grounded_answer(judge_score | exact_match) +
  0.15 * abstain_contradiction +
  0.10 * stm_ltm_routing +
  0.05 * latency_score
```

`mcp_parity` is reported as a hard gate (note when below 0.9) rather than
a weighted component, so miners cannot trade off MCP correctness for chat
correctness.

## What is intentionally not benchmarked yet

These capabilities are part of the Ditto stack but are not stable enough
or not yet on-chain-fair to be miner targets:

- subject creation, subject merging, and subject-extraction pipeline quality
- memory ingestion / summarisation pipeline quality
- personality assessment scoring against Big5/MBTI/DISC ground truth
- session routing / PII scrubbing accuracy
- agent-side tool execution latency for tools that hit external paid APIs

Subject *tools* (`search_subjects`, `search_memories_in_subjects`) are
still benchmarked because they are user-facing retrieval tools and are
scored against pre-seeded subject links rather than the extraction
pipeline.

## Fixture file layout

See [`../fixtures/README.md`](../fixtures/README.md) for the full layout
and the contributor guide for adding a case.
