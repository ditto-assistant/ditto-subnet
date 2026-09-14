# DittoBench wire protocol

All shapes below are JSON with `snake_case` keys, matching the Go validator's
wire contract. The Rust definitions live in [`src/protocol.rs`](src/protocol.rs).

## HTTP endpoints (your miner serves these)

### `GET /health`
Returns `200 {"status":"ok"}`.

### `POST /run`
The validator may POST several cases concurrently (`case_concurrency`, 1–64).
`bench_version` is required. This starter accepts the inclusive range
`MIN_SUPPORTED_BENCH_VERSION..=MAX_SUPPORTED_BENCH_VERSION` in
[`src/protocol.rs`](src/protocol.rs) (v8 through v13 once the v13 starter-kit
and wiring PRs land, #1851 and #1519; accepting a version is not the same as
practising on it) and practices on `ACTIVE_BENCH_VERSION` (9). Every
contract from Bench v9 on reaches the harness as **wire version 9**
(`publicWireBenchVersion` in the validator): v10–v13 change the dataset, the
grader and the gates, never what your harness must advertise or branch on, so
never gate behaviour on a number above 9. Scored tool cases include
`tool_endpoint`; the harness must execute non-memory tools through it so the
validator observes the trajectory. `user_id` selects the case's isolated memory
graph.

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
  Your `2xx` is the **ingest acknowledgement**: return it only once every pair
  in the request is embedded and queryable, because the validator dispatches
  the questions that depend on that wave the moment it arrives, and a question
  answered from a store still mid-ingest grades 0 exactly as a fabricated
  answer would. From `bench_version` 13 a bounded share of corrections arrives
  this way (the initial seed no longer carries every record), so a harness
  that acknowledges before it has finished indexing loses real credit.

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

with the `bench_version >= 7` strict rules on top: a forbidden argument on an
expected tool zeroes the case, hop order multiplies the whole score on ordered
multi-hop cases, the extra-call / over-budget penalty is doubled, result-usage
is multiplicative (`trajectory × 1.0` when the answer carries the served
needle, `× 0.1` when it ignores it, `× 0.0` when it carries the served decoy),
and a non-empty self-reported `tool_calls` that disagrees with the observed
trajectory halves the case. An observable case that never executed through
`tool_endpoint` scores 0 in scored scope (0.05 in practice).

The on-chain composite (`0.5 × tool_mean + 0.5 × memory_mean`) is then
multiplied by bounded integrity factors. Each is `1.0` (no effect) when its
trigger is absent, so accuracy stays dominant and every factor is a pure
function of already-published per-case results (re-derivable from the run
details). The curve below is the v7+ contract that every live version (v9–v12,
and v13) uses; the pre-v7 `[0.85, 1.0]` / first-extra-free curve is history:

- Tool efficiency (observed-execution runs): **no free overshoot**; the
  over-call penalty saturates at +3 extra calls at a maximum of 40%, and only
  cases scoring ≥ 0.6 contribute. Memory-routing cases, `allow_extra_tools`
  cases and unobserved cases are exempt. `MaxToolCalls=15` describes the task
  shape and is not a hard ceiling.
- Memory over-call: maximum 25%. Metamorphic consistency (invariance families
  answered inconsistently): maximum 40%. The product of these bounded factors
  is floored at 0.40.
