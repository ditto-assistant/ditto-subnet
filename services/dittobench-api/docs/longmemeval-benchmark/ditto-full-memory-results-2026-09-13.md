# Full-memory Luna retest: completed paired result

Both isolated LongMemEval-S arms completed and passed the independent strict
500-case audit. Graph OFF scored **67.2% (336/500)**; graph ON scored
**72.4% (362/500)** with the same frozen reader binary and prepared memory
snapshot. This is an observed **+5.2 percentage-point flag-condition difference,
not a demonstrated causal graph benefit**: graph discovery fell back on
98.93% of calls, and effective graph coverage was very limited.

Reader: `openai/gpt-5.6-luna`, `medium` reasoning, observed provider `OpenAI`.
Judge: `google/gemini-3.1-flash-lite`, existing Ditto binary QA judge prompt.
Prompt clock: `question-date`. Both arms used full native memory preparation,
opaque memory IDs, isolated users, `-require-graph`, and concurrency 8. OFF
disables subject-edge retrieval, not subject preparation or ordinary subject
search. ON adds the opt-in subject-edge candidate stage and neighbor tool.
These are research QA scores, not production miner scores or official
LongMemEval evaluator results. No official post-hoc judge was run.

## Audited results

| Condition | Correct | QA accuracy | Descriptive Wilson 95% interval |
| --- | ---: | ---: | ---: |
| Full memory, graph retrieval OFF | 336/500 | 67.2% | 62.97–71.17% |
| Full memory, graph retrieval ON | 362/500 | 72.4% | 68.32–76.14% |

| Question type | OFF | ON |
| --- | ---: | ---: |
| Knowledge update | 59/78 | 63/78 |
| Multi-session | 70/133 | 84/133 |
| Single-session assistant | 44/56 | 48/56 |
| Single-session preference | 16/30 | 21/30 |
| Single-session user | 62/70 | 63/70 |
| Temporal reasoning | 85/133 | 83/133 |

Both were correct on 319 cases; ON alone on 43, OFF alone on 17, neither on
121. The machine audit retains the exact paired McNemar calculation as a
descriptive test under its assumptions, not proof of generalization or graph
causality. This is one provider-sampling pair, not repeated-run variance.
Cases reuse 19,195 source sessions across 23,867 occurrences, so independence
is not established. The Wilson intervals have the same limitation.

Final run IDs:

- OFF: `20260913-054910-1b5755609b3ea95660fdba289e6a747adb8c5dae`
- ON: `20260913-055526-1b5755609b3ea95660fdba289e6a747adb8c5dae`

The [OFF audit](results/2026-09-13-ditto-graph-off-audit.json) and
[ON paired audit](results/2026-09-13-ditto-graph-on-paired-audit.json) verify exact
dataset/category/question coverage, successful final answers and judgments,
requested/observed model identity, per-case prompt dates and flags, source,
weights, manifest and prepared-state hashes. All 1,281 OFF and 1,312 ON recorded
answer-provider response identities specify the requested Luna model and
OpenAI provider. They do not independently attest dreaming or judge-provider
identity. Attempted unavailable tools such as `search_web` receive a benchmark
stub; their name counts are not evidence of web access.

## Graph execution was the limiting factor

ON recorded **3,711 discovery failures out of 3,751 calls (98.93%)**, with
fallback to stock candidates. The 350 successful candidate occurrences are not
350 globally unique memories or cases. Counters include seed preparation and
graph-enabled composite retrieval in this invocation, not explicit neighbor
tool calls. No `explore_subject_neighbors` calls appeared in the 500-case traces.

Only six cases had graph-marked seed memories: 39 ID occurrences, of which 35
were already in the corresponding OFF seed sets. Just four IDs across three
cases were absent from those OFF seeds. All six graph-marked cases had
**unchanged correctness**: four correct in both arms, two incorrect in both.
Graph candidates in subsequent retrieval could still affect other cases;
seed-only diagnostics do not prove zero graph influence.

The complete seed ID sets differed on 62 cases, ignoring order. The frozen
database therefore did not yield identical seed contexts. This audit does not
identify why those sets differed or isolate stochastic provider behavior,
retrieval behavior, rendered prompts, or graph effects. Do not assign the
26-answer difference to four newly observed seed IDs, and do not describe this
run as a high-coverage test of the graph's potential.

Frozen `pkg/services/retrieval/subject_graph.go` uses a maximum two-second SQL
statement budget within a 2.5-second discovery context, plus cleanup, and falls
back on errors. A bounded in-run log observation identified
graph SQL statement timeouts. The preserved representative, **nonexecuting**
EXPLAIN at `2026-09-13T06:09:53.804728Z` estimated one scoped row each for
subjects/pairs/links versus observed counts 319/242/556, with nested-loop join
filters and correlated user/KG partitions. Recent autoanalyze existed. This is
consistent with selectivity underestimation, not a proven causal root cause or
missing-index diagnosis. It was not the actual in-flight prepared/generic plan;
no EXPLAIN ANALYZE, maintenance, statistics change, tuning, or mid-arm source
change was performed. The [verification receipt](results/2026-09-13-ditto-final-verification.json)
pins the diagnostic artifact and its limitations.

## Timing and retries

