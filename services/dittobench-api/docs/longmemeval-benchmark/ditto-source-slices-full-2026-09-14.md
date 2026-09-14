# Full-500 source-slices experiment

## Preregistered protocol

User authorized proceeding after the [60-case pilot](ditto-source-slices-pilot-2026-09-14.md).
Run fresh control and treatment on **all 500 isolated LongMemEval-S questions**,
not a resume of the pilot or a comparison against a differently compiled old run.

- Same clean source and binary, `openai/gpt-5.6-luna` medium reader,
  `google/gemini-3.1-flash-lite` built-in judge, question-date prompt clock.
- Graph ON (`-require-graph -subject-graph`), same prepared private fixture and
  retrieval weights. `-concurrency 8` each, launches close in time.
- Control `-seed-context summary`; treatment `-seed-context source-slices-v1`.
  Slice algorithm unchanged from backend PR2736, no production tool changes.
- Full500 launcher rejects sampling, case filters, wrong models/config/input
  hashes, dirty/unproven binaries and reused outputs. Source derives from
  pilot `1cd1cf15245ea39123ea421e9bb2b7bf51d204c1`; only QA artifact IDs and
  launcher change. UUID suffix prevents same-second remote upload collisions.
- One attempt/arm. No incorrect-answer retries or regrading. If transport fails,
  preserve original report/journal and permit at most one failure-only resume
  after documenting exact failed IDs, same source and checkpoint compatibility.
- Prepared snapshot/hydration and zero graph failure gates must pass; compare
  actual ordered seed IDs and baseline JSON fields for all500, disclose drift.
- Report all500 plus previously-piloted60 and remaining440 separately. Remaining
  440 is not clean held-out: the retrieval ranker has 474/500 question-ID/text
  training overlap; the full corpus has also been evaluated previously.
- Report category counts, paired wins/losses, Wilson intervals, source-context
  bytes, cumulative prompt tokens, tool calls and final provider receipt costs.
  One stochastic run/judge is not a general efficacy guarantee.
- Cost: include all captured reader/judge calls from every attempt, separate
  OpenRouter charges and provider-reported BYOK estimates (not invoices), total
  and mean/median/p95 per question. Original seed/dream and query-embedding
  cost remains unknown unless independently captured; do not treat it as zero.
  New seed/dream calls are zero because preparation is reused.

No reseeding, dreaming, fixture writes/cleanup, merge, deployment or activation.
Both code and protocol published before inference. Result artifacts retained
under the new task workspace, not written into earlier pilot workspaces.

## Reproduction

Backend: `scripts/benchmarks/lme_slices_full.py --spec <new-arm-spec.json>`
verifies without inference; append `--execute` only for an approved paid run.
Specs include exact500 ordered IDs, source/binary/input hashes and distinct
task-local output paths. Existing fixed campaign guards remain unchanged.

Subnet: `analyze_slices_full.py` checks both native reports, full launch spec
and prior pilot evidence. It exports whitelisted public-fixture reports and
replays them to verify unchanged paired metrics. Costs use the existing
`audit_resumed_openrouter_costs.py` against closed journals and GET-generation
receipts, excluding private receipt IDs from the committed summary.

Results and exact execution identities will be appended after completion.

## Failure-only resume decision (before retry)

Control first invocation saved500 rows but only496 judged: three reader TLS
`bad record MAC` failures (`gpt4_e414231e`, `2ebe6c92`, `gpt4_68e94287`), and
one judge TLS failure (`gpt4_7ca326fa`). Native graph/hydration and unchanged
prepared snapshot gates passed. Progress counters/logs did not expose these
case failures until the final report; they are not500 valid scores.

Apply the preregistered single failure-only native resume. The wrapper pins the
exact original report/checkpoint/journal hashes, source and binary; copies all
496 judged rows unchanged, including wrong answers; and claims retry4 once.
It refuses any different failure, retry complement, altered result or dirty
source. The native harness re-prepares all500 contexts but queries only four.
For the one judge-failed case, native resume regenerates its answer as well as
retrying judging: it is an unjudged case, not a known-wrong answer retry. Its
original answer is retained, the final outcome is not cherry-picked, and all
original/new costs are counted. This is a protocol limitation relative to a
judge-only retry, explicitly preserved in the record. No third attempt allowed.

## Completed accuracy and integrity

**Summary: 348/500 (69.6%). Source slices: 418/500 (83.6%). Net +70 correct,
+14.0 percentage points, with 82 paired wins and 12 losses.** Both arms are
isolated LongMemEval-S, Luna medium, built-in Flash Lite judge, question-date
clock and prepared graph ON. There is no regrading or full-run cherry-picking.

