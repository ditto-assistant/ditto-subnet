# Dashboard SPA Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the monolithic `dashboard/index.html` with a Vite + SolidJS + TypeScript SPA at full appearance/behavior parity with today's dashboard, on branch `e/dashboard-spa`, as one draft PR into `main`.

**Architecture:** Harvest scaffold/toolchain/serving-path/module-layout from `origin/e/dashboard-ux` (PR #356); re-derive all page markup, logic, and CSS from `origin/main`'s `dashboard/index.html` (9,890 lines: CSS 94–2530, markup 2531–3038, inline JS 3039–9888). Parity is enforced by porting all ~57 appearance tests (~837 asserts) from main's `ditto/tests/api_server/test_dashboard.py` into vitest, plus per-page DOM goldens rendered from the monolith with fixture data.

**Tech Stack:** SolidJS 1.9.x, Vite (plain, pinned), Vitest, oxlint, TypeScript 5.9.x, jsdom; FastAPI serving `dashboard/dist`; **no Vite+ / `vp` / curl-piped installers**.

**Spec:** `docs/superpowers/specs/2026-07-31-dashboard-spa-refactor-design.md` — read it first; its Decisions section is settled, do not relitigate.

## Global Constraints

- Branch `e/dashboard-spa`; commit per task; Conventional Commits, terse, why over what; **never mention AI/Claude/codegen in any commit, PR, or comment; no Co-Authored-By/Generated-with trailers.**
- Do not modify: `alembic/`, `ditto/db/`, any `/api/v1` endpoint behavior, `.github/workflows/ci.yml` beyond adding the dashboard job, `scripts/update.sh` beyond the single build-step insertion (Task 9).
- All pins exact: no `latest`, no unpinned GitHub actions (use a full commit SHA), no network installers in CI.
- Reference monolith is **`origin/main:dashboard/index.html`** — never #356's components — for any markup/copy/logic/CSS question.
- Untracked `/root/work/ux/ditto-platform/{.impeccable/,DESIGN.md,PRODUCT.md}` are not yours: never `git add -A`; stage explicit paths only.
- Working dir `/root/work/ux/ditto-platform`. Python checks: `make lint lint-copy typecheck test`. `uv run pytest` needs no env.
- When a monolith behavior and an old Python test disagree, the monolith on main wins (tests may predate a merged change); note it in `dashboard/PARITY.md`.

---

### Task 1: Extract reference materials

**Files:**
- Create: `dashboard-refactor-notes/monolith-map.md` (working notes, **gitignored** — add `dashboard-refactor-notes/` to repo-root `.gitignore` in this task)
- Create: `dashboard-refactor-notes/assert-inventory.md`

**Interfaces:**
- Produces: `monolith-map.md` — per page (`overview`, `leaderboard`, `benchmark`, `operations`, `submissions`, `reviews`): monolith line ranges for its markup, the JS functions that drive it (name + line), the CSS selectors it owns, the `/public/*` endpoints it reads, shared widgets used. Also a shared-infrastructure section (router/hash handling, fetch/poll loop, theme bootstrap, global search, entity popovers, copy controls, formatters).
- Produces: `assert-inventory.md` — every test in main's `ditto/tests/api_server/test_dashboard.py` classes `TestDashboard`/`TestDashboardScoringTransparency` **except** the 12 serving-contract tests (etag/gzip/304/disabled/social-preview/api-mounted), as a table: `old test name | docstring gist | page(s) | assert class(es) (markup/copy/js-source/negative/css/endpoint) | target vitest file`.

- [ ] **Step 1:** `git show origin/main:dashboard/index.html > dashboard-refactor-notes/monolith.html` and `git show origin/main:ditto/tests/api_server/test_dashboard.py > dashboard-refactor-notes/old-tests.py`; add `dashboard-refactor-notes/` to `.gitignore`.
- [ ] **Step 2:** Read `monolith.html` fully (fan out readers by section if using workflows) and write `monolith-map.md` per the Produces contract. Every one of the 6 `data-page` sections and every `id=` referenced from JS must appear in exactly one page's inventory or the shared section.
- [ ] **Step 3:** Write `assert-inventory.md` covering **every** remaining test (expect ~45–50 rows). Cross-check: `grep -c "async def test" old-tests.py` minus serving tests must equal the row count.
- [ ] **Step 4:** Commit (the `.gitignore` line only): `chore: ignore dashboard refactor scratch notes`

### Task 2: Scaffold — toolchain, config, module skeleton

**Files:**
- Create: `dashboard/package.json`, `dashboard/package-lock.json`, `dashboard/vite.config.ts`, `dashboard/vitest.config.ts`, `dashboard/.oxlintrc.json`, `dashboard/tsconfig.json`, `dashboard/.gitignore`, `dashboard/index.html` (SPA shell — **this replaces the monolith; the monolith copy lives in `dashboard-refactor-notes/monolith.html` from Task 1**), `dashboard/src/main.tsx`, `dashboard/src/App.tsx` (route switch rendering placeholder pages), `dashboard/src/test-setup.ts`
- Create: `dashboard/src/lib/{router,router.test,config,api,api.test,format,format.test,copy}.ts`, `dashboard/src/stores/routeStore.ts` + test, `dashboard/src/types/` (from #356, updated), `dashboard/src/data/useEndpoint.ts`
- Delete: `dashboard/assets/paperditto-512.png` → move to `dashboard/public/assets/paperditto-512.png`; keep `dashboard/README.md` (rewrite in Task 10)

**Interfaces:**
- Consumes: `git show origin/e/dashboard-ux:<path>` for every harvested file.
- Produces: `npm run dev|build|preview|test|lint|format:check|typecheck|check` all defined; `check` = typecheck + lint + format:check. Router exposes pages `overview|leaderboard|benchmark|operations|submissions|reviews` + entity paths; `lib/config.ts` reads `<meta name="ditto:api-base">`/`ditto:wandb-url`; `data/useEndpoint.ts` polls with the same cadence/backoff the monolith uses (see monolith-map shared section).

- [ ] **Step 1:** Harvest each file from `origin/e/dashboard-ux`, applying spec deltas: `package.json` devDeps = pinned exact `vite`, `vitest`, `oxlint`, `vite-plugin-solid`, `typescript`, `jsdom`, `@solidjs/testing-library`, `@testing-library/jest-dom`, `solid-js` dep pinned — **no `vite-plus`, no npm alias/overrides**. Pick current-latest stable versions with `npm view <pkg> version`, write exact versions.
- [ ] **Step 2:** Split #356's `vite.config.ts`: build/proxy/plugins in `vite.config.ts` (keep `DITTO_DASHBOARD_PROXY_TARGET` proxy override + comment), test block into `vitest.config.ts`, lint rules into `.oxlintrc.json` (drop `vite-plus/*` rules).
- [ ] **Step 3:** SPA `index.html`: `<meta name="ditto:api-base" content="" />`, `<meta name="ditto:wandb-url" ...>`, social-preview meta tags **copied from the monolith head** (old Python test `test_includes_social_preview_metadata` stays green), inline theme-bootstrap script (port from monolith head, lines ~49–92), `<div id="root">`, module script.
- [ ] **Step 4:** Harvest router/routeStore/format/api + tests; extend route table with `leaderboard`; run `npm ci && npm run check && npm test` → all green, placeholder App renders.
- [ ] **Step 5:** `npm run build` succeeds; `git add` explicit paths; commit `feat(dashboard): scaffold SolidJS SPA toolchain`.

### Task 3: Serving path — factory + Python serving tests

**Files:**
- Modify: `ditto/api_server/factory.py` (dashboard-serving section only: `_DASHBOARD_FILE`/`_render_dashboard`/root+entity routes → serve `dashboard/dist/index.html`, add `/assets/{path}` route)
- Rewrite: `ditto/tests/api_server/test_dashboard.py` → #356's 19 serving-contract tests + `fake_dist` fixtures (`git show origin/e/dashboard-ux:ditto/tests/api_server/test_dashboard.py` is the base; keep its module docstring rationale)

**Interfaces:**
- Consumes: #356 factory delta ONLY for dashboard serving — diff with `git diff origin/main origin/e/dashboard-ux -- ditto/api_server/factory.py` and take nothing that deletes an import/router that exists on main.
- Produces: `/` and entity paths serve injected dist HTML (ETag/304/gzip semantics preserved); `/assets/<hashed>` → `immutable, max-age=31536000`, unhashed → `max-age=86400`; traversal → 404; missing dist → API-only + log line.

- [ ] **Step 1:** Write the new `test_dashboard.py` (port #356's file; adjust `make_api_server_config` usage to today's conftest). Run `uv run pytest -q ditto/tests/api_server/test_dashboard.py` → fails (factory still monolith-serving).
- [ ] **Step 2:** Apply the factory delta. Run same command → all pass.
- [ ] **Step 3:** `make lint typecheck` green on touched files; commit `feat(api): serve dashboard SPA build output`.

### Task 4: Fixtures + golden harness

**Files:**
- Create: `dashboard/fixtures/*.json` (one per `/public/*` endpoint the monolith calls, per monolith-map), `dashboard/fixtures/README.md` (capture date + exact curl per file)
- Create: `dashboard-refactor-notes/golden/{render-monolith.mjs,goldens/<page>.html}` (gitignored)
- Create: `dashboard/src/test-fixtures.ts` (typed fixture loader for vitest)

**Interfaces:**
- Produces: `renderPageGolden(page)` procedure: jsdom loads `monolith.html` with `fetch` stubbed to fixtures, hash-routes to the page, waits for idle, serializes `document.querySelector('[data-page="<page>"]')` (plus shell once) with volatile bits normalized (timestamps, relative ages). SPA golden diffing in Task 6–8 reuses the same normalizer.
- Fixture endpoints: enumerate from monolith-map (the monolith builds URLs as `API_BASE + path`; grep `"/public/` in `monolith.html`).

- [ ] **Step 1:** Capture fixtures: `curl -fsS https://platform-api-dev.heyditto.ai/api/v1/public/<path>` per endpoint (fall back to `https://platform-api.heyditto.ai` for any dev 404/empty). Sanity: every fixture parses as JSON and is non-empty where prod UI shows data.
- [ ] **Step 2:** Build `render-monolith.mjs` (node + jsdom, `runScripts: 'dangerously'`, stub `fetch`, `matchMedia`, `localStorage`, `IntersectionObserver` as needed). If a page cannot render under jsdom after honest effort, record why in monolith-map and fall back to `npx playwright install chromium` + a Playwright renderer — same output contract.
- [ ] **Step 3:** Emit all 6 page goldens + shell golden; eyeball each for non-empty, plausible content (a golden of an empty section is a harness bug, not a pass).
- [ ] **Step 4:** Commit fixtures + loader only: `test(dashboard): record public API fixtures`.

### Task 5: Shared shell + infrastructure port

**Files:**
- Create: `dashboard/src/components/shell/{Sidebar,ThemeControl,MobileShell}.tsx`, `dashboard/src/components/search/GlobalSearch.tsx`, `dashboard/src/components/ui/{EntityButton,Pager,Sparkline,States,StatusChip,CopyControl}.tsx`, `dashboard/src/components/EntityPanel.tsx`, `dashboard/src/lib/scoring.ts` (pure formulas: dethrone floor `champComposite + effectiveMargin`, `dethroneBandScale`, cohort/quorum readers using `Number(state.cohort_size)`-style coercion — extracted from monolith JS), `dashboard/src/styles/` (shell/base tokens extracted from monolith CSS lines 94–2530)
- Create: tests beside each (`*.test.tsx`), `dashboard/src/lib/scoring.test.ts`

**Interfaces:**
- Consumes: Task 2 router/useEndpoint; Task 4 fixtures.
- Produces: `<AppShell>` wrapping routed pages: sidebar routes all six sections (old test `test_sidebar_shell_routes_every_section`), theme switcher modes `system|light|dark|time` (old test `test_includes_system_and_time_aware_theme_switcher`), global search (`test_includes_accessible_global_search`), entity query-popovers + pages (`test_dashboard_entities_use_query_popovers_and_pages`), mobile sidebar z-order below modal (`test_mobile_sidebar_stays_below_modal`), copy controls (`test_includes_copy_controls_for_operational_identifiers`). `lib/scoring.ts` exports are the only place scoring math lives — components must import, never inline.

- [ ] **Step 1:** For each old test in assert-inventory tagged shared/shell: write the vitest port FIRST (jsdom + @solidjs/testing-library), watch it fail.
- [ ] **Step 2:** Port implementation from monolith JS/markup until green. Formula asserts become `scoring.test.ts` cases (e.g. floor uses additive margin — test `floor(c, m)` equals `c + m`, and a regression case that it is NOT `c * (1 + m)`).
- [ ] **Step 3:** `npm run check && npm test` green; commit `feat(dashboard): port shell, search, theme, entity infrastructure`.

### Task 6: Page ports — overview + leaderboard

**Files:**
- Create: `dashboard/src/pages/OverviewPage.tsx` + `dashboard/src/components/overview/{EmissionsPanel,RolloutPanel,ChampionBox,CompactBoard}.tsx`; `dashboard/src/pages/LeaderboardPage.tsx` + `dashboard/src/components/board/{BoardTable,BoardFilter,VersionPills,FamilyStanding}.tsx`; per-page styles; tests beside each.

**Interfaces:**
- Consumes: shell/widgets from Task 5, `useEndpoint`, fixtures, goldens `overview.html`/`leaderboard.html`.
- Produces: overview two-pane split (`overview-split`), champion box, memory-timeline lead, compact board, emissions+rollout strips below board, **no `<details>` disclosure of standings** (`test_overview_shows_the_full_board_without_a_disclosure` incl. its negative asserts); leaderboard full column set (`rank,name,bench,composite,tool,memory,latency,first_seen` sortable), filter over name/uid/hotkey (`test_leaderboard_carries_a_filter_over_name_uid_and_hotkey`), version pills, family standing (#591), no tie labels (`test_leaderboard_omits_tie_labels`), emissions threshold copy "Beat this to contend" + "this is a floor, not a guarantee" via `lib/scoring.ts`.

- [ ] **Step 1:** Port every assert-inventory row tagged overview/leaderboard to failing vitest tests (keep old docstrings as comments).
- [ ] **Step 2:** Implement from monolith markup+JS until green.
- [ ] **Step 3:** Golden diff both pages against Task 4 goldens (same normalizer); investigate every hunk — accept only class-rename/framework-artifact diffs, record accepted diffs in `dashboard/PARITY.md`.
- [ ] **Step 4:** Commit `feat(dashboard): port overview and leaderboard pages`.

### Task 7: Page ports — benchmark + operations

**Files:**
- Create: `dashboard/src/pages/BenchmarkPage.tsx` + `dashboard/src/components/benchmark/{MemoryTimeline,VersionArchive,RolloutState}.tsx`; `dashboard/src/pages/OperationsPage.tsx` + `dashboard/src/components/operations/{FleetTable,PipelineAtlas,SnapshotSkew}.tsx`; styles; tests.

**Interfaces:**
- Consumes: Task 5 widgets; goldens `benchmark.html`/`operations.html`.
- Produces: memory timeline plots field + crowns champion, names gaps, window not pinned to bench version, v7 present with gaps, king label clear of chart (#512); archive pill files older bench versions (#587); rollout/authority state never promotes target (`test_benchmark_authority_state_never_promotes_rollout_target`); accessible benchmark progress per-check (#430). Fleet: slot counts = dispatch-funded not advertised (#540), evicted-but-running never idle (#537), standing gated on servable bench (#514), inoperative folded into collapsible (#511/`test_inoperative_fleet_nodes_fold_into_the_collapsible`), accessible fleet status, panels share one snapshot + show skew (`test_operations_panels_share_one_snapshot_and_show_skew`), validator names optional untrusted decoration.

- [ ] Steps 1–4: same TDD → implement → golden diff → commit cycle as Task 6. Commit `feat(dashboard): port benchmark and operations pages`.

### Task 8: Page ports — submissions + reviews + transparency copy

**Files:**
- Create: `dashboard/src/pages/SubmissionsPage.tsx` + `dashboard/src/components/pipeline/{PipelineColumns,PipelineDetail,QuickFilters}.tsx`; `dashboard/src/pages/ReviewsPage.tsx` + `dashboard/src/components/evidence/{AgentEvidence,DisputeForm,ScreeningReviewCards,AthQueue}.tsx`; styles; tests incl. `dashboard/src/lib/scoring.test.ts` additions.

**Interfaces:**
- Consumes: Task 5 widgets; goldens `submissions.html`/`reviews.html`.
- Produces: pipeline columns by stage (waiting_screening/screening/waiting_validator/evaluating/scored), server-backed quick filters, URL filter/page restore + sanitize (`test_submission_filters_and_page_restore_and_sanitize_the_url`), mobile+keyboard accessible filters, previous-generation waiting rows labeled (#458), kept-failure-as-history (#459/`test_validator_progress_keeps_superseded_failures_as_history`), terminal states (#462), policy-rescreen explainer, score-floor message attributes its number (#516), ATH review queue, terminal screening review cards, miner-facing review copy, model-use verdict + judged bar (#527), scoring/emissions/KOTH explainer with **no hardcoded fold constants** (all five banned literals from `test_no_hardcoded_fold_constants` stay banned — grep the built bundle), benchmark version never a literal, no reference-baseline stat, submission source download links (#278), public source repos advertised.

- [ ] Steps 1–4: same cycle. The negative asserts here run against **built output**: add `dashboard/src/build-invariants.test.ts` that builds once (or reads `dist/` in CI order) and greps banned literals. Commit `feat(dashboard): port submissions and reviews pages`.

### Task 9: CI + deploy integration

**Files:**
- Modify: `.github/workflows/ci.yml` (add `dashboard` job ONLY)
- Modify: `scripts/update.sh` (single insertion)
- Modify: repo-root `package.json` scripts if faircopy needs to cover `dashboard/src` copy (check `lint-copy` config; dashboard user-facing copy must be lintable or explicitly excluded with a comment)

**Interfaces:**
- Produces: CI job: pinned `actions/setup-node@<full SHA for current v5.x>` with `node-version: 24`, `cache: npm`, `cache-dependency-path: dashboard/package-lock.json`; steps `npm ci`, `npm run check`, `npm test`, `npm run build` in `dashboard/`. update.sh: in the preflight zone (after `uv sync`, before pm2 section), `(cd "$REPO_DIR/dashboard" && npm ci --no-audit --no-fund && npm run build)` with the script's existing error-handling idiom, so failure aborts pre-pm2 and existing rollback rules apply.

- [ ] **Step 1:** Add CI job; validate with `gh api repos/{owner}/{repo}/actions` schema locally via `actionlint` if available, else careful review.
- [ ] **Step 2:** Insert update.sh step matching surrounding style (read the whole script first; respect its deploy_stage bookkeeping — set/restore stage vars the way neighboring blocks do).
- [ ] **Step 3:** `make lint lint-copy` green. Commit `ci(dashboard): build and test the SPA; build on deploy`.

### Task 10: Full verification + PARITY.md + PR

**Files:**
- Create: `dashboard/PARITY.md` (57-row mapping: old test → new test file::name(s) → status; plus accepted golden-diff notes)
- Rewrite: `dashboard/README.md` (dev workflow: npm ci/dev/test/build, proxy env, fixtures, parity policy)

- [ ] **Step 1:** `cd dashboard && npm ci && npm run check && npm test && npm run build` — all green.
- [ ] **Step 2:** `make lint lint-copy typecheck test` — all green (if hundreds of DB failures appear, run `make test-db-reset` once before believing them).
- [ ] **Step 3:** PARITY.md complete — every assert-inventory row mapped; zero rows dropped without a written reason.
- [ ] **Step 4:** Serve for real: `make stack-up && make migrate && make api-up` (background) + `npm run build`; curl `/` and one entity path; confirm SPA HTML + 200 assets. Screenshot-level QA is deferred to the user (unattended run) — note it in the PR body as pending.
- [ ] **Step 5:** Push branch; open **draft** PR into `main` titled `refactor(dashboard): migrate to Vite SolidJS SPA`; body: summary, harvest-vs-rederive rationale, parity evidence (test counts, PARITY.md), verification transcript, pending-QA note, `Supersedes #356`. Do not close #356.
- [ ] **Step 6:** Watch CI on the pushed head (`gh pr checks --watch`); fix red until green or record exact failure state in the PR as a comment... (comment must be plain factual status).

## Self-review notes

- Spec coverage: Decisions 1–3 → Tasks 2/9 (toolchain), 1/4/5–8 (re-derive + parity), 3 (serving), 9 (deploy), 10 (gates). All six spec verification gates land in Task 10 except gate 5 (manual device QA — explicitly deferred, flagged in PR).
- Old-test names cited per task come from `assert-inventory.md` (Task 1) — Task 1 MUST complete before 5–8; Tasks 6/7/8 are parallelizable after 5.
- Type consistency: pages consume `useEndpoint`, `lib/scoring.ts`, shell components exactly as named in Tasks 2/5.
