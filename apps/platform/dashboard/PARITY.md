# Parity: served-HTML assertions to vitest

The pre-SPA dashboard was one `index.html`, and two Python suites were the only
executable record of what it is supposed to show. `ditto/tests/api_server/test_dashboard.py`
asserted on the served markup, copy, and inline script — 52 tests, ~837
assertions. The second suite, `ditto/tests/api_server/test_dashboard_slots.py`,
lifted the five slot fan-out functions out of `index.html` with regexes and ran
them under node against fixture heartbeats — 24 tests, because slot fan-out is
logic rather than markup and a served substring cannot tell "renders two jobs"
from "renders one job twice". None of the 76 was dropped in the port: the 11
serving-contract tests stayed in Python (wandb injection, cache headers, 304s,
`/assets/`, the disabled and missing-build fallbacks) and the 65 appearance and
behavior tests moved here, translated by kind.

| kind | becomes |
| --- | --- |
| markup (`class=`/`id=`/`data-*`) | jsdom render + DOM queries |
| copy and rendered values | rendered-text queries |
| inline-script source text | pure functions in `src/lib/` with behavior tests |
| negative (`... not in body`) | DOM-absence checks, or `src/build-invariants.test.ts` when the guard is "this literal must never ship" |
| CSS source text | assertions on the page stylesheet |
| endpoint paths | `src/lib/api.test.ts` |

Each ported test keeps the original docstring reasoning as a comment, because
those docstrings say *which regression* the assertion exists for. Keep this
table honest: if you move or retire one of these, edit the row.

## Appearance tests

