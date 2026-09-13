# Subject-graph retrieval RCA: execution failure, not a clean accuracy test

The graph-enabled campaign mostly fell back to stock retrieval because its
candidate SQL exceeded the existing deadline. A scoped-query rewrite now passes
database-only probes across all 500 fixture users. **No new answer/judge run has
been performed with this fix**, so these measurements establish execution and
candidate parity, not a new memory-accuracy score.

## Preserve the original result and its limits

The frozen **isolated LongMemEval-S, fully seeded and dreamed** campaign recorded
67.2% OFF and 72.4% ON with `openai/gpt-5.6-luna`, `medium` reasoning,
`google/gemini-3.1-flash-lite` as the built-in QA judge, and a question-date prompt
clock. [The original final report](ditto-full-memory-results-2026-09-13.md)
contains both run IDs, retries, timings and provenance. ON is
`20260913-055526-1b5755609b3ea95660fdba289e6a747adb8c5dae`; final OFF is
`20260913-054910-1b5755609b3ea95660fdba289e6a747adb8c5dae`.

ON recorded 3,711 failures / 3,751 discovery calls (98.93% fallback), 350 candidate
occurrences, six graph-marked seed cases and no neighbor-tool calls. All six
graph-marked cases had unchanged correctness; full seed-ID sets differed in 62
cases. The observed score difference is not established as a causal graph gain.
[The separate training-lineage audit](ditto-retrieval-training-overlap-2026-09-13.md)
also verifies supervised ranking-input overlap with 474 evaluation questions and
byte-identical checkpoint export. This concerns Ditto's ranker, **not evidence
about Luna's training**, and prevents a clean held-out generalization claim.

## What actually failed

An [aggregate server-log receipt][errors] associates statements and errors by
exact log timestamp and backend PID over the original ON window. It accounts for
all 3,711 graph failures: **3,710 statement timeouts and one user-request
cancellation**, with no unmatched errors/statements. Three additional timeouts
belong to non-graph SQL and are excluded. The parser records the raw input hash
without publishing SQL, embeddings, raw errors or credentials. PostgreSQL's
user-request label does not establish that a human canceled a request.

The earlier 55/55 bounded log observation was a tool-recorded sample, not a
separately retained exact raw-log window. The complete receipt supplies stronger
evidence without changing the old report or backfilling new runtime counters.

On a private copy, [actual old-query plans][old-plan] show the representative
anchor stage taking 1,918.367 ms and 3,553,272 shared buffer hits. Its
`idx_memory_pairs_user_kg` scan ran **85,444 times**, returning 119 rows per loop
against an estimate of one. The full old query exceeded even a diagnostic
10-second timeout. Production's two-second SQL statement budget was unchanged.
The repeated scope/incidence joins and severe row underestimation explain this
observed expensive plan; this is not proof that an index was missing or that all
production queries have the same plan.

## Fix, including the failed intermediate attempt

Merely materializing eligible memories still timed out in all three tested
scopes; the [failed prototype receipt][prototype] is retained. The successful
[query rewrite][query] materializes permitted memories and `eligible_links`
once, reuses `ANY` membership for eligible anchor/neighbor subjects, and joins
the scoped incidence set for final candidates. It avoids repeated correlated
memory/link scans while retaining candidate selection semantics.

The opt-in/default-off behavior, user/KG/session/thread/date/exclusion scopes,
caps, ranking thresholds and stock fallback remain. The SQL statement budget
remains two seconds inside a 2.5-second discovery context, plus cleanup; no
deadline increase, schema migration, evaluation tuning or retraining is used.
The [fixed actual plan][fixed-plan] measures anchors at 6.747 ms / 2,569 shared
hits and the full query at 8.254 ms / 8,986 shared hits. It reads the 242 eligible
memory rows and 556 scoped link rows once each in this representative scope.

## Database-only verification

The [scale helper and independent reference][scale-test] select deterministic
stored subject embeddings, not fresh question embeddings or gold-answer labels.
The reference uses separate scope-indexed reads and reconstructs v1 candidate
ranking in Go. These are execution/scope probes, not end-to-end reader tests.

| Probe | Errors | Reference parity | p50 / p95 / maximum discovery ms |
| --- | ---: | ---: | --- |
| All 500 users, serial | 0 | 500/500 | 47.038 / 295.619 / 937.110 |
| All 500 users, concurrency 8, uncontended repeat | 0 | 500/500 | 14.370 / 54.175 / 394.745 |

Both probes leave every tested connection usable and return 3,566 candidate
occurrences with identical per-user candidate hashes. The repeat's p99 is
315.030 ms and wall time is 3.370 seconds. The [receipt collection][receipts]
also retains the first passing concurrency-eight attempt, which overlapped a
short unrelated test load. Cache warmth, ordering and workload differ: these
numbers are observations, not a controlled speedup factor or production SLA.
Durations include pool acquisition and candidate discovery, exclude the later
reference checks, and can compete with concurrent reference reads; wall time
includes all checks. An independent coordinator rerun again passed all 500
users, reference comparisons and connection checks, with zero errors and the
same ordered candidate hashes; its receipt is retained too.