| Category | Summary | Source slices | Net correct |
| --- | ---: | ---: | ---: |
| Knowledge update | 62/78 | 63/78 | +1 |
| Multi-session | 83/133 | 103/133 | +20 |
| Single-session assistant | 47/56 | 56/56 | +9 |
| Single-session preference | 19/30 | 20/30 | +1 |
| Single-session user | 61/70 | 69/70 | +8 |
| Temporal reasoning | 76/133 | 107/133 | +31 |

Wilson 95% intervals: summary **65.43–73.47%**, slices **80.10–86.59%**.
These describe case-sampling uncertainty, not repeated-run/provider/judge
variance or clean held-out generalization. The ranker-training overlap remains.

The gain extends beyond the pilot: prior 60 score **42→47/60** in these fresh
runs; other 440 score **306→371/440** (net +65). These are not additional held-out
data: all 500 have been evaluated before, and the ranker has 474/500 overlap.

All **500 ordered seed-ID lists match**, and **all baseline serialized memory
fields are preserved exactly** in treatment context. Source/prompt/tools/weights
and selected cases match. Every invocation checked 500 fixture users and had
zero graph-discovery failures. Prepared snapshot stayed
`8af521b88d155c4c6e81befe89495b7e74328f65a734b91fdd45348a1a8a8023`
before/after all invocations. This supports a presentation improvement; it is
not an ON/OFF graph experiment or proof that all prior graph regression was
caused by summaries.

### Root-cause follow-through

All three previously probed summary-loss cases now pass with slices, with
**zero follow-up tools**, while the matched summary control fails:

- `3c1045c8`: restores the department average 29.5 and correctly answers the
  user is 2.5 years older.
- `gpt4_372c3eed`: includes the omitted associate's degree and counts 10 years
  of formal education.
- `gpt4_cd90e484`: preserves relative event timing and answers two weeks from
  binocular purchase to goldfinch return, not record-timestamp subtraction.

The memory was selected in both arms; source presentation exposes the detail
needed to answer. This is strong evidence for testing this policy in the
product retrieval/tool path. It remains a benchmark-only query-window
prototype inspired by PR859, **not** stored subject-span annotations or a
production feature. Twelve regressions and 82 remaining errors still exist.

| Reader behavior | Summary | Source slices |
| --- | ---: | ---: |
| Mean initial seed-context bytes | 3,888.95 | 15,157.67 |
| Mean cumulative prompt tokens | 27,965.12 | 21,808.99 |
| Questions without follow-up tools | 248 | 338 |
| Mean final recorded session recall | 99.00% | 98.88% |

Larger initial context coincided with fewer follow-up calls and lower cumulative
prompt tokens. Final session recall includes later retrieval, so its slight
difference does not contradict identical initial seed IDs. Session coverage
is not evidence that the answer-bearing fact was shown.

### Recovery and exact run identity

The resume audit verifies **496 original judged rows unchanged**, including
all 150 original wrong answers. Two of the four recovered cases pass and two
fail. The regenerated judge-failed case still abstains and is judged wrong;
both its original and final answers are retained in the audit.

- Source SHA: `dfcb9498ca508d2f759ef83dd7c0c3d3512526a5`.
- Binary SHA256: `e9ddf21cc9cfb3e24b53c77a14e27aa78fd3bdf20c6b1d596ce84ceadc092ae4`.
- Protocol-before-inference subnet SHA: `70066cb7` (PR1908).
- Summary first attempt:
  `20260914-162449-dfcb9498ca508d2f759ef83dd7c0c3d3512526a5-bcc925de-ff4b-4549-b287-d2b79f1944e0`;
  16:24:49–16:34:33 UTC, 496 judged.
- Summary final failure-only resume:
  `20260914-163935-dfcb9498ca508d2f759ef83dd7c0c3d3512526a5-db3079dc-cf49-44d1-ad84-f1ff87cd1ead`;
  16:39:35–16:43:35 UTC, 500 judged including 496 preserved.
- Slices:
  `20260914-162449-dfcb9498ca508d2f759ef83dd7c0c3d3512526a5-7747f09f-467f-4146-b116-b8d54477e858`;
  16:24:49–16:32:25 UTC, 500 judged first attempt.

Distinct UUID keys solved the pilot's remote artifact collision. Original logs,
checkpoints and journals remain intact. The retry added roughly four minutes
of native preparation/inference, not another 500 model answers. Wall times
include retrieval; per-case query latencies do not cover that full preparation.

