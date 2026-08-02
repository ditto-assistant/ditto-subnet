# Dashboard SPA refactor — design

Date: 2026-07-31. Branch: `e/dashboard-spa` (off `origin/main` @ 4b9856a). Single
cutover PR into `main`.

## Goal

Complete the migration started in PR #356 (`e/dashboard-ux`): replace the
monolithic `dashboard/index.html` (9,890 lines: CSS lines 94–2530, static markup
2531–3038, inline JS 3039–9888) with a Vite + SolidJS + TypeScript SPA — but
built against **today's** dashboard, which has absorbed 40 dashboard commits
since #356's merge-base (`b8bd941`). #356 is a *reference to harvest from*, not
a branch to rebase: its components predate the leaderboard page, the two-pane
overview, submission families, the bench archive pill, fleet slot
gating/counting, retained retest seeds, score-floor attribution, the gridded
gradient ground, and more (`git log b8bd941..origin/main -- dashboard` for the
full list).

## Decisions (settled with the user — do not relitigate)

1. **Harvest #356, re-derive UI.** Scaffold/toolchain/serving-path/module
   design come from `origin/e/dashboard-ux`; every page's markup, logic, and
   CSS are re-derived from today's `dashboard/index.html` on `main`. #356
   components are style reference only.
2. **Plain Vite, no Vite+.** Drop the `vp` CLI, the
   `npm:@voidzero-dev/vite-plus-core` alias, and the `curl -fsSL
   https://vite.plus | bash` CI step. Use pinned `vite` + `vitest` + `oxlint` +
   `vite-plugin-solid` + `typescript` with plain `npm ci`. This repo pins all
   CI actions by SHA; no unpinned curl-piped installer.
3. **Parity = classified port of all appearance assertions + rendered-DOM
   goldens as a coarse net.** Details below. This is the load-bearing phase:
   #356's fatal flaw was deleting 1,590 lines of appearance tests (57 tests,
   ~837 assert lines) and trusting a fresh vitest suite.

## What to harvest from `origin/e/dashboard-ux`

- `dashboard/package.json` — swap Vite+ for plain pinned toolchain; keep
  solid-js pin, scripts shape, devEngines npm pin.
- `dashboard/vite.config.ts` — keep solid plugin, dev proxy `/api ->
  http://localhost:8000` (env-overridable via `DITTO_DASHBOARD_PROXY_TARGET`),
  `build.target es2022`, `outDir dist`; move test config to standard vitest
  config; move lint rules to `.oxlintrc.json`.
- `dashboard/tsconfig.json`, `dashboard/src/test-setup.ts`,
  `dashboard/.gitignore`.
- Module layout: `src/pages/` (one per page), `src/components/` (shell,
  ui, per-domain), `src/data/useEndpoint.ts`, `src/lib/{api,config,copy,
  format,router}.ts`, `src/stores/routeStore.ts`, `src/types/`.
- `src/lib/router.ts` + `router.test.ts` (474 lines), `src/stores/routeStore.ts`
  + test (316 lines), `src/lib/format.test.ts` (285), `src/lib/api.test.ts`
  (203) — reusable near-verbatim; re-check route table against today's pages
  (adds `leaderboard`).
- `ditto/api_server/factory.py` changes — dist-serving at `/`, `/assets/`
  route with content-hash-forever/day caching, traversal guards, wandb-URL
  injection into built index. **Rebase onto today's factory**: #356's diff
  also deletes ~15 admin routers/resolvers that exist on main — that is
  staleness, take only the dashboard-serving delta.
- `ditto/tests/api_server/test_dashboard.py` from #356 — the 19
  serving-contract tests + `fake_dist`/`missing_dist` fixtures. These replace
  main's serving tests; main's 57 appearance tests are ported separately (see
  Parity).
- CI dashboard job shape (check/test/build) — reimplemented with
  `actions/setup-node@<pinned SHA>` + `npm ci`. Do **not** take #356's other
  ci.yml edits (it removes the Postgres service and downgrades setup-uv — both
  stale).

## What NOT to harvest

