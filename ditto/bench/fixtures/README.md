# DittoBench fixtures (public set)

This directory holds the **public** fixture corpus the DittoBench validator
runner consumes when scoring miner harness submissions. Everything here ships
with the repo and is freely available; private and canary cases live in a
validator-only encrypted manifest and are **never** committed here.

The coverage matrix lives at
[`../docs/coverage_matrix.md`](../docs/coverage_matrix.md) and maps each
fixture file to the Bittensor mechanism (`ditto_core` / `ditto_retrieval`) and
the user-facing domain it scores.

## Layout

```
fixtures/
  toolcall/                         DittoCore (Mechanism 0) fixtures
    search_memories.jsonl
    fetch_memories.jsonl
    search_subjects.jsonl
    search_memories_in_subjects.jsonl
    search_web.jsonl
    read_links.jsonl
    create_image.jsonl
    no_tool.jsonl                   abstention + safety/privacy refusal
    multi_tool.jsonl                multi-step sequencing
  retrieval/                        DittoRetrieval (Mechanism 1) fixtures
    needles.jsonl                   single/multi-needle, stale, STM legacy
    contradictions.jsonl            newer-fact-wins / contradiction-update
    mcp_parity.jsonl                MCP-vs-chat parity
    stm.jsonl                       STM-only and STM-with-distractors
    longmemeval_evidence.jsonl      LongMemEval evidence-ID derived cases
  response_quality/
    golden.jsonl                    internal regression gate; not on-chain
  longmemeval/
    longmemeval_oracle.json         500-question oracle dataset
    seed_manifest.json              seeded pair-id mapping
```

## Adding a case

1. Append a single-line JSON object to the appropriate file.
2. Keep the `id` unique across the whole `toolcall/` (or `retrieval/`)
   directory; use short kebab-case. The Python loader rejects duplicates
   with `DuplicateCaseIDError`. Validators merge every `*.jsonl` in a
   directory before grading, so colliding IDs would silently overwrite
   scores during aggregation.
3. `category` groups cases in the aggregated report. For `retrieval/` the
   category must be in `ditto.bench.loader.taxonomy.RetrievalCategory`. For
   `toolcall/` the optional `domain` field must be in
   `ditto.bench.loader.taxonomy.CoreDomain`. New categories or domains require
   a code change in `taxonomy.py`.
4. For `toolcall/` cases prefer the richer `arg_matchers` language over the
   legacy `required_args` map:

   ```jsonc
   {
     "name": "read_links",
     "arg_matchers": [
       {"kind": "url_list", "key": "urls",
        "any_of": ["https://example.com/a"]}
     ]
   }
   ```

   See `ditto.bench.loader.cases.ArgMatcherKind` for the full kind list
   (`exact`, `contains`, `regex`, `url_list`, `memory_id_list`,
   `string_array_contains`, `forbidden`, `present`).
5. For retrieval cases the `user_fixture_id` must correspond to a seeded
   fixture user (currently `dittobench_fixture_alice`). Seeded fixture users
   live in the validator's private manifest; the public dataset only
   references them by ID.
6. For response-quality cases, `reference_response` is the canonical
   *current-harness* answer. Regenerate it deliberately (do not auto-generate)
   so drift detection remains meaningful. `response_quality` is an internal
   regression gate and is **not** an on-chain mechanism.

## Visibility

The `visibility` field on `ToolCallCase` and `RetrievalCase` distinguishes:

- `"public"` (default): ships with the repo and is scored by every miner.
- `"private"`: validator-only; loaded from the encrypted manifest at run time.
  Reshuffled on a cadence so miners cannot cache responses across tempos.
- `"canary"`: rotating hidden cases used to detect benchmark memorisation; a
  miner that scores ~1.0 on `public` cases and substantially worse on
  `canary` cases is flagged.

See [`../docs/anti_gaming.md`](../docs/anti_gaming.md) for the full
hidden-split / canary / memorisation-discount / distractor protocol.

## Cost considerations

The cheapest mechanism to score is `ditto_retrieval` (embeddings + vector
search only). `ditto_core` is next (one LLM turn per case, with the harness
choosing whether to execute tools). `response_quality` is the most expensive
because it also invokes a judge (~4 extra judge calls per case with full
rubric) and is therefore retained only as a non-on-chain regression gate.

A full sweep across the per-tool fixtures (~50 cases across 5 models + judge)
should stay under a few dollars. Validate with `--sample 3` first; see
[`../docs/protocol.md`](../docs/protocol.md) for the contributor workflow.
