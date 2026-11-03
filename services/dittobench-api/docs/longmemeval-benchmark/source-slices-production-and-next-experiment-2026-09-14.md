# Source slices: production delivery and next experiment

## What shipped

Backend [#2741](https://github.com/ditto-assistant/backend/pull/2741) merged as
`bd3a5870f0e32cd2fe6bb30fd6d4864ce1d4f4b3`.
[Production deployment 34874449433](https://github.com/ditto-assistant/backend/actions/runs/34874449433)
completed successfully. The public build endpoint served that SHA, then
`ac94e768f887568b48ac1ee5f7e641eefc9e20d6`, a verified descendant containing
the feature. This is source/deployment proof, not a production accuracy study.

The modern chat context now attaches query-relevant verbatim windows to memories
that otherwise had only summaries. It preserves the existing top-two detailed
root policy, children packing, relevance selection, summaries and raw fallback.
Added JSON fields are capped at 32 KiB per prompt; extraction skips source roles
over 256 KiB. These are byte bounds, not token bounds. Unsummarized source is not
duplicated. The shared extractor uses deterministic text matching, not an LLM.

**No changes to dreaming, ingestion, embeddings, database schema, stored memories
or graph activation.** MCP/search-tool payloads and the legacy userPrompt-only
API path are unchanged. This is read-time prompt construction.
`PROMPT_MEMORY_SOURCE_SLICES=off` restores legacy packing; unset/on enables it.
The flag was absent from both production runtime environment files when checked.
No production user data was edited. The first bounded production log sample
contained no excerpt events. A subsequent 20-minute, 20,000-line sample of the
active green backend observed one request with excerpts and 5,040 added bytes.
Only aggregate counts were retained: this proves the live path executes, not
real-user answer quality or the benchmark gain.

Authenticated preview QA at final head
`f2e7db7170b7409a80bd4056c1d13c25da675ddc` used only the synthetic be-2741
hero account. With deliberately vague summaries, a modern structured-input
request correctly answered Kyoto and pediatric nurse from source. Its matching
request logged six source excerpts and 1,526 added bytes using Luna.
The persisted answer was independently fetched with authentication. Legacy
userPrompt probes did not exercise the new path and are not counted as passes.
Reproducible helper, synthetic SQL, and evidence:
[backend evidence](https://github.com/ditto-assistant/backend/blob/b44cf52cedb049f45d9be9d435b3a3998bcccb6c/docs/reports/2026-09-14-source-slices-preview-evidence.md).
The normal preview destroy workflow
[34875789869](https://github.com/ditto-assistant/ditto-app/actions/runs/34875789869)
completed successfully; the lane state file was confirmed absent. Local benchmark
fixtures and volumes were preserved.

## Measured efficiency, not an invoice

The matched isolated LongMemEval-S 500-case experiment used graph on in both
arms, a reused seeded/dreamed snapshot, Luna medium reader, and
`google/gemini-3.1-flash-lite` judge. The benchmark reader need not use Gemini.
The reader binary/source was `dfcb9498ca508d2f759ef83dd7c0c3d3512526a5`.
The exact run IDs, receipts and protocol are in the
[full report](https://github.com/ditto-assistant/ditto-subnet/blob/2fcd1e140b637c17c713a949d2b93828ec4b4adf/services/dittobench-api/docs/longmemeval-benchmark/ditto-source-slices-full-2026-09-14.md).

| Metric | Summary | Source slices |
| --- | ---: | ---: |
| Correct | 348/500 (69.6%) | 418/500 (83.6%) |
| Mean cumulative reader prompt tokens, completed answers | 27,965.120 | 21,808.988 |
| Mean reported output tokens, completed answers | 289.166 | 206.900 |
| Captured reader + judge estimated cost, including retry artifacts | $1.68243374 | $1.72299759 |
| Captured cost per question | $0.00336487 | $0.00344600 |
| Captured cost per correct answer | $0.00483458 | $0.00412200 |

This is +14 percentage points, **22.0% fewer prompt tokens**, and **2.41% more
captured cost**, not a cost saving. Cost per correct answer is 14.7% lower.
Larger initial context can reduce repeated retrieval and later cumulative tokens;
token totals and dollar estimates need not move together. Token metrics refer
to final completed answers; cost also includes captured failed attempts.
Historical seeding/dreaming and embedding costs remain unknown, as does any
charge for the judge TLS failure with no returned generation ID. No new seed or
dream calls were made in this comparison. Do not call these all-inclusive totals.
Production packing differs from this experiment; do not promise +14pp in prod.
The retrieval ranker had training overlap on 474/500 questions: this is not a
held-out generalization result or an apples-to-apples competitor comparison.

## Less wasteful retries

Backend [#2743](https://github.com/ditto-assistant/backend/pull/2743) merged as
`1617a1c14d80eb71b5890de0b056b67de3047a9e` after all 12 checks passed/skipped
at `b44cf52cedb049f45d9be9d435b3a3998bcccb6c`.
[Production deployment 34875709596](https://github.com/ditto-assistant/backend/actions/runs/34875709596)
also completed successfully. A subsequent public build check returned the exact
merge SHA `1617a1c14d80eb71b5890de0b056b67de3047a9e`, containing both changes.

Strict prepared runs with `-require-graph -checkpoint PATH` durably save reader
answers and original receipts before judging. Resume can judge that same answer
without another reader call, embedding or seed retrieval. Completed wrong answers
are not rerolled. Recognized transient judge failures receive at most one extra
attempt with a cancellation-aware delay. Malformed/auth/unknown errors do not.
Partial checkpoint rows, incompatible conditions and persistence errors fail
closed. One writer per checkpoint is still required.

This changes the benchmark runner, not production chat retry policy. Private
0600 reader sidecars must not enter public research bundles. Cost accounting
must union all usage journals, including failed judge attempts; cached answers
must not be billed again as new work. Local race tests and full CI passed.
No new paid full evaluation has measured the retry savings yet.

## Next innovation: source-linked state and event reconciliation

Reproducible audit:

```sh
# Requires an authenticated gh session with repository read access.
gh api -H 'Accept: application/vnd.github.raw+json' 'repos/ditto-assistant/ditto-subnet/contents/services/dittobench-api/docs/longmemeval-benchmark/results/2026-09-14-slices-full-public-evidence.json?ref=2fcd1e140b637c17c713a949d2b93828ec4b4adf' > /tmp/lme-public-evidence.json
python3 services/dittobench-api/docs/longmemeval-benchmark/analyze_source_slice_weakpoints.py /tmp/lme-public-evidence.json
```

The analyzer verifies SHA-256
`4dbce2f6b28bf717b0bccf36f845491d9cfc6520dfaa10442a2d1326d7e15ceb`
before parsing. It makes no inference calls and uses only sanitized public data.
Remaining errors: multi-session 30, temporal 26, knowledge-update 15,
preference 10, single-session-user 1. Thus 71/82 lie in the first three categories.
Twelve of the 15 knowledge-update failures made no follow-up tool calls. These
are observations, not proof that all errors share one cause.

Two inspected counterexamples already contain the relevant source:

- `2698e78f`: April 3 source says therapy every two weeks; November 3 source
  explicitly says every week. Both are complete user excerpts. The answer
  selected biweekly. This is a stale-fact selection failure, not absence of
  the newer statement from initial retrieval.
- `852ce960`: August 10 source says a $350,000 Wells Fargo preapproval;
  November 29 refers to $400,000. Both are complete user excerpts. The answer
  reports conflict rather than selecting the benchmark's $400,000 answer.
  Unlike the therapy example, this requires care: the later statement refers
  retrospectively to an event. Record time is not automatically effective time.

The old [PR #881](https://github.com/ditto-assistant/backend/pull/881), inspected
at `caf7fc381c350583c1777dd787408950e072c1c7`, proposes subject state cards with
current value, evidence IDs, effective time and supersession. It remains open
and explicitly supplies only a data layer, not extraction or harness integration.
Do not blindly merge the old migration or claim it is already a working system.

### Proposed isolated experiment, not yet implemented or deployed

Prototype a **read-time evidence ledger**, with no new persistent writes or
dreaming changes. Each assertion retains subject identity, source pair ID,
verbatim support, observed time, explicitly supported effective time, and whether
it is a fact, plan, hypothetical or assistant suggestion. Preserve older versions
for historical questions; never apply an unconditional latest-wins rule.
Distinct event identity is necessary before counting repeated mentions.

Compare source slices alone against slices plus this single reconciliation stage,
using the identical prepared snapshot, candidate retrieval, reader, judge and
question-date clock. If the stage uses an additional model call, pin that model
and account for its tokens, generations and failed attempts separately. Do not
silently bundle a graph/ranker/prompt overhaul into the treatment.

First add deterministic adversarial fixtures: changed recurring schedule,
historical personal best, retrospective updates, unfulfilled plans, duplicated
event mentions, conflicting dates, partial excerpts and cross-user negative
controls. Missing or ambiguous provenance must abstain or fetch original source,
not fabricate an effective date or rewrite stored state.

Then diagnose all 82 errors and 12 regressions, explicitly labeled development
data. Register the final full 500-case A/B before running it, preserve wrong
answers, use stage-aware resume and union all cost receipts. Report paired wins,
regressions, category accuracy, tokens, calls, latency, and incremental total cost.
Require a positive net score change and no unreviewed identity/temporal safety
regressions before considering any production promotion. Set a provisional
10% captured-cost increase ceiling; disclose missing cost rather than treating
the ceiling as proven when receipts are incomplete. Validate broader benefit on
a separately untouched corpus; another run on these 500 cases is not held out.

## Reproduction and delivery workspace

Workspace: `workspaces/lme-production-slices-20260914-125810-d0b005` under
`/Users/peyton/.codex/worktrees/eb94/heyditto-stack`.
Repos: backend and ditto-subnet. Backend branches:
`codex/lme-production-slices`, `codex/lme-stage-aware-retry`.
Research branch: `codex/lme-production-slices`.
Local fixtures and prior workspaces were preserved.

Validation included:
`go test -race ./pkg/dittobench -run 'TestLME(ReaderCheckpoint|JudgeRetry)' -count=1`;
focused prompt/source-slice, checkpoint and run-ID tests;
six Python slice benchmark tests; full backend CI; authenticated preview evidence.
The next experiment is a proposal only, with no new accuracy claim.

Production activation was checked from the orchestration checkout with:

```sh
set -euo pipefail
bash .agents/skills/logs/logs.sh prod --since -20m --lines 20000 \
  --grep 'packed memory source slices' --json --limit 100 |
  jq -s '{observed_requests:length,
    requests_with_excerpts:([.[]|select(.source_slice_memories>0)]|length),
    max_added_bytes:([.[].source_slice_added_bytes]|max)}'
```

The rolling time window is not a durable traffic census; the observation above
records only the bounded sample seen during delivery.