- Canary integrity (every run): a per-run seed-derived nonce is planted in the
  conversation and one memory case asks for it. An honest recall miss carries
  **no composite penalty** (it is already the case's own miss); surfacing the
  planted decoy nonce (a cross-user leak) multiplies the composite by 0.25 and
  compounds across leaks. A harness with a lexical nonce index passes.
- Conversational sanity: `0.25 + 0.75 × geomean(slices)` over the chitchat,
  declarative-acknowledgement and declarative-behaviour slices; a slice fully
  failed pulls the factor to its 0.25 floor. From `bench_version` 13 a canned
  acknowledgement without the stated value scores 0.25 on a declarative case,
  below the 0.5 correctness line.
- Reproduce-under-transform audit: enforced, maximum 40%, keyed on the
  directional base-only-minus-transform-only brittleness signal
  (`transform_robustness` in the run details).
- v9+ score gates (`scoregates`): model-use and authoritative-tool coverage are
  binary (a zero-inference run zeroes); v12 adds model dependence, an
  inference-latency check and a capped answer-stuffing penalty; v13 adds the
  relay-evidenced gates below, all shadow/observe in v13.0.

The pre-v7 description that used to sit here (`[0.85, 1.0]` factors, a free
first extra call, a `×0.85` canary miss) described the v3–v6 contracts and is
retired.

Token usage never moves the composite (quality-only since v7); the relative
efficiency bonus lives in the Platform layer as a capped tie-break. Bench v13
adds a per-case inference cost factor over successful completions, sampled
choices and output tokens that is **reported, never applied** in v13.0 (see
*Bench v13 additions* below). The authoritative statement of every factor is
[`services/dittobench-api/PROTOCOL.md`](../../services/dittobench-api/PROTOCOL.md)
(*bench_version 7: strict scoring*).

### On-chain timeouts

| Call | Ceiling |
| --- | --- |
| `GET /health` (container start to healthy) | 3 min for the container to answer; each probe is bounded at 10 s |
| `POST /run` (per case, a miss scores 0) | 5 min (`bench_version >= 7`; `DITTOBENCH_V7_CASE_TIMEOUT`) |
| `POST /seed` (per wave; your 2xx is the ingest acknowledgement) | 15 min (`DITTOBENCH_V7_SEED_TIMEOUT`) |

A scored run overlaps `/run` calls up to `case_concurrency` (default 4, max 64)
and admits `max(4, case_concurrency)` in-flight inference and tool calls per
harness; above that the broker answers `429` with `Retry-After: 1`, so retry on
429 inside a case. A gate held across a network `.await` turns those overlapping
cases back into one queue and misses the run deadline with no per-case error.

## Bench v13 additions (harness-visible)

Bench v13 is the typed-semantic contract
([`bench-versions.md`](../../research/dittobench-datagen/docs/bench-versions.md),
*Bench v13*). It reaches your harness as **wire version 9** with the same
request and response shapes; everything new is an additive optional field or a
rule about what the validator records and grades. The full statement of every
rule, with its case note and vector test, is
[`services/dittobench-api/PROTOCOL.md`](../../services/dittobench-api/PROTOCOL.md)
(*bench_version 13* sections). This is the harness-side summary — the one
Bench v13 section of this document; the architecture guide that shows how
honest designs pass is the README's *Bench v13: how to stay inside the gates*.
The stock kit is the reference honest architecture: `src/v13.rs` holds each
piece (`answer_slot_from_prose`, `preload_catalog` / `semantic_top_k`,
`HARNESS_POLICY_PROMPT`, the completion log) and `src/baseline.rs` wires them.
The `answer` slot is **off unless `DITTOBENCH_ANSWER_SLOT` is set**
(`v13::ANSWER_SLOT_ENV`; the `--gates` rehearsal sets it): the wire stays at
bench 9, so the slot cannot be gated on the contract version, and under the
live v9+ grading policy a populated slot is authoritative with no prose
fallback, so a default-on slot would change live v12 scores. With the switch
on, the policy prompt asks the model for a final `Answer: <value>` line and the
slot is that line, copied verbatim.

### What changes in the requests you receive

- **The tool catalog is a per-seed surface.** Production tool names never
  change, but every description is a per-seed paraphrase (read it, do not match
  its bytes); `set_theme.theme` and `set_reasoning_effort.effort` carry a
  JSON-schema `enum`; `set_accent_color.color` and `set_chat_font.font` say
  their options are workspace-configured and listed only by
  `discover_capabilities` (the canonical spelling exists only in that served
  result, so list-then-act); three to five **coined decoy tools** with coined
  names are spliced in, answered `{"error": "<name> is not configured …"}`
  unless the case expects them — and on ≥ 10% of tool cases the decoy *is* the
  right tool and serves the needle, so a blacklist of unknown names loses real
  weight. The baked model-slug and font option-pool families
  (`set_model`/`set_main_model`, `set_font`) are retired; `set_chat_font`
  **stays on the wire** as a discovery-grounded setter whose options are listed
  only by `discover_capabilities`, so keep it in the catalog you offer. Pass
  every wire tool to the model unchanged (`WireTool::from_wire` already does).
- **Served content is coined per seed.** `list_workflows`, `list_schedules`,
  `list_agent_jobs`, `search_tools`, `run_code` and `discover_capabilities`
  results differ per seed and the dependent cases grade whether your answer
  used them; fixed strings baked from an earlier run score the 0.1 miss gate.
- **`/seed` waves arrive during the memory phase and your 2xx is the ingest
  acknowledgement.** About a tenth of the world's corrections leave the initial
  seed and arrive in waves 1–2; the questions that need them are dispatched the
  moment your `/seed` returns 2xx. Return 2xx only once every pair in the
  request is embedded and queryable.
- **Point-in-time anchors live inside `user_input`.** "As of 12 March, who
  owned the handoff?" names a date that exists in no seeded record; the answer
  is the state in force on that date (the superseded value, with the current
  value planted as its distractor). These arrive as `as_of_twin` pairs; an
  index that only knows the current state scores exactly one half. Answer the
  question in front of you, at the time it names.
- **Some cases expect a clarifying question, some expect a grounded decline.**
  On an ask-half restraint case the value is absent from the records: make no
  non-memory tool call and ask a question that names the missing slot ("which
  typeface?") and cites something you searched; "what would you like?" scores
  0. On the matched act-half the stored preference holds the value: search it
  and act (confirm-and-act passes). On an unanswerable memory case, decline
  (`abstain: true` or a decline phrase) **and** cite a token from the records you
  searched; you may name the tempting value as insufficient evidence, but
  asserting it as the answer scores 0, and a generic refusal scores 0.

### What the validator now records about your inference calls

The ticket-bound broker records, per successful completion, **metadata only**:
the names and schema digests of the tools you offered, the normalised
`tool_choice`, a digest of your system/developer/prefill spans, the tool names
the model emitted, 64-bit hashes of the canonical value tokens in every
completion span (message content, tool-call arguments, structured-output
fields), in every harness-authored prompt span, and in every tool result the
validator served you, plus choices and output tokens. No prompt or completion
text is kept. Send
`X-Ditto-Case-Id: <case_id>` on the inference calls you make while serving a
`/run` and every case stays attributable under concurrency; without it a
concurrent run books some completions run-wide and the affected cases are
marked incomplete, which always fails **open**.

### The gates, their notes, and their posture

| Gate | You lose the case when… | Note | v13.0 posture |
| --- | --- | --- | --- |
| Catalog-present | the model was never offered a choosable non-memory catalog on a no-tool case (restraint you decided, not the model — `tool_choice: "none"` and a memory-only catalog count as no offer), or the expected tool was never offered; waived when your retained set holds the top-3 of the published TF-IDF embedding (`v13::semantic_top_k` in this kit, `scorer.CatalogSemanticTopK` in the validator) or is merely non-empty on a chit-chat/declarative/decline case (the negation family excepted) | `restraint_without_offer`, `expected_tool_not_offered` | shadow (`DITTOBENCH_V13_CATALOG_GATE_POSTURE`) |
| Swallowed model call | the model emitted a non-memory call on a no-tool case and you did not execute it through `tool_endpoint` | `swallowed_model_call` | shadow |
| Claim-span provenance | the graded value tokens in the credited span (`answer`, else `final_text`) are not contained in any completion of the case after the public normaliser `scoregates.NormalizeSpan` (`/100` rescale, direction map, draft replacement); a credited value with no completion at all | `served_text_not_model_emitted`, `no_model_completion` | shadow (`DITTOBENCH_V13_CLAIM_PROVENANCE_POSTURE`, shared with the causal gate) |
| Slot tie-break | the `answer` slot alone would pass but no typed-equivalent value is asserted in `final_text` | `slot_not_in_prose` | grading rule |
| Causal model dependence | the graded value appears in a prompt span you authored (system prompt, template, prefill, tool-role message) before any completion produced it, and in no `/seed` record, served tool result, the case's `user_input` or the validator's system prompt | `answer_in_prompt` | shadow (same switch) |
| Twin / pair post-pass | you gave the same decision or the same answer to both halves of a `decision_twin` / `as_of_twin`, or the counterfactual member got the base member's answer | `twin_concordant`, `counterfactual_insensitive` | observe |
| Inference cost | output tokens of successful completions exceed the published per-class budget (3 completion-equivalents of 512 tokens on memory / single-tool cases, 5 on chains); floor 0.6 at twice the budget | `per_case[].inference_cost` | shadow, reported only |

Shadow and observe mean recorded, not applied: your composite does not move,
the notes appear on your per-score detail on the Platform (#1852), and the
local replay below (#1851) reproduces them against a pass-off artifact. A gate
flips to enforce only as a fleet-wide operator decision after the calibration
cohort (#1521) shows 0 false zeros on every honest architecture.

### What the grader now accepts

- **Prose is graded, the slot is a tie-break.** "I first thought Oslo, but it
  is Lisbon", "Lisbon, not Oslo", "was 4,200, now 3,800" each assert one value;
  rejected candidates are no longer distractor hits. More than two distinct
  asserted values for one scalar claim, or two inconsistent ones, score 0.
- **The requested unit.** A bare `411067` on a minor-unit question passes, as
  do `411,067 cents`, `$4,110.67`, `USD 4,110.67`, `4.110,67`; `$411,067`
  fails. Never rescale the model's number.
- **The question's own vocabulary.** Direction is three-valued (increase /
  decrease / unchanged) with the question's words (raise/lower, up/down,
  gaining/losing) accepted; dates pass in any unambiguous rendering at the
  requested granularity.
- **The question's language or English.** A case rendered in es/pt/fr/it/de/nl
  (fraction 0 in v13.0) accepts the answer in that language or English,
  including translated status words and decline phrases; values stay canonical.
- A declarative acknowledgement without the stated value scores 0.25.

### What is prohibited (in addition to the v12 list)

Withholding or emptying `tools[]` on a request-keyed family so the model
cannot act; swallowing a model-emitted call before execution; computing the
graded value on the host and laundering it through a "reply exactly" completion;
replacing the model's served text by wording; rescaling its number or mapping
its direction word onto grader vocabulary. Each of these is now relay-visible
and noted per case, and each is a screener I4/I5/I7 finding under policy v14.

### Local replay (shadow)

`uv run ditto practice --bench-version 13 --gates` (or
`python3 scripts/local-rehearsal.py --bench-version 13 --gates`) records what
this kit offered and emitted per case (`DITTOBENCH_COMPLETION_LOG`), turns the
`answer` slot on (`DITTOBENCH_ANSWER_SLOT=1`), and replays every rule above against the public (salt 0) pass-off artifact, printing
per-case notes and a shadow gate-induced loss. `--keep-artifacts DIR` keeps the
dataset, transcript, completion log and result for an offline re-run with
`scripts/rehearsal_gates.py`. Every gate is shadow in v13.0, so the replay never
moves a local score. The validator's relay is the authoritative evidence
source; the local log is the same rule over the harness's own view.
