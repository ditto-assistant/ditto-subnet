# Live DittoBench runtime

Scored cases overlap. Diagnose and change that through Backroom MCP, not
HeyDitto app logs.

Source review of a miner harness is `$backroom-review`. The scorer contract is
`services/dittobench-api/PROTOCOL.md`.

## Contract

The scorer POSTs overlapping `/run` against the **one process-wide inference
URL**. Miners do not route per-question `inference_base_url`.
`case_scoped_inference_v1` is ignored.

Kept: per-case `tool_endpoint`, ticket-scope `model_use`, memory wave barriers
(seed wave *w*, run those cases, then *w+1*).

`benchmark_runtime.case_concurrency` is stamped onto **new** v10+ leases.
In-flight leases keep their old stamp. v9 has no stamp and uses the scorer
default.

## Inspect (read first)

1. `get_inference_concurrency_settings` —
   `effective.settings.benchmark_runtime.case_concurrency` and
   `expectedRevision` (`effective.revision`). Do not assume serial or 4.
2. `get_inference_runtime_metrics` — chat/embedding `peak_*_concurrency_60m` vs
   limits, `failed`/`timed_out`, relay `capacity_declines`. This admin path can
   30s-timeout; retry once, then proceed on tickets.
3. `get_confirmation_lane_diagnosis` → `fleet.versions` (validator stack).
   Broker chat/tool admission scales to `max(4, case_concurrency)` on 0.100.4+.
4. `list_stuck_submissions` with `state: ["running"]`.
5. `get_validation_retry` on one live agent — `tickets[].issued_at`, `deadline`,
   `failure_detail`. Serial Bench 11 was 97–109 min and died at `6600.0s`.
   Overlapping `/run` finishing in well under that, with `tool_mean` not ~0, is
   the live proof.

## Set case concurrency

Only when the user asks to change it. `set_inference_concurrency_settings`
requires the **complete** settings object, `expectedRevision` from the GET, and
confirmation `APPLY INFERENCE CONCURRENCY SETTINGS`.

Before raising:

- Fleet majority on a stack that scales broker admission (0.100.4+).
- Chat peaks still under `chat_per_*` limits (32/256/512 today).
- `chat_request_budget` — 4-wide already forced 8192→16384; **16384 is the
  schema hard ceiling**. 8-wide burns it faster; you cannot raise it from
  Backroom.
- Embedding peaks vs 12/64/192. Validator-local
  `DITTOBENCH_V8_EMBEDDING_CONCURRENCY` defaults to 8 (binds before Platform).
- Values ≤16 pass the job-wire max even on older validators.

After write, re-GET and confirm `effective.revision` and `case_concurrency`.
New tickets only.

## Debug map

| Symptom | First look |
|---|---|
| Still serial | Stored revision still 1; or in-flight lease stamp; or scorer predates overlapping `/run` |
| `6600.0s` / lease abort | `issued_at` before overlapping `/run`; leftover exhausted slots |
| `tool_mean` ~0 on v10+ | tool route 409 without a case window — session-scoped provenance on 0.100.3+ |
| `inference broker activation rejected` | grant TTL vs broker session cap (180-min canonical / 4h confirmation / leftover 430-min grants need 8h broker TTL) |
| Chat 413 / allowance exhausted | `chat_request_budget` at 16384 hard max |
| Embedding 429 | Platform 12/64/192 or Perplexity; local semaphore 8 should bind first |
