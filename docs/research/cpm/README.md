# CPM training data from DittoBench (P0 step 1)

Research tooling for the context-packet model (CPM) P0 in
[`docs/research/sn118-model-competition.md`](../sn118-model-competition.md)
(§I.2 task, §I.4 source 1, §R3.7 runbook). Nothing here touches a score path. It lives under `docs/` (release-ignored), like the
[no-inference prototypes](../../no-inference/), so it never cuts a component release.

`export_cpm.py` turns public `cmd/generate` artifacts into CPM training data.
Every label comes from the public artifact: no grader-only (`json:"-"`) field
and no LLM.

```bash
cd research/dittobench-datagen
for s in 42 7 1234; do
  go run ./cmd/generate -bench-version 18 -seed "$s" -run-size full -out "/tmp/cpm/v18-$s.json"
done
python3 -I ../../docs/research/cpm/export_cpm.py /tmp/cpm/v18-{42,7,1234}.json --out-dir /tmp/cpm/out
```

## Output

| File | One record per | Contents |
| --- | --- | --- |
| `pairs.jsonl` | seeded memory pair | `seed_key`, `pair_id`, `user_id`, `wave`, `session_id`, `timestamp`, `prompt`, `response`, `source` (`memory_wave` or `tool_prereq:<case>`) |
| `cases.jsonl` | memory or tool case | the query, candidate ids and labels (below) |
| `catalog.jsonl` | tool in a seed's pinned catalog | `name`, `description`, `parameters` |
| `manifest.json` | export | input paths and SHA-256, counts, label coverage |

**Memory cases** (`kind: "memory"`):

- `candidate_pair_ids`: the pairs the case's user has when the question runs
  (`user_id` defaults to `miner`; waves ≤ `run_after_wave`).
- `relevant_pair_ids`: `v10_evidence_pair_ids`, the gold relevance label.
- `decision`: `answer_kind`, plus `abstain` for absence and chitchat cases.
- `spans`: per candidate pair, the character spans in `prompt` or `response`
  where an answer string (`answer_value`), a distractor (`distractor`) or the
  forbidden answer (`forbidden`) appears. Matching is case-insensitive and
  word-bounded.

**Tool cases** (`kind: "tool"`):

- `shortlist_target`: the expected tool names, the target for catalog
  pruning.
- `prerequisite_pair_ids` and the fixture `needle`.
- `decision.abstain`: true when no tool is expected (restraint cases).

## Measured on v18 seed 42 (full profile)

| | Value |
| --- | --- |
| Cases | 125 memory (15 abstain) + 50 tool (3 restraint) |
| Seeded pairs (memory waves + tool prerequisites) | 181 + 359 |
| Catalog | 1,454 tools. Every one of the 73 expected tool names is in it |
| Mean candidates per memory case | ≈507. Primary-user cases include the 359 tool prerequisite pairs, which hold 56% of the relevant ids |
| Relevant ids present among the candidates | 436 / 436 |
| Answer span found inside the evidence pairs | 82 / 107 answerable cases (77%) |

**Exact answer spans still miss about a quarter of cases**, and that is the
bench working as designed:

- **Composed answers.** Many answers are composed across hops. For example, an
  owner's email is built from a name in one pair and a company domain
  elsewhere.
- **Derived values.** Numbers and money values are computed (sums, counts),
  so they do not appear verbatim.
- **Typos and noise.** The generator injects typos and noise, so an answer
  can be misspelled in the haystack.

**`relevant_pair_ids` is the reliable label.** Train relevance on it. Treat
spans as auxiliary supervision. The S3 end-to-end reward (§I.5) covers what
spans cannot.

**Both jobs are real pruning problems.** Each memory case has about 507
candidates to pack under a budget, and each tool case has to be pruned from a
catalog of 1,454 tools (100k–200k tokens).

## Splits

Split by seed, never by case. Cases from one seed share a haystack, so a
case-level split leaks evidence. The public generator is unlimited, so hold out
whole seeds for validation and test.

## Related

- [competition-v1.md](competition-v1.md): the v1 build contract.
- [bittensor-compute.md](bittensor-compute.md): proposal to run the scorer on
  Bittensor GPU subnets (Lium first) beside the AWS lane.
- [open-questions.md](open-questions.md): open questions for ML review
  (train-on-test within a season, noise, copying, metric, determinism).