| Measure | OFF | ON |
| --- | ---: | ---: |
| Mean selected per-case reader latency | 11.016 s | 12.506 s |
| Median selected per-case reader latency | 4.661 s | 6.499 s |
| Nearest-rank p95 reader latency | 24.231 s | 49.508 s |
| Recorded reader prompt tokens | 12,827,533 | 13,172,487 |
| Recorded reader output tokens | 401,207 | 146,665 |
| Sum of invocation wall intervals | 1,717.231 s | 5,303.729 s |

Per-case timing starts inside `QueryCase`, **after seed preparation**, excluding
serial graph candidate discovery. It is not a sufficient graph-performance
comparison. The wall intervals include seed preparation and provider tails,
but not pre-run bootstrap, post-run upload, or the earlier dreaming campaign.
These are observed timings, not a controlled decomposition of performance.
Reader token totals exclude separate judging/preparation and failed attempts;
they are not complete billed usage. Luna pricing was absent from the harness
table, so no monetary amount is reported.

OFF attempt 1 ran `05:21:53.086755–05:46:42.086389Z` (1,488.999634 s) and retained
497 judged cases plus three unjudged TLS `bad record MAC` errors. The slow last
case completed without interruption. An initial resume command then failed at
bootstrap due to a mistyped DB hostname, before any reader/preflight inference.
The corrected resume ran `05:49:10.865647–05:52:59.097506Z` (228.231859 s),
retrying only the three missing reader/judge pairs. All 497 earlier full per-case
objects are exactly preserved. Seed contexts were nevertheless re-prepared for
all 500 cases, so this was not only three total embedding/provider calls.
First start to final finish spans 1,866.010751 s including the gap. The resumed
Standard/Speed aggregate is not used as a 500-case latency summary.

ON ran `05:55:26.872729–07:23:50.602083Z` (5,303.729354 s), without retries or
intervention. Original incomplete reports and all logs are preserved;
append-only checkpoints are retained at their final hashes. Successful reports
followed the inherited full-report Hippius upload;
no bucket ACL/readability claim is made. The incomplete OFF report did not upload.

## Integrity and claim boundaries

Both readers used source `1b5755609b3ea95660fdba289e6a747adb8c5dae` and binary
SHA `695b6c3a0338b902949deb55456a87b17e1ba97e2b92dca63054e187ef75fa21`, with the
same pinned configuration. Three independent snapshots—before OFF, after OFF,
after ON—are byte-identical, and match all backend pre-snapshot objects and
post hashes. Snapshot SHA:
`01c6a7070cba4e1455894c6da07132dcbba1b5cbc37c2c58c9f26db4adb3d055`.
Final opaque manifest SHA:
`a232c2b7ee77f06597535416682c00187f9c44bb2a324de7ccd5dce3fa13ccef`.

Preparation was a documented multi-phase, mixed-source recovery process, not
one flawless invocation of the final reader revision. The full preparation,
113-user bounded settle, graph/label completion, zero remaining pending work,
empty receipt proof set, opaque rewrite and exact original-turn audit are pinned
in the [preparation handoff](results/2026-09-13-ditto-prepared-opaque-reader-handoff.json)
and [protocol](ditto-full-memory-retest.md). Configured extraction model/fallback
is not proof that every dreaming call used Luna. No preparation or reader
process remains active; preserved fixture containers and volumes were not deleted.

The learned-retrieval training data in the repository overlaps **474/500**
evaluation question IDs. Binary training lineage is not completely established,
but an unqualified held-out or fair leaderboard claim is inappropriate. The
historical 89.6% Gemini result used a different answerer/preparation condition,
omitted turns, wrong occurrence dates and model-visible evidence/abstention
labels. Do not frame the difference from 89.6% as a clean model or memory-system
regression. There is also no matched Luna no-dream control here, so the isolated
benefit of dreaming is not measured.

## Reproduce the result audit

Use the pinned dataset and private final reports with the exact filenames above.
From the subnet repository root, set `LME_BASE` to the preserved backend clone,
`LME_DATASET` to its `.tmp/lme/data/longmemeval/longmemeval_s_cleaned.json`, and
`LME_OFF`/`LME_ON` to the final report paths. Output files must be new.

```sh
python3 services/dittobench-api/integrations/longmemeval/audit_backend_run.py \
  --dataset "$LME_DATASET" --run "$LME_OFF" --output off-audit.json \
  --condition full-memory-luna-medium-opaque-graph-off \
  --judge-model google/gemini-3.1-flash-lite
python3 services/dittobench-api/integrations/longmemeval/audit_backend_run.py \
  --dataset "$LME_DATASET" --run "$LME_ON" --paired-run "$LME_OFF" \
  --output on-paired-audit.json --condition full-memory-luna-medium-opaque-graph-on \
  --judge-model google/gemini-3.1-flash-lite --graph-retrieval
```

The ON audit is the left side of the stored paired table; OFF is the right, so
its `right_minus_left_accuracy` is -0.052. The human-readable ON-minus-OFF
difference above is +0.052. The [protocol](ditto-full-memory-retest.md) retains
exact reader commands and independent read-only snapshot reproduction.
This evidence does not merge, deploy, or activate any production change.