- `scripts/update.sh` — #356 replaces main's 385-line hardened deploy script
  (2026-07-25 near-outage failure semantics: preflight-before-mutate,
  rollback-to-running-revision, verify-served-commit) with a 57-line rewrite.
  Instead: surgically add a dashboard build step (`npm ci && npm run build` in
  `dashboard/`) to **main's** script, in the preflight zone before pm2 is
  touched, so a build failure aborts the deploy under the existing rollback
  rules. `node`/`npm` are already host prerequisites (pm2). `dist/` is
  gitignored, so a failed deploy leaves the previous build serving.
- Anything in #356's `factory.py`/`ci.yml` beyond the dashboard delta.
- The 38-file split CSS (`src/styles/01…38-*.css`, incl. 572-line
  `38-parity.css`) — evidence the extraction never came out clean. Re-extract
  from today's `<style>` block instead, organized per component/page.

## Parity harness

Main's `ditto/tests/api_server/test_dashboard.py` (1,590 lines) is the only
executable spec of dashboard appearance/behavior. Port every appearance
assertion into the SPA's vitest suite **before or alongside** each page port;
keep each test's docstring (they record which regression the assert guards).
Translation classes (~counts):

| Class | ~ | Target |
|---|---|---|
| Markup (`class=`/`id=`/`data-*`) | 105 | jsdom render + DOM queries |
| Copy / rendered values | ~560 | rendered-text queries |
| Inline-JS source text (e.g. `var floor = champComposite + effectiveMargin`, `dethroneBandScale`, asserted-absent `champComposite * (1 + margin)`) | 77 | extract formulas to pure functions in `src/lib/`, unit-test behavior; keep asserted-absent formulas as unit tests on the correct math |
| Negative (`not in body`) | 66 | DOM-absence checks; source greps where they guard against hardcoded constants |
| CSS source (e.g. leaderboard page un-hiding `.hide-md/.hide-sm`) | 15 | assert on built CSS / style files |
| Endpoint paths (`/public/...`) | 15 | `src/lib/api.ts` unit tests |

Python side keeps only the 19 serving-contract tests (from #356) — the
appearance suite's obligations move into vitest with an explicit
mapping (each old test name → new test file/name) recorded in
`dashboard/PARITY.md` so nothing silently drops.

**Goldens (coarse net, dev-time):** render today's monolith with fetch stubbed
to recorded fixtures, snapshot per-page DOM; diff SPA renders against them
during the port. Fixtures: record JSON from the public read-only API
(`https://platform-api-dev.heyditto.ai/api/v1/public/...`, prod fallback
`https://platform-api.heyditto.ai`) into `dashboard/fixtures/`. Prefer jsdom to
execute the monolith's inline JS; fall back to Playwright-chromium only if
jsdom can't run it. Goldens gate the port, not CI permanently.

## Pages (re-derive each from today's index.html)

`overview` (two-pane split + champion box, memory timeline lead, compact
board, emissions/rollout strips), `leaderboard` (full board, filter over
name/uid/hotkey, version pills, family standing), `benchmark` (memory
timeline + versions incl. v7 + archive pill, rollout/authority state),
`operations` (fleet table w/ slot counts + gating + inoperative collapsible,
pipeline atlas, snapshot skew), `submissions` (pipeline columns, quick
filters, URL restore/sanitize), `reviews` (ATH review queue, screening
review cards, dispute form). Shared: shell/sidebar, global search, theme
switcher (system/light/dark/time), entity popovers/pages, copy controls,
SHA/trend widgets, pager.

## Verification gates (all must pass before PR)

1. `cd dashboard && npm ci && npm run check && npm test && npm run build`
2. `uv run pytest -q ditto/tests/api_server/test_dashboard.py`
3. `make lint lint-copy typecheck test`
4. Golden diffs reviewed per page; `dashboard/PARITY.md` complete (57/57
   mapped)
5. Manual QA desktop + 390 px against live dev API
6. CI green on pushed head; PR opened as draft into `main`

## Out of scope

Visual redesign (pixel parity with today's dashboard is the goal), API
changes, any `ditto/db`/migration work, touching the deployed VM.
