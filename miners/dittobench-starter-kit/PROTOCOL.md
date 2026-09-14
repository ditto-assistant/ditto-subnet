# DittoBench wire protocol

All shapes below are JSON with `snake_case` keys, matching the Go validator's
wire contract. The Rust definitions live in [`src/protocol.rs`](src/protocol.rs).

## HTTP endpoints (your miner serves these)

### `GET /health`
Returns `200 {"status":"ok"}`.

### `POST /run`
The validator POSTs one case at a time. `bench_version` is required. This
starter accepts v8 through v13 (`MIN_SUPPORTED_BENCH_VERSION..=MAX_SUPPORTED_BENCH_VERSION`
in `src/protocol.rs`); `cargo run -- evaluate` stays pinned to
`ACTIVE_BENCH_VERSION` (v9). A scored run tells the harness the **public wire
version, 9**, whatever contract is being scored: every v10–v13 addition a
harness can see (per-seed catalog, `enum` schemas, coined decoy tools, richer
descriptions) is an additive optional field, so a harness never branches on
the version. Scored tool cases include `tool_endpoint`; the harness must execute
non-memory tools through it so the validator observes the trajectory. `user_id`
selects the case's isolated memory graph.

V9 identifiers are opaque capabilities. Persist UUID-shaped `case_id`,
`user_id`, `pair_id`, `session_id`, and `subject_id` values exactly and compare
them only for equality; never derive family, order, or grading behavior from
their spelling. V9 `/seed` omits `wave` and always includes the `pairs`,
`subjects`, and `links` arrays. Repeated calls remain ordered idempotent upserts.
V9 `/run` always carries its opaque user capability. Return supplied opaque
`pair_id`, `pairIds`, and `subject_id` values unchanged in tool calls.

Prompts, memory text, timestamps, subject descriptions, tool schemas, and tool
results retain product semantics. Benchmark seed, run size, digest,
question/family/category labels, expected answers, grader state, and ontology
are intentionally absent, as is arbitrary caller-provided container environment.

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

with the v7+ strict rules on top: a forbidden argument zeroes the case, an
out-of-order chain multiplies the whole score by its order credit, extra and
over-budget calls carry a doubled penalty, an observable case that never ran
through `tool_endpoint` scores 0 in scored scope, a result-usage case keeps only
`0.1 ×` its trajectory credit when the answer ignores the served value and 0
when it carries the served decoy, and (v10+) a call the model never emitted
through the relay earns nothing.

The on-chain composite (`0.5 × tool_mean + 0.5 × memory_mean`) is then multiplied
by the v7+ composite gate. Every factor is `1.0` (no effect) when its trigger is
absent and is a pure function of already-published per-case results
(re-derivable from the dataset plus the transcript):

- Bounded style factors, whose product is floored at `0.40`:
  - Tool efficiency (observed cases scoring ≥ 0.6, excluding memory-routing,
    `allow_extra_tools`, and unobserved cases): **no free overshoot**; the penalty
    grows linearly from the first extra call and saturates at +3 extra calls at a
    40 % maximum.
  - Metamorphic consistency: each invariance family is one fact asked several
    ways; the factor is `1 − 0.40 × (fraction of families answered
    inconsistently)`.
  - Memory over-call: up to a 25 % penalty for calling non-memory tools on
    memory-routing cases.
- Unfloored integrity tiers:
  - Canary integrity: a per-run seed-derived nonce is planted in the
    conversation and one memory case asks for it. Surfacing the planted decoy
    nonce (a cross-user leak) is `×0.25`; an honest miss only costs that one
    memory case.
  - Conversational sanity: `0.25 + 0.75 × geomean` over the chit-chat,
    declarative, and declarative-behavior slices — a slice fully failed drops the
    factor to 0.25.
  - Reproduce-under-transform audit (enforced): up to a 40 % penalty when base
    phrasings are answered correctly materially more often than their
    transformed twins (needs ≥ 4 pairs).
- v9+ score gates (`scoregates`): model-use and authoritative-tool coverage are
  binary (a zero-inference run zeroes), v12 adds model dependence, an
  inference-latency check, and a capped answer-stuffing penalty. Token counts do
  not move the composite (v7+ token contract is quality-only); the curve-v3
  efficiency adjustment on Platform only breaks exact quality ties.

The pre-v7 description that used to sit here (`[0.85, 1.0]` factors, a free
first extra call, a `×0.85` canary miss) is retired: it described the v3–v6
contracts.

### Bench v13: prose-graded claims and relay-observed gates

Bench v13 (`research/dittobench-datagen/docs/bench-versions.md` "Bench v13",
`services/dittobench-api/PROTOCOL.md` "bench_version 13") keeps this wire
contract and changes what is graded and what is watched. The wire
`bench_version` stays 9. Every rule is stated so that a correct, model-emitted
answer can never be zeroed by it; each names the substitution it charges.

