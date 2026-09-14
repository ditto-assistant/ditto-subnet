# Fixed subject-graph full retest — 2026-09-14

## Outcome and interpretation

**336/500 (67.2%)**, independently audited: isolated LongMemEval-S, fully
seeded/dreamed Ditto memory, fixed opt-in subject graph ON, OpenRouter
`openai/gpt-5.6-luna` with medium reasoning, built-in QA judge
`google/gemini-3.1-flash-lite`, question-date prompt clock, concurrency 8.
Final run: `20260914-135247-c274a3f96e8176577043de95b048f1f2f3e9272d`.
The [strict audit](results/2026-09-14-fixed-graph-strict-audit.json) verifies all
500 unique cases, successful query/judge statuses, native hydration for 500
fixture users, positive seeded memory for every case, opaque IDs and unchanged
prepared snapshots. There are no unanswered or silently scored error cases.

Under the same named isolated-S/Luna-medium/built-in Gemini-3.1-Flash-Lite
judge condition, the [earlier graph-ON run](ditto-full-memory-results-2026-09-13.md)
`20260913-055526-1b5755609b3ea95660fdba289e6a747adb8c5dae` scored 362/500 (72.4%).
The new result is **26 fewer correct, −5.2 percentage points**. This is a
descriptive comparison, not a matched causal estimate: source, native schema,
graph freshness and tool catalog changed. Four feedback tools and an
`includeAttachments` argument entered the catalog; no feedback-tool calls were
observed. Seed-ID sets differ in 330/500 cases; graph-marked seed cases changed
from 6 to 500. Graph-marked IDs can overlap stock retrieval. We neither tuned
against these answers nor reran incorrect answers to raise accuracy.

| Question type | Cases | Earlier ON correct | Fixed ON correct |
| --- | ---: | ---: | ---: |
| Knowledge update | 78 | 63 | 59 |
| Multi-session | 133 | 84 | 71 |
| Single-session assistant | 56 | 48 | 47 |
| Single-session preference | 30 | 21 | 19 |
| Single-session user | 70 | 63 | 60 |
| Temporal reasoning | 133 | 83 | 80 |

Question-ID-aligned transitions: 318 correct in both, 120 incorrect in both,
44 earlier-correct → fixed-incorrect, 18 earlier-incorrect → fixed-correct.
These are descriptive counts, not independence/generalization or causal proof.
The same learned Ditto ranker has [474/500 evaluation-question training overlap](ditto-retrieval-training-overlap-2026-09-13.md).
That concerns Ditto's ranking model, **not evidence that Luna was trained on
these questions**. Neither score is an uncontaminated held-out leaderboard result.

## Completed attempts and retrieval behavior

Both corrected invocations use source
`c274a3f96e8176577043de95b048f1f2f3e9272d`, binary SHA-256
`09b02ebb47094eff7729366920c5aa84fcff80acdd655cc1aef413584894e332`, and
condition digest `5b5c15226d6c2a81323d44948c40bf395f2e0c20f4b6d392a0c7cc51fd40a4c1`.

| Invocation | UTC start–finish | New judged cases | Graph calls / failures | Candidate occurrences |
| --- | --- | ---: | ---: | ---: |
| `20260914-132613-c274a3f96e8176577043de95b048f1f2f3e9272d` | 13:26:13.422821–13:38:49.354608 | 495 | 3,744 / 0 | 31,140 |
| `20260914-135247-c274a3f96e8176577043de95b048f1f2f3e9272d` | 13:52:47.178608–13:56:29.394439 | 5 | 2,543 / 0 | 21,712 |

The first report exited 1 because five reader attempts returned
`agent loop: remote error: tls: bad record MAC`. The exact errors and original
hashes are pinned by the [bounded resume protocol](ditto-fixed-tls-resume-2026-09-14.md).
One authorized same-binary retry copied the 495 completed rows into a new
checkpoint; every reused row is exactly equal to its original. Only the five
unjudged cases received new reader/judge attempts. Context preparation still
ran for all 500 cases again, including query embeddings and retrieval.
Original artifacts were not overwritten. The first report correctly fails the
strict audit; only the complete second report passes.

Together: **6,287 graph calls, zero discovery failures**, 52,852 candidate
occurrences (not unique memories), and 500 final cases with graph-marked seeds.
The final answer traces contain three explicit subject-neighbor tool calls,
reported separately from discovery counters. The earlier ON arm's 98.93%
discovery fallback problem is absent here; this operational fix does not imply
an accuracy improvement. Final traces also include ten web-search tool calls.