The final PostgreSQL regression suite passed without skips: three retrieval
tests cover recall/OFF parity, scope/caps/cycles, and timeout/savepoint fallback;
two memory-tool tests include ten unsafe-argument subcases. Additional focused
retrieval, audit and parser tests passed; the subnet integration suite passed
all 55 tests, including six offline lineage tests. See the owning backend's
[PG tests][pg-tests] and [neighbor-tool tests][tool-tests] for executable cases.
A separate [prepared neighbor-SQL probe][neighbor-probe] passed custom and
generic plans for one connected subject on the copy, returning eight rows in
each mode within the unchanged budget. It is not tool API/authentication QA,
whole-tool latency coverage or a comparative plan-mode timing experiment.

## Isolation, reporting and next gate

The original source `1b5755609b3ea95660fdba289e6a747adb8c5dae`, binary SHA-256
`695b6c3a0338b902949deb55456a87b17e1ba97e2b92dca63054e187ef75fa21`, prepared
fixture, opaque manifest and accuracy artifacts are preserved unchanged. RCA
probes use the separate `ditto_lme_graph_rca` database copy; PG regression tests
create/clean their own databases, not the copy or original. No inference or
production mutation is part of these probes.

New [failure-reason metadata][audit] distinguishes five bounded classes and
reports discovery complete only when calls are positive, failures zero and
counts consistent. Counters remain invocation-only; `-require-graph` attests
fixture readiness, not successful runtime discovery. These new fields are
unavailable in the frozen report, not implicitly zero. Before quoting a fixed
graph accuracy result, freeze the new source and repeat matched full QA with
the same provenance gates and explicit exposed-dataset limitations. No merge,
deployment, clean holdout result or new paid QA completion is asserted here.

Work is isolated in
`/Users/peyton/.codex/worktrees/eb94/heyditto-stack/workspaces/longmemeval-graph-rca-20260913-121556-cd7832/`,
with `backend` and `ditto-subnet` on shared branch `codex/longmemeval-graph-rca`.
The [scale summary][scale-summary] records exact probe commands and receipt
hashes. Final backend validation uses `go tool xgo test -count=1 -v -run
'^(TestSubjectGraphPG|TestExploreSubjectNeighbors)' ./pkg/services/retrieval
./pkg/tools/memory` with `TEST_POSTGRES_URI` pointing to the isolated local
server. Subnet validation is `python3 -m unittest -q` from
`services/dittobench-api/integrations/longmemeval`.

[errors]: https://github.com/ditto-assistant/backend/blob/9178a0f0ba074bdddfd9f8a7ac1c8518c7cdf7f9/scripts/benchmarks/graph-failure-receipt-20260913.json
[old-plan]: https://github.com/ditto-assistant/backend/blob/9178a0f0ba074bdddfd9f8a7ac1c8518c7cdf7f9/docs/reports/2026-09-13-graph-rca-old-actual-plan.json
[fixed-plan]: https://github.com/ditto-assistant/backend/blob/9178a0f0ba074bdddfd9f8a7ac1c8518c7cdf7f9/docs/reports/2026-09-13-graph-rca-fixed-actual-plan.json
[prototype]: https://github.com/ditto-assistant/backend/blob/9178a0f0ba074bdddfd9f8a7ac1c8518c7cdf7f9/docs/reports/graph-rca-20260913/eligible-materialized3.json
[query]: https://github.com/ditto-assistant/backend/blob/9178a0f0ba074bdddfd9f8a7ac1c8518c7cdf7f9/pkg/services/retrieval/subject_graph.go
[scale-test]: https://github.com/ditto-assistant/backend/blob/9178a0f0ba074bdddfd9f8a7ac1c8518c7cdf7f9/pkg/services/retrieval/subject_graph_scale_test.go
[receipts]: https://github.com/ditto-assistant/backend/tree/9178a0f0ba074bdddfd9f8a7ac1c8518c7cdf7f9/docs/reports/graph-rca-20260913
[pg-tests]: https://github.com/ditto-assistant/backend/blob/9178a0f0ba074bdddfd9f8a7ac1c8518c7cdf7f9/pkg/services/retrieval/subject_graph_pg_test.go
[tool-tests]: https://github.com/ditto-assistant/backend/blob/9178a0f0ba074bdddfd9f8a7ac1c8518c7cdf7f9/pkg/tools/memory/subject_neighbors_test.go
[audit]: https://github.com/ditto-assistant/backend/blob/9178a0f0ba074bdddfd9f8a7ac1c8518c7cdf7f9/pkg/dittobench/longmemeval_subject_graph_audit.go
[neighbor-probe]: https://github.com/ditto-assistant/backend/blob/9178a0f0ba074bdddfd9f8a7ac1c8518c7cdf7f9/scripts/benchmarks/graph-neighbor-probe-20260913.json
[scale-summary]: https://github.com/ditto-assistant/backend/blob/9178a0f0ba074bdddfd9f8a7ac1c8518c7cdf7f9/docs/reports/graph-rca-20260913/scale-summary.json
