# LongMem shadow rollout record — 2026-09-12 → 2026-09-14

Compact, checked-in evidence for the bounded shadow rollout that produced the first
accepted LongMem confirmation since issuance was stopped on 2026-08-27 (rev 39).
Everything here is shadow: no base score, ranking, emission or sanction changed.

## Policy revisions (Backroom `set_confirmation_bundle_settings`, scope `*`)

| Rev | When (UTC) | Change | Why |
| --- | --- | --- | --- |
| 40 | 09-12 19:59 | shadow, rank/top_n 1, challenger_z 0, cap 1, profile **v8** `8e5be01e…` | rev 39 pinned v7 `23e7168e…`, which the release no longer installs — it could never issue |
| 41 | 09-12 20:29 | off | canary 1 terminal |
| 42 | 09-13 00:05 | shadow (same knobs) | canary 2 window |
| 43 | 09-13 00:39 | off | canary 2 terminal |
| 44 | 09-14 01:19 | shadow (same knobs) | canary 3 window |
| 45 | 09-14 01:52 | off | canary 3 terminal (accepted) |

## Canary chain

| # | Bundle | Subject / artifact | Validator | Stack | Outcome |
| --- | --- | --- | --- | --- | --- |
| 1 | `85447c2e` (initial) | aceron_v16 `9538de72` / `06db691f` | 5CFtzzb4 (Rizzo) | 0.258.0 | completed/unqualified: 48× received-failure zero; reader 0, judge 0, embedding 12,170 |
| 2 | `bf9f5bfe` (retest gen 1) | same | 5CFtzzb4 | 0.258.2 (#1804 diagnostics) | completed/unqualified: `received_failure_kinds/http_status_503=48`, `reader_attempts=196`, reader grants 0 |
| 3 | `ff9c80c6` (initial, new king) | lets_623 `c25489aa` / `58deab6e` | 5HEouuDa (ditto-validator-prod) | 0.259.0 (#1806 clamp) | **completed/qualified**: 34/48 (0.708 ± 0.063); reader 96/96 receipted, judge 48/48; `full_quality` 0.786028 |
| — | `1a2555cf` (retest gen 2 of `bf9f5bfe`) | aceron_v16 | — | — | pending, unissued (cap spent by canary 3); needs an explicitly authorized window |

Canary 3 evidence: reporter `5HEouuDa`, `bundle_signature 54648515…0b089`, Platform
`verified_at 2026-09-14T01:50:19Z`, `evidence_sha256 ef7457f8…611f`, settings rev 44,
profile v8; reader `openai/gpt-oss-20b` 1,400,615 prompt + 18,863 completion tokens
($0.1107, receipt set `f05324e1…`); judge `openai/gpt-4o-2024-08-06` (Azure) 48/48
($0.0311); total $0.1418, latency 1,638 s; per capability extraction 7/8,
multi-session 7/8, temporal 4/8, knowledge update 6/8, preference 3/8, abstention 7/8;
ablations shadow-only (inference `observational_drop_not_causal`, embedding
`delta_below_threshold`, synthetic, applied factor 10000). Leaderboard and emissions at
01:51Z were identical to the 01:20Z baseline. Open question (nonblocking): the grant
ledger shows reader 112 requests for ticket `5841aa0b` against 96 receipted in the
evidence.

## Repairs (all merged to `main`, all with independent review at the exact head)

| PR | Release | What |
| --- | --- | --- |
| #1803 | Platform v0.258.1 `f75336e0` | `profile_installed` / `installed_profiles` on `get_confirmation_bundle_settings`; lane diagnosis cause `profile_not_installed`; Backroom control panel notice |
| #1804 | stack v0.258.2 `061dd20b` | scorer `longmem_diagnostics` (received-failure kinds, reader attempts, embedding dispatches) → validator W&B `confirmation/longmem_*`; redacted harness log tail on the validator |
| #1806 | stack v0.258.3 (`c7e311a9`) | `rewriteReaderRequest` clamps an explicit `max_tokens` over-ask to the frozen per-request bound instead of refusing; `received_failure_reader_agent_rejections` diagnostic |

Root cause of canaries 1–2: the reader lane's frozen bound is
`2,304,000 / 1,152 = 2,000` completion tokens per request; the aceron artifact pins
`max_tokens` 4096 (`src/single_tool_model.rs`), so every reader call was a
pre-reservation 400 and the harness answered `/run` with 503. The same signature had
hit aceron_v17 and unione on 2026-08-27.

## Where the working files live

Task workspace `workspaces/longmem-shadow-rollout-20260912-155804-6b5954/` in the
heyditto-stack checkout that ran the rollout: `evidence/canary-1.md` (full running
log), `repro/` (exact artifact `agent.tar.gz` sha `06db691f…`, `mock_broker.py`,
`run_real_case.sh`, `project_case.py`, `longmemeval_s_cleaned.json` sha
`d6f21ea9…`, harness crate at `ba5b463a`). Nothing in `repro/` is needed to reproduce
the accepted result; it documents the diagnosis.

## bench_version 13 failure diagnosis — 2026-10-21/22

Live state read through Backroom MCP at 2026-10-22T01:0xZ. `bench_version` is now
13; everything above was recorded against the v8/v9 profiles, so treat the
canary numbers as history, not as current contract.

| Evidence | Value |
| --- | --- |
| Lane outcome split (v13 lifetime) | **63 failed / 13 completed / 3 superseded** — 83% of bundles lost |
| Recent sample | 32 failed / 13 completed, all `confirmation_execution_failed` at `running_confirmation`, median 134 s |
| Validator fleet | 4 validators at 0.380.1, `fresh_verified`, 15 base benchmarks concurrent |
| Budget at read time | 0 attempts today, $0 settled |

The fleet is healthy and base benchmarks run normally, so the failure is
confirmation-lane-specific.

### Why a bundle dies

A bundle makes roughly 96 reader plus 48 judge serial provider calls. The frozen
profiles set `allow_fallbacks: false`, so **any single call failure ends the whole
48-case run**. Failure compounds over that chain: the observed reader
`provider_timeout` rate alone (4 of 39,608 chat calls in one hour, ~1.0e-4) costs
~1.4% of bundles, and a judge 429 rate of ~2.5% per call gives
`(1-0.025)^48 ≈ 0.30` — which is the order of the measured loss.

Two distinct terminal mechanisms, both confirmed from relay traces:

| Bundle | Lane | Shape | Terminal code |
| --- | --- | --- | --- |
| `05e1ebab` 10-21 21:49 | judge | 429 after **1** judge call, 177 ms | `confirmation_confirmation_provider_unavailable` |
| `6a997cc2` 10-21 19:47 | reader | `latency_ms 90015`, `timed_out` | `confirmation_internal_server_error` |
| `f141d7cb` 10-19 17:25 | judge | 429 after 27 judge calls, 328 ms | `confirmation_confirmation_provider_unavailable` |
| `53486919` 10-19 17:33 | judge-shaped | 298 ms fast-fail | same signature |

The reader 90 s death is `DITTO_INFERENCE_TIMEOUT_SECONDS=90` expiring, not an
unexplained hang. A timeout may already have been billed, so it is correctly
non-retryable under the receipt-free accounting boundary.

The judge is pinned by `longmemeval-official-gpt4o-azure-zdr-v2` to
`openai/gpt-4o-2024-08-06` with `only: ["azure"]`, `allow_fallbacks: false`,
`zdr: true`. Its 429 carries `limit_source: upstream_provider_shared_pool`,
`provider_error_code: rate_limit_exceeded`, `Retry-After: 30`, and
`endpoints: {total: 2, available: [{Azure, selected: false}]}` — the one
permitted endpoint exists and was refused for capacity. There is nowhere to
route.

**This is not the September OpenRouter story.** `longmem-openrouter-429-kills-bundles`
attributed failures to the *reader* route (`openai/gpt-oss-20b`); at v13 the
reader runs on Amazon Bedrock and succeeds, and the dominant terminal failures
are judge-lane. That memory's diagnostic rule (read the last failed record's
`upstream.phases[0].status`) still holds; its conclusion does not.

### Three code findings

1. **The confirmation lane never retried anything.** `postProviderWithRetryPolicy`
   opened with `_ = policy; _ = sleep` and made exactly one `postOnce` call, with
   `result.attempts = 1` hardcoded. Every policy field the confirmation handler
   computed — including its reader/judge branch — was discarded. This was
   deliberate and tested ("A failed scored request is parked for an operator
   retry"), not an accident.
2. **The receipt-free classifier no longer matched the gateway envelope.**
   `providerFailureIsReceiptFree` rejected the live 429 on four independent
   gates: an added `user_id` sibling, an added `error.metadata`, four added
   `openrouter_metadata` fields, and `attempt: 1` where it demanded `0`. It also
   required `endpoints.total == len(available)`, which is false for any
   provider-pinned profile, and compared provider identity against a grant
   `route_provider` that is a sort strategy (`throughput`) for the reader. Every
   test fixture was hand-authored with `attempt: 0`, `total: 1`, so CI could not
   see it. Because the classifier fails closed with no log or metric, this
   silently disabled the ordinary chat path's retry **and** left the provider
   circuit breaker unable to open — so Platform kept issuing bundles into a
   rate-limited provider at ~$14 of settled reservation each.
3. **`confirmationReaderBackpressureDelay` was dead code with passing tests.**
   Carefully written (handles `ErrRange`, non-ASCII digits, duplicate headers),
   asserted by two tests, and called from no production path. Its existence is
   why the September investigation read as "retry needs an owner decision"
   rather than "retry does not exist".

### What landed, and what is still inert

The envelope fix is unconditional: it restores the ordinary chat retry and the
circuit breaker to their own stated intent, and pins the captured production
envelope in `provider_envelope_test.go` so the next drift fails a test.

Confirmation-lane retry is **merged inactive** behind
`DITTO_CONFIRMATION_BACKPRESSURE_RETRY` (default `false`). It is a gate rather
than a fix because re-dispatching a frozen scored request changes what the
shadow evidence attests, even though the receipt-free proof guarantees the retry
cannot duplicate provider work or billing. With the gate off the lane stays
single-shot and every existing contract test passes unchanged.

Activation, when the owner decides: set the env var on the relay, confirm
`receipt_free` and `retry_activated` appear in the lane journal's
`confirmation provider rejected request` warning, then re-check
`get_confirmation_lane_diagnosis` after one bundle. The 404 "no allowed
providers" classifier keeps `attempt == 0`: no live 404 envelope has been
captured, so relaxing it would be inference from the 429 shape rather than
evidence.

### Backroom visibility gap (unfixed)

`get_inference_failure_taxonomy` reported `rate_limited_failures: 0` and
`rate_limit_bursts.active: false` in every window while the lane was losing 83%
of bundles to 429s. Its windows cap at 3600 s and it carries no judge-model
group, so a lane that runs a handful of bundles a day is structurally invisible
to the tool built to surface exactly this. Per the repository rule to prefer
Backroom visibility for production knobs, that read wants a confirmation-lane
dimension; it is not addressed here.
