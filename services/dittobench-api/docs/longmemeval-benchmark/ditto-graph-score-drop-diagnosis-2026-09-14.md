# Why the corrected graph run scored lower

## Bottom line

The observed change is **362/500 (72.4%) → 336/500 (67.2%)**, a net loss of
26 correct answers: 44 regressions and 18 improvements. Both are isolated
LongMemEval-S, Luna medium, built-in Gemini 3.1 Flash Lite judge, question-date
prompt clock, seeded/dreamed memory and nominal graph ON. Exact runs:

- Earlier: `20260913-055526-1b5755609b3ea95660fdba289e6a747adb8c5dae`.
- Corrected: `20260914-135247-c274a3f96e8176577043de95b048f1f2f3e9272d`.

This investigation demonstrates **lossy evidence presentation, failures to
expand or correctly interpret evidence, and inconsistent judging examples**.
It does **not** isolate the causal contribution of graph expansion to the net
5.2-point change. The earlier arm mostly fell back after graph discovery errors;
it is not a successful matched graph baseline. The full new execution audit
passed, so missing answers, graph SQL errors and reasoning-stream fallback
are not the explanation for the final score.

Read the [reproducible aggregate evidence](results/2026-09-14-graph-score-diagnosis.json)
and [completed-run protocol/cost report](ditto-fixed-graph-results-2026-09-14.md).
No paid inference, regrading, product changes, fixture writes, merge or deployment
was performed for this diagnosis. The original scores remain unchanged.

## What the 500 paired records establish

| Observation | Earlier | Corrected | Interpretation |
| --- | ---: | ---: | --- |
| Multi-session correct | 84/133 | 71/133 | 13 of the 26 net lost answers are here |
| Mean recorded session recall | 99.06% | 99.06% | Session coverage did not improve; this is not fact visibility |
| Cases with graph-marked seed IDs | 6 | 500 | Discovery actually ran in the corrected arm; markers also include overlap with stock retrieval |
| Mean cumulative prompt tokens per question | 26,344.974 | 27,849.110 | More processing, not proof of more useful evidence; includes tools/repeated turns |
| Regressed cases with no tool calls | 7/44 | 14/44 | More of these failures ended without further evidence gathering |

Of the 44 regressions, **42 still have 100% recorded session recall**.
Nineteen regressions and nine improvements occur among the 170 cases with
identical seed-ID sets. Sixteen regressions even preserve seed-ID order.
The other 330 cases contain 25 regressions and nine improvements. Identical IDs
do not prove identical full rendered prompts, later tool results or provider
behavior, but they disprove a blanket account of all losses as missing seed IDs.

All 500 answer-source records are `final` in each run. There are no exactly
identical-answer verdict flips among the 44 regressions; the judge examples
below involve near-equivalent, not byte-identical, answers.

## 1. Retrieved memory is often a summary that omits the answer

The backend's [LongTermJSON](https://github.com/ditto-assistant/backend/blob/c274a3f96e8176577043de95b048f1f2f3e9272d/pkg/services/memorystore/types/memory.go#L1003)
emits `summary` **instead of** the original user/assistant text when a summary
exists. PostgreSQL `description` is mapped into that summary. This is not full
evidence hydration as seen by the reader, even when the native DB hydration
and session-recall checks pass.

Read-only probes of the preserved private fixture verify:

| Case | Original fact, selected in both runs | Summary omission | Observed answer behavior |
| --- | --- | --- | --- |
| `3c1045c8` | Department average age is 29.5 | Summary says the user is considering a master's for professionals in their 30s; omits 29.5 | New answer knows user age 32 but asks for department average; no tool call |
| `gpt4_372c3eed` | Associate's degree at Pasadena City College | Summary reduces this to a strong CS/data-science foundation | New answer counts high school + bachelor's as 8 years, omitting associate's; no tool call. Old answer used five tools and returned 10 |
| `gpt4_cd90e484` | Binoculars obtained exactly three weeks ago | Summary omits the relative interval | Both runs fetched, but selected different binoculars exchanges; see below |

This summary behavior existed in both runs. It is a demonstrated bottleneck
and explains specific new wrong answers, **not a newly introduced bug that
alone accounts for the entire difference**. The probes hash all selected
rows and record fact-presence booleans without publishing full memory contents.

