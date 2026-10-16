# LongMem shadow rollout record — 2026-09-12 → (ongoing from rev 46)

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
| 46 | 09-14 18:55 | shadow, **cap 1→5** (all other knobs and the v8 profile unchanged) | owner request: start using LongMem confirmation — steady-state shadow at 5 attempts/day |

## Shadow resume (rev 46, 2026-09-14)

Owner directed resuming shadow mode and issuing 5 bundles/day ("LongMem is an
important component, start using it"). Applied 18:55Z with confirmation phrase
`APPLY V9 CONFIRMATION MODE SHADOW`; `issuance_active: true` and the v8 profile
`8e5be01e…` confirmed installed via `get_confirmation_lane_diagnosis` in the same
minute. The dollar cap stays $1000/day (nonbinding: settled cost on canary 3 was
$0.142/bundle; the platform's epoch projection of ~$137/day at cap 5 is a
worst-case reservation, not expected spend).

Issuance is automatic under the cap via Platform reconciliation, not five
discrete manual tickets: the rev-46 write immediately reconciled the *current*
rank-1 king — which had moved to `aceron_v17` since canary 3 — and minted bundle
`4a5916b5` (ticket `62e72160`, leased to validator `5Cg3DiRf…`, slot `longmem-0`,
deadline 22:55Z). Day-1 budget showed 2 of 5 attempts used within seconds
(canary 3's completion + this lease). The pending operator retest `1a2555cf`
(aceron, gen 2) remains admissible under the standing cap. Live scoring is
shadow-only and unchanged by design: shadow cannot alter base scores, ranking,
emissions or sanctions.

## Canary chain

| # | Bundle | Subject / artifact | Validator | Stack | Outcome |
| --- | --- | --- | --- | --- | --- |
| 1 | `85447c2e` (initial) | aceron_v16 `9538de72` / `06db691f` | 5CFtzzb4 (Rizzo) | 0.258.0 | completed/unqualified: 48× received-failure zero; reader 0, judge 0, embedding 12,170 |
| 2 | `bf9f5bfe` (retest gen 1) | same | 5CFtzzb4 | 0.258.2 (#1804 diagnostics) | completed/unqualified: `received_failure_kinds/http_status_503=48`, `reader_attempts=196`, reader grants 0 |
| 3 | `ff9c80c6` (initial, new king) | lets_623 `c25489aa` / `58deab6e` | 5HEouuDa (ditto-validator-prod) | 0.259.0 (#1806 clamp) | **completed/qualified**: 34/48 (0.708 ± 0.063); reader 96/96 receipted, judge 48/48; `full_quality` 0.786028 |
| — | `1a2555cf` (retest gen 2 of `bf9f5bfe`) | aceron_v16 | — | — | pending, unissued at rev 45; admissible under the rev-46 standing cap (5/day) |

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