| # | old test (`ditto/tests/api_server/test_dashboard.py`) | guards | now lives in |
| --- | --- | --- | --- |
| 1 | `test_includes_submission_pipeline` | No docstring; mega-spec of the merged Overview: family-ranked leaderboard, artifact release/download, chain observation, operations pipeline board, paged activity table + detail modal, accepted/confirmation scores, quarantine states, screening-dispute form. Inline comments guard two removals: the per-sample dot plot ("was a debugging view; the row now carries the seed-round count") and the rank-1 "Up next" badge ("Rank 1 alone must never earn the badge: a gated row can hold the head of the list while no validator is able to lease it"). No issue # cited. | src/pages/Overview.test.tsx (endpoint paths → src/lib/api.test.ts) |
| 2 | `test_includes_off_network_harness_memory_comparison` | No docstring; comments guard that the memory timeline now leads the overview (no longer at the bottom of the benchmark page), that the harness filter and per-harness hardcoded evidence links were dropped, and that the kicker above the title was removed. Hardcoded third-party evidence (run IDs, means, models, seed) is pinned verbatim. No issue # cited. | src/pages/Overview.test.tsx (endpoint path → src/lib/api.test.ts) |
| 3 | `test_overview_shows_the_full_board_without_a_disclosure` | "Standings are never hidden behind a click" — ditto-platform#383 collapsed the nine-column table behind a `<details>`; that stays banned. Overview is a two-pane layout with a compact board; the full column set lives on a dedicated Leaderboard page ("compactness through a second surface, not through disclosure"). | src/pages/Overview.test.tsx + src/pages/Leaderboard.test.tsx (ABSENT greps → src/build-invariants.test.ts) |
| 4 | `test_leaderboard_carries_a_filter_over_name_uid_and_hotkey` | "The one capability worth keeping from #383's rail" — in-place table filter (vs. the shell's global search which jumps to a single record); must reset paging and live on the board toolbar, not a sidebar. | src/pages/Leaderboard.test.tsx |
| 5 | `test_memory_timeline_plots_the_field_and_crowns_the_champion` | No docstring; comments pin: field comes from the existing per-version leaderboard; settled contracts are immutable so only the newest board refetches; champion plate is the point of the chart and its label lives in a reserved gutter; contracts are equal bands not wall-clock; measured viewBox for phone type; reveal animation must enhance an already-visible default. | src/pages/Overview.test.tsx (endpoint path → src/lib/api.test.ts) |
| 6 | `test_memory_timeline_names_the_gaps_instead_of_implying_data` | "A contract can outrun both the reference runs and its own rollout. Neither may be papered over" — a reference line that just stops reads as a baseline collapsing to zero, and an open-rollout band drawn like a settled one implies an immovable rank. Both states are derived from data (harness records + `/public/bench/rollout`) so later contracts inherit the treatment. No issue # cited. | src/pages/Overview.test.tsx |
| 7 | `test_memory_timeline_window_is_not_pinned_to_a_bench_version` | No docstring; comment: bands are whatever the timeline endpoint returns, windowed by count — "No version literal decides what is drawn." Guards against re-hardcoding bench versions into the chart. | src/build-invariants.test.ts (version-literal negative greps; window logic → src/pages/Overview.test.tsx) |
| 8 | `test_api_failures_do_not_render_sample_data` | No docstring; guards that API failure renders explicit unavailable states, never demo/sample data. No issue # cited. | src/pages/Overview.test.tsx (`SAMPLE` negative greps → src/build-invariants.test.ts) |
| 9 | `test_includes_public_miner_facing_ath_review_queue` | No docstring; specs the public `#/reviews` page: ATH-review explainer copy, live list with cached-snapshot fallback, fan-out pagination over the public activity endpoint — and that nothing admin/auth leaks onto the public page. No issue # cited. | src/pages/Reviews.test.tsx (endpoint path → src/lib/api.test.ts) |
| 10 | `test_includes_server_backed_submission_quick_filters` | No docstring; specs server-side quick filters on the submissions table (status buttons build the query server-side; paging resets; every status has a label class), plus the below-score-floor and operator-review explanations. No issue # cited. | src/pages/Submissions.test.tsx |
| 11 | `test_score_floor_message_attributes_the_number_it_quotes` | "The low-priority explanation has to be falsifiable from public data." Old copy said "below the current fifth-place score of 0.886" — uncheckable because the floor was 5th-highest `composite` while the rank column ordered by `official_composite`, so displayed rank 5 was routinely a different agent/number; this disagreement generated a support report. Both surfaces now cut with the one canonical ordering (`ditto.score_order`) on `official_composite`; copy must say so and name the floor holder. No issue # cited. | src/pages/Submissions.test.tsx |
| 12 | `test_submission_filters_and_page_restore_and_sanitize_the_url` | No docstring. Guards activity filter/page state living in the hash query, one-time normalization of legacy real-query filters, page-number validation, popstate restore, and URL sanitization. | src/pages/Submissions.test.tsx |
| 13 | `test_submission_filters_are_mobile_and_keyboard_accessible` | No docstring. Guards 44px touch targets, aria-pressed state, and focus outline on activity filter buttons. | src/pages/Submissions.test.tsx |
| 14 | `test_explains_policy_rescreen_from_public_activity_state` | No docstring. Guards the notice explaining policy-version rescreens (screening backlog is intentional, not data loss), derived from public activity state. | src/pages/Submissions.test.tsx |
| 15 | `test_includes_accessible_fleet_status` | No docstring. Guards the fleet health table (headings, screener toggle, offline/retired split) and the removal of privacy-leaking / hardcoded-threshold copy. | src/pages/Operations.test.tsx (endpoint paths → src/lib/api.test.ts) |
| 16 | `test_inoperative_fleet_nodes_fold_into_the_collapsible` | Inline comments: validator last-reports are never pruned, so dead hosts must fold into a collapsible (one offline rule for both fleets); offline window read from snapshot, never restated in copy; folded validators keep badge/drill-down/deep link; closed summary names every fold reason (a ledger count with no visible row is how a broken validator went invisible before). | src/pages/Operations.test.tsx |
| 17 | `test_a_validator_that_cannot_serve_the_scored_bench_is_gated` | Inline comments: obsolete-build validators fold away ("Healthy · Idle" beside the working fleet was a fiction) but a CURRENT validator with a broken scorer stays visible — hiding it would repeat #511; badge precedence Obsolete build > Scorer down > bench gate; bench version comes from the snapshot, never a literal. | src/pages/Operations.test.tsx |
| 18 | `test_operations_panels_share_one_snapshot_and_show_skew` | No docstring. Guards that all operations panels consume exactly one `/public/operations` fetch (no per-panel refetches), that the bench badge never max()-promotes versions, and that platform/heartbeat assignment skew is surfaced. | src/pages/Operations.test.tsx (single-fetch + banned endpoints → src/lib/api.test.ts) |
| 19 | `test_benchmark_authority_state_never_promotes_rollout_target` | No docstring. Extracts `benchmarkAuthorityState` from the page and executes it under node: an in-flight rollout target (desired v7) must never be reported as active — only `activated` stops `rolling`; missing desired falls back to active. | src/lib/bench-state.test.ts |
| 20 | `test_leaderboard_state_separates_active_desired_and_history` | No docstring. Extracts `leaderboardBenchState` and executes it under node: the leaderboard's selected version must stay independent of active/desired (historical view selects 5 while active=6, desired=7), and the rollout target must never overwrite the current bench. | src/lib/bench-state.test.ts (ABSENT grep → src/build-invariants.test.ts) |
| 21 | `test_validator_progress_keeps_superseded_failures_as_history` | Docstring: `validator_tickets` is one mutable row per (agent, version, validator); a reissue preserves `failure_reason`/`failed_at` as audit trail, and the drawer used to let that preserved failure win outright — three scored validators rendered as "Scoring run failed · deferred" stamped with superseded failure times, with three accepted scores above them unexplained. Extracts helpers + `renderValidationAttempt` and executes under node. | src/pages/Submissions.test.tsx |
| 22 | `test_includes_accessible_benchmark_progress` | No docstring. Guards live benchmark/screening progress rendering: stage labels, version chips, rescore state, `<progress>` accessibility, reduced-motion/forced-colors/mobile media queries, and per-second elapsed timers. | src/pages/Operations.test.tsx |
| 23 | `test_includes_public_terminal_screening_review_cards` | No docstring. Guards the public terminal-screening rejection card: findings, source locations, policy observations, digest-verified privacy framing. | src/pages/Submissions.test.tsx |
| 24 | `test_includes_copy_controls_for_operational_identifiers` | No docstring. Guards clipboard copy buttons for hotkeys/IDs/SHA-256 with live-region status, keyboard activation, and execCommand fallback with manual-copy failure copy. | src/components/shell/CopyButton.test.tsx |
| 25 | `test_includes_miner_facing_review_details_copy` | No docstring. Guards the one-click "review packet" text block miners paste when asking for a review (agent id, name/version, hotkey, status, artifact SHA, canonical URL), present in exactly two places. | src/pages/Submissions.test.tsx |
| 26 | `test_validator_names_remain_optional_untrusted_decoration` | No docstring. Guards that validator display names/stake weights are optional decoration from a separate feed (reset on refetch, escaped, hotkey stays the anchor identity), fleet sorted by stake then hotkey, and unavailability flagged rather than fatal. | src/pages/Operations.test.tsx |
| 27 | `test_includes_system_and_time_aware_theme_switcher` | No docstring. Guards the four-mode theme switcher (system/light/dark/time) with localStorage persistence, prefers-color-scheme tracking, time-phase (dawn) logic, and sidebar grid layout. | src/components/shell/ThemeSwitcher.test.tsx |
| 28 | `test_sidebar_shell_routes_every_section` | Inline comment: dashboard is a sidebar shell with hash-routed pages; theme switcher lives in the sidebar; leaderboard has a dedicated page alongside its compact home in the overview. | src/components/shell/Sidebar.test.tsx |
| 29 | `test_advertises_public_source_repositories` | No docstring. Guards the open-source repo links (platform twice, subnet, screener) with accessible labels. | src/components/shell/Sidebar.test.tsx |
| 30 | `test_dashboard_entities_use_query_popovers_and_pages` | Inline comments: entity params live in the hash query (real query carries config knobs only); drilldowns are overlays over the current page; `ENTITY_PAGES` is only the cold-link fallback; legacy real-query and path-style entity links are recognized and normalized. #648 rewrote how an agent link resolves and what it costs; that slice is row 42. | src/lib/entity-links.test.ts (endpoint path → src/lib/api.test.ts; #648's deep-link / deferral / pause slice → row 42) |
| 31 | `test_mobile_sidebar_stays_below_modal` | No docstring. Guards z-index layering: modal (50) above backdrop (40) above sticky sidebar (30) on mobile. | src/components/shell/Sidebar.test.tsx |
| 32 | `test_includes_accessible_global_search` | No docstring. Guards the combobox/listbox global search with keyboard shortcuts (/, cmd-k, arrows, escape) navigating to pages via pushState. | src/components/shell/GlobalSearch.test.tsx |
| 33 | `test_benchmark_badge_communicates_rollout_transition` | No docstring. Guards the DittoBench badge naming the rollout transition instead of a bare "latest" claim. | src/components/shell/BenchBadge.test.tsx (ABSENT grep → src/build-invariants.test.ts) |
| 34 | `test_leaderboard_omits_tie_labels` | No docstring. Guards that the removed tie chip never returns. | src/build-invariants.test.ts |
| 35 | `TestDashboardScoringTransparency.test_no_hardcoded_fold_constants` | Class docstring: consensus parameters (incumbent margin, champion share, tail size, authority threshold, bench version) are API-served and must never be markup literals — a literal is a claim that silently stops being true, and miners read it as the rule they are judged by. | src/build-invariants.test.ts |
| 36 | `TestDashboardScoringTransparency.test_renders_the_dethrone_floor_and_rollout_state` | Class docstring (as #35); guards the "score to beat" as its own element computed from API-served margin (not an inlined formula), published as a floor not a guarantee, plus the rollout/authority strip with API-read threshold. | src/lib/scoring.test.ts (floor math; banned formula → src/build-invariants.test.ts; strip markup → src/pages/Leaderboard.test.tsx; endpoint → src/lib/api.test.ts) |
| 37 | `TestDashboardScoringTransparency.test_explainer_covers_scoring_emissions_and_koth` | Class docstring (as #35); guards the four `<details>` explainers covering scoring, emissions, king-of-the-hill, and version transitions, with active/rollout versions filled from JS variables. | src/pages/Benchmark.test.tsx (formula copy also mirrored in src/lib/scoring.test.ts) |
| 38 | `TestDashboardScoringTransparency.test_composite_detail_separates_quality_and_token_adjustments` | Class docstring (as #35); guards the composite-calculation breakdown separating quality gates from the bounded token-efficiency adjustment. | src/lib/scoring.test.ts |
| 39 | `TestDashboardScoringTransparency.test_benchmark_version_is_never_a_literal` | Class docstring (as #35); static markup carries only a placeholder — the frozen-setup tag and version copy are filled from the API. | src/pages/Benchmark.test.tsx |
| 40 | `TestDashboardScoringTransparency.test_no_reference_baseline_stat` | Docstring: the stock-harness reference baseline is deliberately unpublished — v7 calibration is sharply bimodal (15 of 20 seeds score conversational_sanity exactly 0.000, composite 0.185-0.221; 5 clear the gate at 0.344-0.450; no mass at the mean 0.248, sd 0.087), so any single number describes a run that does not exist; guards against the stat reappearing as a bare composite. | src/build-invariants.test.ts |
| 41 | `TestDashboardScoringTransparency.test_neighbouring_comparison_features_survive` | Docstring: removing the baseline must not take out its neighbours — the off-network third-party harness comparison and the token-efficiency budget are separate measurements that merely live beside the removed card. | src/pages/Benchmark.test.tsx |
| 42 | `test_dashboard_entities_use_query_popovers_and_pages` (the assertions #648 added to row 30) | ditto-platform#648 introduced the targeted `/public/agent/{id}/summary` path and the follow-up keeps that fast first paint without making the reader trigger a request. Summary and pipeline use separate keyed Solid Query rows, start concurrently, cancel on stale routes, and render local loading/error states. The old global activity lookup stays banned, overlay routes show their loading state, detail failures have an explicit retry, and an open agent card still pauses periodic global reads with one hydrate on close. | src/components/EntityPanel.test.tsx (parallel summary/pipeline link, no activity request, overlay loading state, unavailable copy); src/pages/Submissions.test.tsx (automatic evidence loading, cache deduplication, granular skeletons, retry, summary facts during pending detail); src/data/useEndpoint.test.ts (entity-first pause and close hydrate); src/lib/api.test.ts (caller cancellation) |

## Slot fan-out behaviors (`test_dashboard_slots.py`)

The 24 behaviors of the second source suite (#540, 2026-07-29). Five of its
tests were source greps over `index.html`; those became renders of the real
components (`FleetTable` in the operations page, `EntityPanel`'s validator
modal). The rest were pure logic and are now plain imports from
`src/components/operations/fleet.ts`. Six of them were already asserted by
`fleet.test.ts` when it was written from the same issue, so they are mapped
there rather than duplicated, and `slots.test.ts` carries a comment at each
point where it stops short for that reason.

| old test (`ditto/tests/api_server/test_dashboard_slots.py`) | guards | now lives in |
| --- | --- | --- |
| `TestValidatorSlotIds.test_renders_one_row_per_configured_slot` | One slot row per advertised slot. | `src/components/operations/slots.test.ts` › "renders one row per configured slot" |
| `…test_two_concurrent_jobs_are_both_addressable` | "The regression this change exists for": two jobs, two distinct rows — never one row rendered twice. | `slots.test.ts` › "makes two concurrent jobs both addressable" (rendered half: › "never synthesises slot ids from a count, and draws two jobs as two rows") |
| `…test_active_slot_outside_configured_range_is_still_shown` | A job is never dropped because the count looks smaller; synthesising ids from `configured_slots` hid exactly the case an operator most needs to see. | `slots.test.ts` › "still shows an active slot outside the configured range" |
| `…test_assigned_but_not_yet_active_slots_appear` | An assigned-but-not-yet-running slot is part of the union. | `slots.test.ts` › "shows assigned-but-not-yet-active slots" |
| `…test_slots_order_numerically_not_lexically` | Slot order is by ordinal, not string. | `slots.test.ts` › "orders slots numerically, not lexically" (+ › "compares by ordinal, so slot-10 follows slot-9") |
| `…test_single_slot_validator_is_unchanged` | The fan-out did not change the one-slot fleet. | `slots.test.ts` › "leaves a single-slot validator unchanged" |
| `…test_missing_capacity_fields_fall_back_to_one_slot` | An empty payload still renders one slot, never zero. | `slots.test.ts` › "falls back to one slot when the capacity fields are missing" |
| `TestAnyBenchmarkStage.test_stage_on_a_higher_slot_counts_as_progress` | "Keying off slot zero alone suppressed live progress for slot one." | `slots.test.ts` › "counts a stage on a higher slot as progress" |
| `…test_idle_validator_reports_no_granular_progress` | An idle validator keeps its plain worker-state chip. | `slots.test.ts` › "reports no granular progress for an idle validator" |
| `…test_legacy_single_benchmark_still_counts` | The pre-fan-out `active_benchmark` field still counts as progress. | `slots.test.ts` › "still counts a legacy single benchmark" |
| `TestCappedSlots.test_slots_above_the_cap_are_marked` | Eight advertised under a cap of six: the top two are capped, not idle. | `src/components/operations/fleet.test.ts` › "caps the leftover healthy slots from the highest ordinal down" |
| `…test_a_validator_inside_the_cap_has_nothing_capped` | A fleet inside its cap is marked nowhere. | `slots.test.ts` › "caps nothing on a validator inside the cap" |
| `…test_running_work_is_charged_first_and_never_marked` | "Lowering the cap costs new leases only, never one in flight" — and the leftover budget lands on the lowest idle ordinal. | `slots.test.ts` › "charges running work first and never marks it" |
| `…test_leases_beyond_the_cap_do_not_borrow_from_idle_slots` | A cap dropped under live work caps every idle slot, not a negative. | `slots.test.ts` › "never lets leases beyond the cap borrow from idle slots" |
| `…test_an_unhealthy_slot_keeps_its_own_state` | "Unavailable is about the validator; capped is about the operator" — an unhealthy slot is neither marked nor charged. | `slots.test.ts` › "leaves an unhealthy slot its own state" |
| `…test_a_payload_without_the_field_marks_nothing` | "A dashboard served against an older API must not invent a cap." | `fleet.test.ts` › "says nothing when the payload predates allowed_slots" |
| `TestFundedSlotCount.test_the_cap_binds_below_healthy_capacity` | The numerator is `min(allowed, healthy)` when the cap binds. | `fleet.test.ts` › "counts funded slots as min(allowed, healthy) with the full tooltip" |
| `…test_health_binds_below_the_cap` | …and when the validator's own health binds instead. | `slots.test.ts` › "binds on health when health is the smaller number" |
| `…test_without_the_field_it_falls_back_to_healthy_slots` | No cap field: healthy slots are the number. | `slots.test.ts` › "binds on health when health is the smaller number" (second assertion) |
| `TestDashboardSource.test_slot_ids_are_not_synthesised_from_a_count` | The old loop built ids as `"slot-" + slotIndex` and dropped the rest. | `slots.test.ts` › "never synthesises slot ids from a count, and draws two jobs as two rows" |
| `…test_per_slot_rows_have_their_own_style_hook` | `.fleet-slot` per slot plus the `+ .fleet-slot` separator rule, so stacked slots read as lines instead of the table growing a column per slot. | `slots.test.ts` › "gives every slot its own row and its own style hook" |
| `…test_a_capped_slot_is_rendered_as_its_own_state` | Not "Idle" (claims usable capacity) and not "Unavailable" (a fault); the tooltip names the cap from the snapshot. | `slots.test.ts` › "renders a capped slot as its own state — not Idle, not Unavailable" |
| `…test_the_fleet_row_reports_funded_capacity` | "The numerator is what dispatch funds, not what is advertised" — and never the old healthy-of-advertised phrasing. | `slots.test.ts` › "reports funded capacity in the row, not advertised or healthy" |
| `…test_detail_modal_renders_every_active_slot` | The modal used to show only the lowest slot, so a second concurrent benchmark was invisible there. | `slots.test.ts` › "renders every active slot, in slot order" |

Two behaviors gained coverage they never had, because the Python suite only
reached `slotOrdinal` and `anyBenchmarkStage` indirectly: an id with no ordinal
(a missing `slot_id`, an unknown naming scheme) sorts last rather than claiming
slot zero or dropping out of the union, and a leased slot that has not reported
a stage yet is not progress. Porting the modal guard also found that
`EntityPanel`'s validator body had the `Slots` summary but not the per-slot rows
under it — that regression was live until it was fixed alongside these tests.

## Notes on the translation

- **Formula guards became unit tests.** The old suite asserted on script text —
  `"var floor = champComposite + effectiveMargin" in body`, and the negative
  `"champComposite * (1 + margin)" not in body`. Those formulas now live in
  `src/lib/scoring.ts` and `src/lib/bench-state.ts`, and the tests assert the
  math: an additive dethrone margin, band decay, quorum coercions, the
  chain-weight fold. `build-invariants.test.ts` still bans the multiplicative
  shape from the shipped bundle, so both halves of the original guard survive.
- **"Never ships" guards run against `dist/`.** The hardcoded fold constants
  (`2% protection margin`, `receives 90% of the miner pool`, and the rest), the
  banned formula shape, literal bench versions in explainer copy, and the
  retired reference-baseline stat are asserted absent from a real build rather
  than from source — which is the property the original greps actually had.
- **Goldens, during the port only.** The monolith was rendered in jsdom against
  `fixtures/` with a frozen clock, and each ported page was diffed against that
  DOM until it matched. The goldens were a porting gate, not a permanent CI
  artifact; `fixtures/` is what remains.

### Accepted differences from the monolith DOM

Everything below diffs non-zero against the goldens and is deliberate.

- **Whitespace between block elements.** JSX emits none; the monolith template
  strings did. Text content is identical.
- **Attribute order within a tag.** Solid emits static attributes before
  reactive ones.
- **`data-sig` on `#leaderboard-version-pills`.** Bookkeeping for the
  monolith innerHTML rebuild, which fine-grained reactivity replaces.
- **`aria-describedby` tooltip numbering.** Per-element description spans
  instead of one document-wide rescan; same mechanism, same text.
- **Section mounting.** The monolith kept all six page sections in the DOM and
  hid five with CSS; the SPA mounts the routed one. Visible output matches.
  One consequence worth knowing: the site footer lives inside the benchmark
  section, so it renders on that page only — as it displayed before.
- **Boot reads on a cold agent deep link (#648).** The monolith had one
  `load()`, so the entity-first pause skipped its boot seed too: opening
  `/agent/{id}` cold cost exactly one request until the card was closed. In the
  SPA each page and store hydrates on mount, so a cold agent link still seeds
  the shell's endpoints once; only the *periodic* reads are paused
  (`agentCardOpen`, `data/useEndpoint.ts`). Suppressing the initial reads too
  would leave every surface behind the card in its connecting state and refetch
  the lot on close — a flash the monolith never had, because it kept its
  last-rendered DOM and merely stopped refreshing it.

## Known gaps: the miner modal body

The drilldown modal escaped both gates — page goldens never open it, and the
old Python suites barely asserted it. An audit against every interaction-only
render path in the monolith closed the validator modal (Capabilities, Stack
identity, Component health, Assignment, evicted leases, the legacy
single-benchmark fallback) and the miner modal's consensus block. These remain,
ordered by user-visible impact, with the monolith line ranges to port from:

| what | monolith | target |
| --- | --- | --- |
| "Benchmark run" stat group (KOTH emissions, revealed support, validation, registration, cases scored, ranking, median latency, benchmark version, harness model, token spend, model-use rows) | 5934–5955, 6020; `modelUseStats` 6083–6108 | `EntityPanel.tsx` `MinerSummary` (`evidence/model-use.ts` already exists) |
| Per-category breakdown grid (~40 cells, each `title=` the category purpose) | 5959–5968, 6032 | `MinerSummary`; CSS already at `styles/widgets.css` |
| Composite trend group (240x58 sparkline, axis bounds, run count, change row) | `trendBlock` 5763–5781, `sparkSvg` 5742–5761, 6022 | `MinerSummary`; only the 62x16 row sparkline was ported |
| Benchmark integrity group (paraphrased cases, lexical-gap rewrites, capped tool cases, memory seeding waves) | 5971–5980, 6021 | `MinerSummary`; `IntegrityTelemetry` type already declared |
| Quality factor detail rows (per-gate multiplier + observed + audit pairs, each label a tooltip carrying the gate's `explanation`) | `compositeCalculationBlock` 5878–5888 | `lib/scoring.ts` `compositeCalculationRows`; `quality_factors` is served but undeclared on `CompositeBreakdown` |
| Per-question results for the miner tenant | `casesSection` 6369–6398, 6033 | `MinerSummary`; `evidence/Cases.tsx` exists, wired to the agent drawer only |
| Submitted-source release card + download for the miner tenant | 3642–3704, 6028 | `MinerSummary`; `evidence/ArtifactRelease.tsx` exists |
| "Copy review details" for the miner tenant | 6131–6157, 6027 | `MinerSummary`; `evidence/review-packet.ts` exists |
| Dataset SHA-256 copy row | `shaRow` 6023–6025, 6034 | `MinerSummary`; `.sha-row` CSS has no emitter |
| Overview group is thinner: no initial quorum median, calibration (Brier), transform robustness; the leaderboard-score row drops its retained-sample count | 5988–6016 | `MinerSummary` |
| `COMPOSITE_CALC_NOTE` is the superseded copy | 5904 | `lib/scoring.ts` |
| Case category labels/purposes are glossary-only; the monolith merges the glossary over a 41-entry inline table, so 18 recorded categories fall back to raw slugs and a generic tooltip (`preference` is the one with a real inline label) | `CATEGORY_LABEL` 6218–6262, `CATEGORY_PURPOSE` 6270–6320 | `evidence/Cases.tsx` — affects the agent drawer too |
| Global search corpus omits `review_event` and `review_original_reason` | 6644 | `shell/GlobalSearch.tsx` |
| Search field desync: pages poke `searchInput.value` directly, so the signal never updates — on a `?q=` deep link the Clear control and popover state are wrong | 6741, 10225–10226, 6591–6599 | `shell/GlobalSearch.tsx` needs a value prop |
| Version-archive menu is not clamped to its scrolling ancestor, so on the overview split it can lose its left edge | `clampVersionArchiveMenu` 5504–5541, 5494 | `board/LeaderboardBlock.tsx` |

Confirmed fully ported, for the record: the `[data-tooltip]` system, timeline
tooltips, the search overlay's results/empty/keyboard paths, the modal shell
chrome (focus trap, inert, Escape, full-page role swap, `#copy-status`), every
disclosure widget, copy feedback, and the whole agent-submission drawer.
