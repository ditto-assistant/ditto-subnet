# DittoBench wire protocol

All shapes below are JSON with `snake_case` keys, matching the Go validator's
wire contract. The Rust definitions live in [`src/protocol.rs`](src/protocol.rs).

## HTTP endpoints (your miner serves these)

### `GET /health`
Returns `200 {"status":"ok"}`.

### `POST /run`
The validator POSTs one v8 case at a time. `bench_version` is required and must
be `8`. Scored tool cases include `tool_endpoint`; the harness must execute
non-memory tools through it so the validator observes the trajectory. `user_id`
selects the case's isolated memory graph.

Request body, `RunRequest`:
```json
{
  "case_id": "web_search-42-0001",
  "bench_version": 8,
  "system_prompt": "You are Ditto...",
  "user_input": "What's the latest on quantum computing?",
  "tools": [
    { "name": "search_web", "description": "...", "parameters": { "type": "object", "properties": { "query": { "type": "string" } }, "required": ["query"] } }
  ]
}
```

Response body, `RunResponse`:
```json
{
  "final_text": "Here's what I found...",
  "tool_calls": [ { "name": "search_web", "args": { "query": "quantum computing" }, "hop": 0 } ],
  "prompt_tokens": 1234,
  "output_tokens": 56,
  "latency_ms": 812,
  "answer": "quantum error correction",
  "abstain": false
}
```

Two optional response fields are worth wiring:

- `answer`: the bare value your `final_text` asserts (a name, a number, a
  comma-separated list). The deterministic grader matches the slot when
  present and falls back to prose containment, so populating it removes
  prose-phrasing risk from grading.
- `abstain`: set `true` for a grounded decline ("that was never mentioned").
  It is the primary decline signal; decline phrasing in `final_text` is the
  fallback. Abstaining on an answerable case scores 0, so gate it on
  retrieval actually coming up empty.
### `POST /seed`
Before asking memory questions the validator installs a fresh haystack.

Request body, `SeedRequest`:
```json
{
  "user_id": "miner",
  "wave": 0,
  "pairs":    [ { "pair_id": "p-0-1", "session_id": "sess-0", "timestamp": "2025-11-03T09:00:00Z", "prompt": "I just moved to Lisbon.", "response": "Noted that you live in Lisbon now." } ],
  "subjects": [ { "id": "subj-city", "subject_text": "City", "description_text": "..." } ],
  "links":    [ { "subject_id": "subj-city", "pair_id": "p-0-1" } ]
}
```
Respond `200 { "pairs": N, "subjects": N, "links": N }` (counts loaded).

DittoBench v8 memory seeding modes:
- Prepared: `pairs`, `subjects`, and `links` are all provided (retrieval in isolation).
- Raw-pairs: `subjects: []`, `links: []`, so only raw conversation
  pairs are seeded. Your harness must build its own subject index from the
  pairs to route subject-scoped questions. A harness that relies on prepared
  subjects scores materially lower here.
- Staged: `/seed` is called repeatedly, each with an incremented
  `wave`, interleaved with `/run`. Seeding is an idempotent upsert: accept
  each wave and merge. Questions may target facts from any wave already seeded.

## Observed tool execution

Every scored v8 tool case carries `tool_endpoint`. Local memory-only practice
may omit it.

- `tool_endpoint`: a validator-served mock tool-execution URL. When present,
  the harness should execute each non-memory catalog tool call by POSTing
  a `ToolExecRequest` there and feeding the returned result back to the model,
  instead of stubbing the tool locally. The validator records those calls as
  the authoritative observed trajectory and can grade whether the answer
  incorporates the returned content.
- `user_id`: the memory graph this case must be answered from (multi-graph
  isolation). Answer only from this user's memory, never leak another user's
  facts. Absent means the default single-user graph.

The round-trip per tool call is `ToolExecRequest` (`hop` is the 0-based order of
the call within the case):
```json
{ "case_id": "web_result_usage-1-0", "user_id": "colleague", "name": "search_web", "args": { "query": "veltrix index" }, "hop": 0 }
```
`ToolExecResponse`:
```json
{ "result": "the Veltrix index reached 3,418 points" }
```
Memory tools are not served by the endpoint. It replies with an empty
`result` and an `error` (e.g. `{"error": "tool not available via this endpoint:
search_memories"}`); treat that like a real tool error.