Invocation wall durations are 755.932s and 222.216s (978.148s summed active
invocations; the gap is excluded). Recomputed successful-case latency is mean
8,446.562ms / median 5,129.5ms / p95 23,275ms. This latency excludes seed-context
preparation, separate judging and failed attempts. Do not use the resumed
five-sample Standard/Speed aggregate as full-run timing.

## Costs per question and campaign

The [cost summary](results/2026-09-14-fixed-graph-cost-summary.json) and
[500-row per-question ledger](results/2026-09-14-fixed-graph-per-question-costs.jsonl)
union both corrected usage journals by generation ID, **including the five
failed reader attempts**. All 1,826 captured generations are GET-reconciled:
1,326 reader and 500 judge. The union reused 1,787 receipts and fetched only 39
new generation IDs. Requested/resolved model aliases are recorded explicitly.

| Scope | OpenRouter charges | BYOK upstream estimate | Estimated captured sum |
| --- | ---: | ---: | ---: |
| Corrected run, both attempts | $0.05018800 | $1.75086879 | **$1.80105679** |
| Separate invalid empty-context attempt | $0.03843100 | $1.02672237 | $1.06515337 |
| This rerun campaign, captured calls | $0.08861900 | $2.77759116 | **$2.86621016** |

For the corrected 500 questions, captured all-attempt reader/judge estimated
cost is mean **$0.00360211358**, median $0.002550555, nearest-rank p95 $0.00759277.
The invalid attempt is excluded from these valid-question statistics but not
from campaign spending. BYOK amounts are OpenRouter's provider-reported upstream
estimates, not vendor invoices; the reader's $0 OpenRouter charge is not free
inference. See [accounting methodology and official sources](ditto-fixed-retest-cost-methodology-2026-09-14.md).

Original seeding/dreaming cost remains **unknown**, not $0: retained preparation
logs lack generation IDs/usage, and all 124,366 memory rows have null token usage.
The reused fixture incurs zero new extraction/dreaming calls in this rerun;
graph refresh used existing embeddings and suppressed detached label inference.
That does not erase the original preparation expense. Query-embedding costs,
including the repeated 500 preparation passes, and charges before any captured
generation ID remain unknown. Thus full lifecycle total and seed/dream cost
amortized per question remain **null**. The user-requested all-in total is not
established. Including recovered historical OFF/ON reader-only estimates gives
$6.29601957 across the retained captured campaign scopes—not an all-in bill.

## Reproduction and release state

Work in `workspaces/longmemeval-fixed-full-retest-20260914-082235-f21c18/`:
owning clones `backend` and `ditto-subnet`, shared branch
`codex/longmemeval-fixed-full-retest`. Preserve the private fixture and all earlier
workspaces. Source-pinned launch/resume commands and input hashes are in the
[resume protocol](ditto-fixed-tls-resume-2026-09-14.md); use the final report above
with `audit_backend_run.py --graph-retrieval --require-hydration-preflight`.
The exact manifest/dataset/source/tool/prompt/weight hashes are in the strict audit.
Both invocation before/after prepared hashes equal
`8af521b88d155c4c6e81befe89495b7e74328f65a734b91fdd45348a1a8a8023`.

From the subnet clone, with paths to private evidence and an authorized key
already in process environment (never in shell history or committed files):

```sh
python3 services/dittobench-api/integrations/longmemeval/audit_resumed_openrouter_costs.py \
  --report "$FINAL_REPORT" --journal "$FIRST_JOURNAL" --journal "$RETRY_JOURNAL" \
  --recovery "$FIRST_RECONCILED_RECEIPTS" --fetch \
  --private-output "$NEW_PRIVATE_RECEIPTS" --summary-output "$NEW_SUMMARY"
python3 -m unittest discover -s services/dittobench-api/integrations/longmemeval -p 'test_*.py'
```

The committed per-question ledger is the cost summary's per-case reader/judge
records joined by question ID to its estimated-cost list; it contains no raw
generation IDs, prompts, answers or credentials. Full reports retain inherited
Hippius publication behavior described in the prior methodology; they are not
sanitized. Earlier source/results and private evidence remain preserved.

[Backend PR #2735](https://github.com/ditto-assistant/backend/pull/2735) merged
at 14:00:33Z as `fa8b02f750bec4afeee15cd17e552fac72052601`, accepting the
**default-off operational repair**, not a demonstrated accuracy benefit.
Merge is distinct from deployment: deployment was not yet verified at this
report handoff, and no global subject-graph activation was performed.
