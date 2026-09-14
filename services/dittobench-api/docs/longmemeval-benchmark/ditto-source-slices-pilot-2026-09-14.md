# Source-slices pilot: preregistered protocol

This is a bounded experimental follow-up to the
[graph-score diagnosis](ditto-graph-score-drop-diagnosis-2026-09-14.md), not a
new full-500 result. No experiment result was inspected before this protocol.

## Hypothesis and stale-PR provenance

Selected memories can contain the answer in raw source while their model-facing
summary omits it. Test whether presenting bounded verbatim source alongside the
same summaries improves answers. This tests **presentation**, not graph benefit.

Inspiration: backend [PR859](https://github.com/ditto-assistant/backend/pull/859),
`309817dcdafebbae10927661ed8df843a5704595`, subject-span annotations and excerpt
windows. That PR is data-layer-only and does not connect the chat/MCP readers.
Its builder overwrites multiple windows for one role, and its documented
unannotated-role fallback is not implemented in the inspected function. We do
not cherry-pick its old migration or claim to implement its subject annotations.

The prototype uses **query-anchored read-time slices**, only in the backend
LongMemEval QA seed serializer. Production, subject indexing, graph retrieval,
tool definitions, `AlreadyFoundPairIDs`, full fetch, and DB contents are unchanged.
PR880 compact serialization/top-3 is deliberately excluded from this ablation.

## Frozen arms

- Condition: isolated LongMemEval-S, previously seeded/dreamed private fixture,
  strict graph preflight and one-hop-v1 retrieval ON in both arms.
- Same clean backend commit and binary in both arms. Baseline code descends from
  merged fix `fa8b02f750bec4afeee15cd17e552fac72052601`.
- Reader `openai/gpt-5.6-luna`, reasoning `medium`; built-in judge
  `google/gemini-3.1-flash-lite`. Provider-resolved versions checked in receipts.
- `-sample 60`: native deterministic category round-robin; first 10 questions
  in input order per each of six categories. No accuracy/gold-based selection.
  Launch specs store all 60 IDs before inference; reports must match them.
- Control: `-seed-context summary` (stock `LongTermJSON`).
- Treatment: `-seed-context source-slices-v1` (retain summary and all seed IDs,
  timestamps, parents, plus bounded source for each user/assistant role).
- Roles <=1,200 Unicode runes retained verbatim. Longer roles: up to three
  windows around exact query-word matches, 200-rune radius, <=32-rune boundary
  expansion, prioritized by distinct query terms; overlap merged, source order
  retained. Fixed stop words; terms 3..64 runes. If no anchors, first/last 200
  runes. Explicit partial flags/ellipses; no gold-dependent processing.
  Unsummarized memories retain the full source already shown by the control;
  the experiment adds evidence and never clips existing baseline fields.
- `-prompt-clock question-date`, `-concurrency 8` per arm. No retry of an
  incorrect answer. One fresh attempt per arm; any transport failure is retained
  and reported before a separately documented bounded failure-only retry.

Backend `scripts/benchmarks/lme_slices_pilot.py` reuses the fixed campaign's
source/binary VCS checks, pinned input/config hashes, literal env parsing,
loopback private DB override and exclusive output guards. It permits only the
exact pilot extension. The existing full-500 launcher remains unchanged.
Dry run first; paid inference only with explicit `--execute`.

## Evidence and acceptance gates

Archive report, launch spec/receipt, logs, checkpoint, append-only provider usage
journal, source SHA, binary SHA, dataset/manifest hashes and selected case IDs.
Both arms must complete/judge all 60 with native hydration, no graph SQL failure,
unchanged prepared-fixture snapshots and identical model/prompt/tool/weight
identities. The condition hashes must differ to prevent cross-arm resume.

Both arms now record actual seed JSON, hash, byte count and truncation status
(512 KiB trace cap). Compare ordered seed IDs **and** baseline memory fields in
the source-slices payload. Report any retrieval drift, not just a pooled score.
Native recency still uses SQL server time, not the question-date prompt clock;
close launches plus observed parity are required, not assumed determinism.

Report paired wins/losses, category counts, aggregate accuracy, session recall,
actual context bytes, cumulative prompt tokens, tool use and receipt costs.
The balanced 60-case score is not the 500-case score or evidence of statistical
equivalence. One sample/run/judge is exploratory; do not declare a general win
from a small difference. The retrieval ranker has 474/500 question-ID/text
training overlap: this is not clean held-out generalization or a mem0 comparison.

## Cost boundaries

Reconcile captured reader and judge generation IDs against final OpenRouter
GET-generation receipts, including charged failed calls. Report reader BYOK
upstream estimate separately from OpenRouter charges; neither is silently an
invoice. Publish total, mean/median/p95 per question and missing-receipt counts.

No new seeding/dreaming is performed; this experiment reuses the prepared
fixture. Incremental seed/dream calls are zero. **Original preparation cost is
unknown**, not zero. Query-embedding cost is unmeasured unless separately
captured; disclose that the total covers captured reader/judge calls only.

No experimental merge, production activation/deployment or fixture mutation.
Results will be appended with exact run IDs and hashes after both arms finish.

## Completed results (2026-09-14, 15:22–15:23 UTC)

**Summary 45/60 (75%) → source slices 48/60 (80%).** Seven paired wins, four
losses; net +3. Both arms completed all 60, first attempt, no answer retries or
regrading. This is promising exploratory evidence, not a statistically
established improvement or a replacement for the existing 336/500 (67.2%)
full graph-on result. The full-500 source-slices run has **not** been performed.

| Metric | Summary | Source slices |
| --- | ---: | ---: |
| Correct / 60 | 45 | 48 |
| Knowledge update / 10 | 7 | 7 |
| Multi-session / 10 | 6 | 6 |
| Single-session assistant / 10 | 7 | 10 |
| Single-session preference / 10 | 7 | 6 |
| Single-session user / 10 | 10 | 10 |
| Temporal reasoning / 10 | 8 | 9 |
| Mean initial context bytes | 3,872.4 | 14,985.6 |
| Mean cumulative prompt tokens | 22,091.6 | 18,420.4 |
| Questions with no follow-up tools | 35 | 43 |
| Captured reader generations | 132 | 99 |
| Captured judge generations | 60 | 60 |
| Graph discovery calls / failures | 405 / 0 | 351 / 0 |

All **60/60 ordered seed-ID lists match**. All **60/60 baseline serialized
memory fields are preserved verbatim** in the treatment payload. Source,
prompt, tools, weights, model and case identities match. Native hydration checks
all 60 isolated users; both session-recall means are 1.0. The same prepared
snapshot hash `79d25fa454decc85f4aade958f956732dcfe68deecccf500a2d149b932802b2e`
is unchanged before/after both arms. Both logs have zero matches for
`ERR|failed|error`; native failure-reason counters also independently pass.

### What improved, and what did not

Three particularly clear assistant-memory wins needed no follow-up tools with
slices; each corresponding summary omitted the exact answer or relation:

- `7161e7e2`: the raw shift table maps Sunday/Admon to **8am–4pm**; summary only
  describes a seven-agent shift schedule. Control answered a week rotation.
- `e9327a54`: the source names **Sugar Factory at Icon Park** and giant
  milkshakes; summary only describes Orlando dessert destinations. Control
  guessed a different venue even after searching/fetching.
- `7e00a6cb`: the source ties **International Budget Hostel** to the Red Light
  District; summary lists five hostels without that relation. Control picked
  The Bulldog. This is relational-detail preservation, not just name recall.

These traces support the summary-loss diagnosis. They do not show that query
windows are optimal or that stored subject-span annotations are unnecessary.
The prototype does not yet apply slices to search-tool results or production.

Losses remain: plant counting loses a succulent; clothing counting differs on
items versus pickup/return actions; preference advice misses the intended
yogurt personalization. The temporal loss `9a707b81` also exposes judge noise:
control says **25 days**, gold allows **21 or 22**, but the judge accepts 25 as
close enough; treatment's 26 is rejected. Scores are retained as judged, not
manually repaired. A larger matched run and judge-sensitivity audit are needed
before claiming a durable net gain.

### Measured cost (captured calls, not full lifecycle)

| Cost in USD | Summary | Source slices |
| --- | ---: | ---: |
| Reader BYOK upstream estimate | 0.17592414 | 0.18977514 |
| OpenRouter judge charges | 0.00668500 | 0.00655400 |
| Captured reader + judge estimate | **0.18260914** | **0.19632914** |
| Mean per question | 0.00304349 | 0.00327215 |
| Median per question | 0.00232666 | 0.00269630 |
| p95 per question (nearest rank) | 0.00703155 | 0.00585212 |

Combined experiment estimate: **$0.37893828**. Treatment is **7.51%** more
expensive in captured calls despite fewer cumulative prompt tokens/calls;
token counts alone are not a cost measure. All 351 captured generations have
final reconciled receipts, no missing route/upstream estimates. Luna resolved
to `openai/gpt-5.6-luna-20260709`, judge to
`google/gemini-3.1-flash-lite-20260507`. Reader OpenRouter account charge is zero
because BYOK; that does **not** make Luna free. The reader amount is a
provider-reported upstream estimate, not a vendor invoice.

Original seed/dream preparation and query-embedding costs remain unknown.
New seed/dream calls for this pilot: **0**, because the fixture was reused.
Per-question costs and complete attribution boundaries are in the
[control cost audit](results/2026-09-14-slices-summary-cost.json) and
[treatment cost audit](results/2026-09-14-slices-source-slices-v1-cost.json).

### Reproducibility and artifact collision

- Backend experiment PR: [#2736](https://github.com/ditto-assistant/backend/pull/2736),
  clean executed SHA `1cd1cf15245ea39123ea421e9bb2b7bf51d204c1`.
- Binary SHA256: `60b62d7bbbad289c2b2e56d7701f8568267be0818aea270276b53cda138db459`.
- Protocol committed before inference at subnet
  `bde3a8a1ef95231a1721701da3dfee5fdf59164b`,
  [PR1905](https://github.com/ditto-assistant/ditto-subnet/pull/1905).
- Launches: control `15:22:06.989579Z`, treatment `15:22:08.286573Z`.
- Both native run IDs are
  `20260914-152212-1cd1cf15245ea39123ea421e9bb2b7bf51d204c1` because bootstrap
  completed in the same second. **Run ID alone is ambiguous.** The inherited
  B2 upload path collided; do not use that common remote URL as both artifacts.
- Separate arm-local native files are intact and hash-pinned:
  control `894db237c6bae4f23971548ee55cb396e68768744cfd7d7d7eecdce3d886644e`;
  treatment `a7ee18bea5b041152222c92187a3d1e83a06057b7c18bb163af44e3e49c49c2d`.
- [Paired audit](results/2026-09-14-slices-pilot.json) includes exact original
  paths/hashes. [Replayable public evidence](results/2026-09-14-slices-public-evidence.json)
  preserves all 60 questions, answers, verdicts, actual seed payloads and
  necessary audit metadata for both arms, plus selected IDs and binary/source
  identity. It is a whitelist-derived artifact, not the original reports;
  excludes provider generation IDs, private receipts, raw tool traces and
  reasoning. Export reruns the paired audit and verifies identical results.

Workspace: `workspaces/lme-memory-slices-experiment-20260914-110740-99ab9c`,
clones `backend` and `ditto-subnet`, both branch
`codex/lme-memory-slices-experiment`. Native inputs/logs/specs/checkpoints are
retained in `backend/.tmp/lme-slices`; private receipts in
`ditto-subnet/.tmp/slices-cost`. No cleanup or fixture writes performed.

Reproduce the paid invocation with the committed launcher and a new reviewed
spec/output path per arm (the stored specs are exclusive-use and refuse reuse):

```sh
go build -o .tmp/lme-slices/dittobench ./cmd/dittobench
python3 scripts/benchmarks/lme_slices_pilot.py --spec <arm-spec.json>
python3 scripts/benchmarks/lme_slices_pilot.py --spec <arm-spec.json> --execute
```

For offline replay, load `2026-09-14-slices-public-evidence.json` and call
`analyze_slices_pilot.analyze(bundle['control'], bundle['treatment'], bundle['case_ids'])`.
Tests: backend `go test ./pkg/dittobench/... ./cmd/dittobench -count=1`;
Python launch tests; subnet `python3 -m unittest discover -s
services/dittobench-api/integrations/longmemeval -p 'test_*.py'`.
Both experiment PRs remain drafts, unmerged and inactive in production.