The [runner](https://github.com/ditto-assistant/backend/blob/c274a3f96e8176577043de95b048f1f2f3e9272d/pkg/dittobench/runners/longmemeval.go#L323)
initializes `AlreadyFoundPairIDs` with those summary-only seeds. Subsequent
searches exclude them; an explicit `fetch_memories` can still expand an ID.
Thus discovery of a summarized record is treated as already seen even if its
answer-bearing detail was never shown. High session recall can hide this gap.

## 2. Reader decisions lose facts or confuse event time with record time

These are trace-observed failure modes, not hypothetical graph effects:

- `gpt4_cd90e484`: the earlier fetch retrieved the exchange saying binoculars
  arrived **three weeks ago**, plus goldfinches seen **a week ago**, yielding
  two weeks. The corrected fetch used a generic binoculars mention instead;
  its answer used the **2-hour-4-minute difference between record timestamps**.
  The goldfinch exchange still explicitly said a week ago. This combines an
  evidence-selection mistake with temporal interpretation failure.
- `e6041065`: the earlier fetch expanded the memory saying only **two** of five
  packed shoes were worn. The corrected fetch expanded adjacent packing advice
  and the five-shoes mention, but not the two-shoes exchange. It then asked for
  the missing count rather than asserting 40%.
- `2698e78f`: both seed sets contain weekly and biweekly therapy summaries.
  Neither run called tools. The corrected answer selected biweekly, while the
  accepted earlier answer mentioned weekly and the conflict. More retrieval
  alone does not resolve temporal precedence/conflicting facts.
- `2ce6a0f2`: the corrected answer explicitly excluded the history-museum event
  as not art-related, producing three rather than four. The information was
  present; the interpretation differed.

These examples do not prove Luna is intrinsically worse with a graph. They
show the current context/tool policy does not reliably turn available records
into exact answers.

## 3. Graph expansion changes competition, not just available context

The [candidate expansion](https://github.com/ditto-assistant/backend/blob/c274a3f96e8176577043de95b048f1f2f3e9272d/pkg/services/retrieval/subject_graph.go#L239)
unions up to 40 graph candidates into the stock pool **before** normalization
and final top-K selection. It preserves the initial stock candidate pool, not
the final stock context. New candidates can displace final selections. Pool
changes also alter frequency/recency bounds and V2 neighbor-density features
for existing candidates. The same trained weights do not imply the same scores.

Therefore this is not "old evidence plus some extra helpful evidence". It is
a changed retrieval distribution under fixed output limits. Seed sets changed
in 330/500 questions, but the saved reports do not contain complete scored
candidate pools or original serialized seed payloads, so they cannot attribute
every displacement or the 5.2-point loss to this mechanism. Treat harmful
competition/normalization as a **source-supported hypothesis needing a matched
ablation**, not a measured causal conclusion. Graph provenance is not proof a
record entered exclusively through the graph arm.

## 4. Some of the score difference is sensitive to judging policy

Three especially clear **manual review candidates** among regressions:

- `88432d0a_abs`: both answers say no recorded egg-tart baking and "0 times".
  The old answer passes; the new one fails for giving a fabricated count.
- `e5ba910e_abs`: both identify $378 headphones and say the iPad price is
  unavailable, requesting it. The old passes; the new fails for supplying the
  known headphones price rather than fully abstaining.
- `cc539528`: both list Ruby/Python/PHP plus Java and Node.js. The old passes;
  the new fails for extra languages; it additionally mentions SQL.

These inconsistent acceptance examples make the single-judge score noisy.
They are **not** authorized score corrections, an estimate of all judge error,
or proof that rejudging would recover 26 points. No verdict was overwritten.

## What changed besides the graph

The saved tool catalog changed: four feedback tools plus `includeAttachments`
were added during current-main consolidation. No new-feedback tool calls were
observed, but unused definitions are still model-visible. Reader/judge model
names, reasoning setting, dataset, manifest and learned weights match.
Prompt source hashes differ; inspection found feedback-gated additions with
the flag off in this harness, but the fully rendered prompt was not archived.
Native recency still uses SQL `NOW()` even though the prompt clock is pinned.
These are confounders, not quantified explanations. The source fixture's
prepared semantic content was verified preserved by the preceding campaign.

The existing 474/500 ranker-training overlap applies to both runs. It prevents
a clean held-out claim but does not itself explain the new difference. This
is not evidence of hosted Luna training overlap.

## Recommended next experiment, not executed

1. Freeze one source, tool catalog, prepared snapshot, retrieval clock and
   provider settings; run matched graph OFF/ON, with repeated runs if claiming
   a reliable effect. Keep wrong answers rather than selecting better retries.
2. Save exact seed payloads and full-content-fetch coverage separately from
   session recall. Record scored candidate pools and channel-exclusive IDs.
3. Evaluate a separately labeled evidence-expansion condition: fetch originals
   for exact counts, dates and updates; distinguish summary-seen from full-seen.
   Do not use gold answers to select the live retrieval policy.
4. Blindly rejudge the fixed saved answers under a frozen rubric to measure
   judge sensitivity independently of reader/retrieval changes.

This order tests the suspected mechanism without treating a hand-tuned score
increase as proof. Any future protocol/model changes need their own condition.

## Reproduce this diagnosis

`diagnose_graph_score_drop.py` pins both input report hashes, validates exactly
500 successful unique cases with matching question/gold/model identities,
computes descriptive transitions, and optionally probes only three named
fixture users in `BEGIN READ ONLY` with a five-second statement timeout.
It refuses an existing output and never calls a model. Set `$OLD_REPORT` and
`$NEW_REPORT` to the named preserved run files and `$PRIVATE_FIXTURE_URI` to
the existing loopback fixture connection; do not use production credentials.

```sh
python3 services/dittobench-api/integrations/longmemeval/diagnose_graph_score_drop.py \
  --old "$OLD_REPORT" --new "$NEW_REPORT" \
  --fixture-db "$PRIVATE_FIXTURE_URI" --psql /opt/homebrew/opt/libpq/bin/psql \
  --output /tmp/new-lme-score-diagnosis.json
python3 -m unittest discover -s services/dittobench-api/integrations/longmemeval -p 'test_*.py'
```

Task workspace: `workspaces/lme-score-regression-diagnosis-20260914-105807-546052/`;
only `ditto-subnet` changed on `codex/lme-score-regression-diagnosis`, based on
results PR #1895 at `a93f4721365318395c31f96db10a3c2d47d72840`. Earlier source
checkouts and reports remain read-only. Native source links above are pinned
to the evaluated backend commit, not current main.