Evidence:
[paired audit](results/2026-09-14-slices-full.json),
[retry preservation](results/2026-09-14-slices-full-resume-audit.json),
[all launch commands/timings/hashes](results/2026-09-14-slices-full-launches.json),
[replayable public-fixture reports](results/2026-09-14-slices-full-public-evidence.json).
The public export excludes provider IDs, private receipt details, raw tool traces
and reasoning; its paired metrics are verified equal to the native reports.

## Final captured cost

| USD metric | Summary (original + retry union) | Source slices |
| --- | ---: | ---: |
| Reader BYOK upstream estimate | 1.63243224 | 1.67333284 |
| OpenRouter judge charges | 0.05000150 | 0.04966475 |
| Captured reader + judge estimate | **1.68243374** | **1.72299759** |
| Mean per question | **0.00336487** | **0.00344600** |
| Median per question | 0.00249817 | 0.00270370 |
| p95 per question (nearest rank) | 0.00724094 | 0.00658392 |
| Reconciled generations | 1,832 | 1,409 |
| Reader / judge generations | 1,332 / 500 | 909 / 500 |

Captured experiment total: **$3.40543133**. Treatment adds **$0.04056385
(2.41%)** relative to control. The original control invocation accounts for
$1.67304511; the new retry adds $0.00938863. Final union reused 1,813 original
receipts and fetched 19 new ones, without double-counting completed cases.
Including the earlier pilot's $0.37893828, captured source-slices research cost
is $3.78436961. These are not full lifecycle totals or vendor invoices.

All 3,241 **captured** generations have final GET-generation receipts with
known route/upstream estimates. Luna resolved to
`openai/gpt-5.6-luna-20260709`, judge to
`google/gemini-3.1-flash-lite-20260507`. Reader OpenRouter account charge is zero
because BYOK, but provider-reported upstream usage is included above and is
not free or an independently verified vendor invoice.

**Known coverage gaps:** the original judge TLS failure returned no generation
ID and has zero judge journal records, so that failed request's possible charge
is unmeasured. Original seed/dream preparation cost and query/tool embedding
cost remain unknown. New seed/dream calls in this experiment are zero because
the prepared fixture was reused. Retry re-preparation is not free merely
because its embedding spend is unavailable. The receipt auditor correctly
leaves `all_attempts_coverage_proven=false` and lifecycle totals null.

Per-question records and provenance:
[control original](results/2026-09-14-slices-full-summary-original-cost.json),
[control final union](results/2026-09-14-slices-full-summary-final-cost.json),
[slices](results/2026-09-14-slices-full-source-slices-v1-cost.json).
Private provider IDs/receipts remain local, not in Git.

## Handoff and validation

- Workspace:
  `/Users/peyton/.codex/worktrees/eb94/heyditto-stack/workspaces/lme-slices-full-500-20260914-121905-bd6f30`.
- Cloned/changed repos: `backend`, `ditto-subnet`; shared branch
  `codex/lme-slices-full-500`.
- [Backend PR2739](https://github.com/ditto-assistant/backend/pull/2739),
  source `dfcb9498ca508d2f759ef83dd7c0c3d3512526a5`; source-slices algorithm
  inherited unchanged from PR2736.
- [Research PR1908](https://github.com/ditto-assistant/ditto-subnet/pull/1908)
  contains protocol, recovery guards, audits, cost attribution and replay data.
- `go test ./pkg/dittobench/... ./cmd/dittobench -count=1`: PASS.
- Backend Python launcher/campaign guard suite: 11 tests PASS.
- Subnet LongMemEval integration suite: 104 tests PASS.
- Public full evidence replay reproduces 348/500 and 418/500, exact500 paired
  seed/baseline-field parity. Resume audit proves all496 judged rows unchanged.
- `git diff --check`: PASS. Backend frozen-head CI passed. Research CI had a
  transient Go module download HTTP/2 failure on its first head; subsequent
  pre-results head passed. Final publication CI must be checked separately.

Native artifacts are preserved in `backend/.tmp/lme-slices-full`; private
cost receipts in `ditto-subnet/.tmp/full-cost`. Private container
`ditto-postgres-lme-luna-20260912`, loopback port54339, database
`ditto_lme_fixed_20260914`, remains unchanged. Earlier workspaces/artifacts are
untouched. Both experiment PRs remain draft/unmerged: **no deployment,
production activation, fixture cleanup or global retrieval change**.