**Grading (memory cases).** The positive check runs on `final_text ∪ answer`.
The `answer` slot is a tie-break, not the graded object: a slot whose value has
no equivalent asserted in the prose scores 0 (`slot_not_in_prose`), so a host
extractor may only ever copy the model's own words. This kit populates the slot
only when `DITTOBENCH_ANSWER_SLOT` is set (`v13::ANSWER_SLOT_ENV`; the `--gates`
rehearsal sets it): the wire stays at bench 9, so the slot cannot be gated on
the contract version, and under the live v9+ grading policy a populated slot is
authoritative with no prose fallback — a default-on slot would change live v12
scores. With the switch on, the policy prompt asks the model for a final
`Answer: <value>` line and the slot is that line, copied verbatim. Quantities are graded in the
unit the question asked for (a bare `411067` on a minor-unit question is
correct; `$4,110.67` beside it is the same value). More than two distinct
candidates for one scalar claim, or two contradictory ones, score 0; a value
cited and rejected as insufficient evidence is not a candidate. New kinds:
`clarify` (an ambiguous or missing-detail request expects a clarifying question
that **names the missing detail** — schema name, description noun, or a
synonym — and **cites a token from the records searched**; "what would you
like?" scores 0) and `absence` (a grounded decline that says what was found;
a generic refusal scores 0). Declarative acknowledgement without the stated
value earns 0.25.

**Catalog-present gate (tool cases; shadow in v13.0).** The relay records what
the harness *offered* the model on every completion. A no-tool case earns
restraint credit only when the model was offered a catalog
(`restraint_without_offer`); an expected tool never offered earns no credit
(`expected_tool_not_offered`); a model-emitted non-memory call the harness did
not execute is a host override (`swallowed_model_call`). Published safe
harbor: trimming is free when the retained set contains the **top-3 tools of
the published model-free embedding** (TF-IDF over name + description, cosine to
the request, ties on name — `v13::semantic_top_k` in this kit, `scorer.CatalogSemanticTopK`
in the validator's relay catalog gate), or when the catalog is merely non-empty
on a declarative/chit-chat/decline case (the negation family excepted). A
catalog of memory tools alone is not an offer (`memory_only_catalog`); an
expected tool the validator executed under proven model emission counts as
offered (`offer_inferred_from_execution`). Offering the full catalog always
passes.

**Provenance gate (shadow).** The graded CLAIM SPAN — the accepted
alternative of the expected answer (the major-unit form of a money answer, the
direction vocabulary, each list item, the value and its accept set) that is
wholly present in the served slot or prose, under the published normaliser
(`scoregates.NormalizeSpan`: NFKC, casefold, label and list-marker strip,
punctuation folded except `$ . , -`) — must be contained in the union of every
model completion's value tokens: message text, tool-call arguments (a
`final_answer` tool), or structured-output fields. A `/100` rescale, a
direction-word map, a composed slot, or a replaced draft fails
(`served_text_not_model_emitted`); a formatter of the model's own number,
JSON mode, markdown, and a splice of two completions pass. A served span that
carries no accepted alternative has no claim to check (`claim_not_applicable`):
the gate fails open, never guesses. `scripts/rehearsal_gates.py` is a verbatim
port tested against the Go vectors.

**Causal gate (shadow).** The graded claim must not have been authored by
the harness into a prompt span (system prompt, template text, assistant
prefill) before any completion produced it, unless that span is covered by a
`/seed` record, a delivered tool result, or the case's own question
(`answer_in_prompt`). Quoting retrieved memory into the prompt is honest RAG,
and re-injecting a value the model derived earlier ("format 4110.67 as
currency") is model-derived; computing the answer on the host and asking the
model to repeat it is not.

**Twin / pair post-pass (shadow).** Decision twins and as-of twins are
distributionally matched pairs where the same surface demands a different
decision; an identical decision across the pair is an evidence-independent
default (`twin_concordant`; rule R1 zeroes the group, R2 takes the pair
product — chosen at calibration). A metamorphic counterfactual answered like its
base zeroes only that pair (`counterfactual_insensitive`). The twin relation is
grader-only (never on the wire or in the pass-off artifact); the local replay
reads it from the scorer report's `per_case[].relation`, so decision-twin notes
need `--report`. Metamorphic relations ride on the artifact
(`v10_provenance.relation`).

**Local replay.** `python3 scripts/local-rehearsal.py --bench-version 13
--gates` (or `uv run ditto practice --bench-version 13 --gates`) records what
this kit offered and emitted per case (`DITTOBENCH_COMPLETION_LOG`), turns the
`answer` slot on (`DITTOBENCH_ANSWER_SLOT=1`), and replays
every rule above against the public (salt 0) pass-off artifact, printing
per-case notes and a shadow gate-induced loss. `--keep-artifacts DIR` keeps the
dataset, transcript, completion log, and result for an offline re-run with
`scripts/rehearsal_gates.py`. The validator's relay is the authoritative
evidence source; the local log is the same rule over the harness's own view.
The rehearsal refuses a `--bench-version` the local scorer build does not
advertise (`GET /v1/capabilities` `supported_bench_versions`), so the kit
ceiling (`MAX_BENCH_VERSION`, 13) leading the scorer is a loud, early error
rather than a `/v1/submit` 400.

### On-chain timeouts

| Call | Ceiling |
| --- | --- |
| `GET /health` (container start to healthy) | 10 s |
| `POST /run` (per case, a miss scores 0) | 60 s |
| `POST /seed` (per wave) | 5 min |