On result-usage cases the validator additionally grades whether the final
answer incorporates the value the executed tool returned, reported per case as
`CaseScore.result_usage` (0-1).

A harness that ignores `tool_endpoint` scores 0 on the on-chain scored path.

## Dataset shapes (local practice)

- `Dataset { seed, generated_at, tool_cases[], memory_cases[] }`
- `ToolCase { id, category, prompt, expected_tools[], max_tool_calls, allow_extra_tools, expected_behavior }`
- `ToolSpec { name, required_args?, forbidden_args? }`
- `MemoryCase { id, question, expected_answer, seed_memories[] }`
- `SeedMemory { prompt, response, days_ago }`
- `ToolDefWire { name, description, parameters }`

## Score shapes

- `CaseScore { case_id, category, tool_score, result_usage, latency_ms, called[], expected[], notes[] }`
  (`result_usage` is emitted only on observed-execution result-usage cases; omitted when 0)
- `ScoreReport { run_id, generated_at, composite, tool_mean, memory_mean, median_ms, n, per_case[] }`

### Scoring rules (local scorer; versioned on-chain differences below)

Scoring is judge-free everywhere: deterministic, no LLM, and locally identical
in kind to the on-chain grader.

Each tool case scores its deterministic tool-accuracy:

- `matched = Σ min(expected_count, observed_count)` over expected tool names
- `base = matched / total_expected`
- `-0.1` per unexpected extra call (skipped when `allow_extra_tools`)
- `score = clamp(base - penalty, 0, 1)`
- no-expected-tool cases score `1.0` iff nothing was called, else `0.0`

Memory accuracy uses the deterministic grader (`src/grade.rs`, mirroring the
validator's public `dittobench-datagen/grade`): the expected value must appear
in the response's `answer` slot (or `final_text` as fallback) by normalized
bounded containment, with an exact number-token path for numeric answers.
Abstaining on an answerable case scores 0.

`composite = 0.5 * tool_mean + 0.5 * memory_mean` when both kinds are present;
otherwise it equals whichever mean exists. This local scorer is only a practice
approximation. The v8 on-chain scorer additionally applies the published
integrity and efficiency factors below.

### On-chain tool grading and composite factors (differ from the local scorer)

The deterministic half of each tool case is graded on-chain as:

```
0.4 × tool-name F1  +  0.4 × argument F1  +  0.2 × trajectory/order credit
```

The on-chain composite (`0.5 × tool_mean + 0.5 × memory_mean`) is then multiplied
by up to three bounded integrity factors. Each is `1.0` (no effect) when its
trigger is absent, so accuracy stays dominant and every factor is a pure function
of already-published per-case results (re-derivable from the run details):

- Tool efficiency (observed-execution runs): bounded to `[0.85, 1.0]`. The first
  extra tool call is free, then the over-call penalty saturates.
- Canary integrity (every run): a per-run seed-derived nonce is planted in the
  conversation and one memory case asks for it. An honest recall miss (the nonce
  is neither surfaced nor leaked) is a bounded `×0.85`; surfacing the planted
  decoy nonce (a cross-user leak) is a hard `×0.5` disqualifier that easy recall
  elsewhere cannot buy back. A harness with a lexical nonce index passes and is
  unaffected. (The canary is also one graded memory case in `memory_mean`.)
- Metamorphic consistency (runs with invariance families): bounded to
  `[0.85, 1.0]`. Each family is one fact asked several ways; a full run carries
  several families. The factor is `1 − 0.15 × (fraction of families answered
  inconsistently)`, so a phrasing-brittle harness that splits families scores
  below a grounded one, while answering every sibling of a family alike leaves it
  at `1.0`.

### On-chain timeouts

| Call | Ceiling |
| --- | --- |
| `GET /health` (container start to healthy) | 10 s |
| `POST /run` (per case, a miss scores 0) | 60 s |
| `POST /seed` (per wave) | 5 min |
