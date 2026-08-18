# Ditto SN118 public dashboard

The public "front door" for Subnet 118: a Vite + SolidJS + TypeScript SPA. Its
agent drilldown uses TanStack Solid Query for keyed request caching,
cancellation, and retries. No
external requests at runtime, **no secrets** — it reads the platform's public
API and links out to wandb for the per-epoch deep dive. This is Surface 3 in
[`docs/public-telemetry.md`](../docs/public-telemetry.md).

## What it shows

- **Subnet snapshot** — total miners are the primary signal, with scored-miner,
  leaderboard, throughput, and latency metrics in one top-level panel.
- **Leaderboard** — one ranked representative per payment-time ownership family,
  selected by the best eligible canonical score. Other finalized generations
  remain available in a collapsed family disclosure with their canonical scores,
  but receive neither fake ranks nor extra emission positions. Submission detail
  pages state when a scored generation is represented by another family member.
  The ranked representative carries a separate KOTH emissions projection that identifies the
  first-seen incumbent champion and participation-tail recipients. The projection
  applies the validator's frozen 0.007 composite-point hysteresis, statistical
  dethrone band, and v6+ high-score decay, so raw rank #1 is never mislabeled as
  champion. A native Subtensor read
  overlays the last publicly revealed validator vectors at one block, while
  explicitly separating those lagging commit-reveal inputs from stake-weighted
  Yuma emissions. Click a row for a drill-down (tool-vs-memory split, first-seen,
  raw rank, projected emissions role, and revealed validator top-choice/support
  counts). Current SN118 registration
  is reported separately: a deregistered hotkey's immutable score stays visible
  but is marked inactive and excluded from weights and emissions until that same
  hotkey registers again. Every leaderboard submission also shows its own
  public-source state. Source becomes downloadable six hours after the third
  accepted score in one benchmark version; this is retroactive and independent
  of KOTH rank. A download requests a five-minute, no-store tarball link.
- **Submission pipeline** — screening and validator-ticket history, including a
  compact accessible benchmark progress bar for each validator currently
  evaluating the submission. Active benchmark work takes precedence over a
  submission's previously completed stage, so version-rollout rescoring stays
  aligned with the validator fleet. Running work carries its ticket-bound bench
  version, and top-five qualification rows state that the prior score remains
  authoritative while the next-version quorum is collected. Every submission
  detail shows whether source is awaiting quorum, in the six-hour privacy window,
  held for review, or public; review-held and rejected source stays private.
  Accepted numeric scores appear immediately
  in the current-version summary as provisional feedback; the prior final
  median remains authoritative until the new three-validator quorum. Each score
  includes its post-commit seed and a
  version-pinned `dittobench-datagen` reproduction command, without exposing
  ticket signatures or associating the number with a validator identity.
  Agent links start a targeted summary and the larger evidence record in
  parallel. The summary paints first; source state, scores, screening, and
  validator history fill in asynchronously without a manual load control.
- **Validator fleet** — signed worker availability, coarse system health, and
  the public active agent with the same stage/progress shown in the pipeline.
  Old clients render as progress not reported; expired or stale work disappears.
  A slot whose lease an operator evicted is the one exception to "expired work
  disappears": the platform releases its half of the lease at once but the
  validator's container keeps running, so the slot reads `Evicted · still
  running` for as long as the validator's own signed report still claims it.
  Where the platform cannot tell — heartbeat protocol below 16 omits a
  claimed-but-quiet slot, so its silence proves nothing — the slot reads
  `Evicted · state unknown` rather than either confident answer. Neither is
  headroom. Only a slot with positive evidence the container is gone goes back
  to `Idle`.
  Inoperative nodes fold into a collapsible below the table, so a validator that
  cannot take work stops crowding the fleet that can. Two ways to qualify: no
  heartbeat inside the offline window (a validator hotkey's last report is kept
  indefinitely, so this is what stops months-dead hosts accumulating), or
  software too old to serve the benchmark being scored, which the platform
  leases nothing to. A *current* validator whose scorer broke is deliberately
  left in the open table reading "Scorer down" — that is a live incident, not an
  obsolete build. The fold keeps the reason: a folded row still carries its
  status badge ("Obsolete build", "Scorer down", "No bench v7"), its drill-down —
  which names the upgrade — and its deep link, which unfolds the section. Status
  counts in the side ledger stay whole-fleet, and the closed summary names every
  fold reason and its count.
- **Stable object links** — all SPA state (popup/selected-row params, the
  submissions filters (including `downloadable=true`), and both pagers) lives
  in a query string inside the hash,
  on whatever page it was opened from (`#/submissions?agent={id}`,
  `#/overview?miner={hotkey}`, `#/operations?validator={hotkey}`,
  `#/submissions?status=rejected&page=2`, `#/overview?page=2` for the
  leaderboard page). Page-scoped view state (the filters and either pager's
  `page`) is cleared when you navigate to a different page, so it never trails
  along as stale state — which is also why both pagers can share the `page` key
  without colliding.
  The real query string carries only deploy/config knobs (`?api=`, `?wandb=`),
  so the document URL — and its HTTP cache entry — stays stable while the SPA
  navigates. Agent and miner popovers link to dedicated `/agent/{id}` and
  `/miner/{hotkey}` pages. Direct visits and browser back/forward navigation
  restore the same state; older link forms (`?agent={id}#/submissions`
  real-query state, plural pathname and hash routes) are recognized and
  normalized to the current form.
- **Anti-overfit assurance** — explains that seeds are fixed only after the
  submission is committed, rotate per submission, and can reproduce a completed
  evaluation without changing the already-submitted artifact.

It intentionally shows **only** what the public API exposes. In-progress score
rows are a narrow safe projection (composite, deterministic dataset inputs, and
acceptance time); identities, signatures, ticket leases, and scorer internals
stay private. The leaderboard serves a read-only KOTH projection for explanation;
validators still compute and submit the authoritative weight vector independently,
and Yuma combines their revealed inputs stake-weightedly. Full per-epoch
telemetry remains in wandb (linked).

## Configure

Resolved in priority order:

| What | Query string | Meta tag (bake in) | Default |
| --- | --- | --- | --- |
| API base | `?api=https://api.host/api/v1` | `<meta name="ditto:api-base">` | same-origin `/api/v1` |
| wandb link | `?wandb=https://wandb.ai/org/ditto-sn118` | `<meta name="ditto:wandb-url">` | `https://wandb.ai/` |

For a deployed dashboard, edit the two `<meta>` tags in `index.html` (they
pass through the build) so the defaults are correct and the query string is
only needed for testing.

## Develop / run

```sh
cd dashboard
npm ci                # install (pinned lockfile)
npm run dev           # Vite dev server on :8080, /api proxied to :8000 (make api-up)
npm test              # vitest (jsdom, recorded fixtures — no network)
npm run check         # tsc + oxlint + prettier --check
npm run build         # production bundle -> dist/
```

Override the dev proxy target without touching CORS:
`DITTO_DASHBOARD_PROXY_TARGET=https://platform-api-dev.heyditto.ai npm run dev`.

If the API can't be reached the page renders an explicit unavailable state. It
never substitutes sample values for live subnet data.

## Layout & tests

- `src/pages/` — one module per sidebar page; `src/components/` — shell,
  board, and per-domain components; `src/lib/` — pure logic (routing, scoring
  and emissions math, bench rollout state, formatting); `src/data/` — the
  polling endpoint resources; `src/types/` — wire shapes.
- `fixtures/` is a recorded production snapshot (see `fixtures/README.md`);
  the vitest suite renders against it with a frozen clock, so tests are
  deterministic and offline.
- Parity with the pre-SPA dashboard is tracked in `PARITY.md`: every
  appearance assertion the served-HTML Python tests used to make maps to a
  vitest test here. Add to it when you move or retire one.

## Deploy

**Default (this repo): served by the platform, same-origin.** The API serves
the built `dist/` at `/` (see `factory.py`; `scripts/update.sh` runs the build
during deploy), so on the deployed hosts it's already live:

- dev  → `https://platform-api-dev.heyditto.ai/`
- prod → `https://platform-api.heyditto.ai/`

Same-origin means the SPA's `/api/v1/public/*` calls need no CORS and the wandb
link is injected from `DITTO_DASHBOARD_WANDB_URL` at serve time — no need to edit
this file per environment. `DITTO_DASHBOARD_ENABLED=false` runs the API headless.

**Alternative: host it yourself.** `npm run build` emits a fully static
`dist/` — upload it to object storage (S3/MinIO/GCS) behind a CDN, or any
static host. A *cross-origin* host
would additionally require CORS on the API's `/public/*` routes (not currently
enabled, since the default is same-origin). The API sets
`Cache-Control: public, max-age=30` on the data; the SPA auto-refreshes on the
same 30s cadence.

## SEO / crawlers

Same-origin serving (the default) also exposes the landing-astro discovery
set at the dashboard origin:

| Path | Freshness |
| --- | --- |
| `/robots.txt` | 5 min |
| `/llms.txt`, `/llms-full.txt` | 5 min |
| `/sitemap.xml` | 30s (includes current miner / agent URLs) |
| `/`, `/leaderboard`, … | 30s HTML with JSON-LD + `<noscript>` standings |

The HTML cache matches the public leaderboard (`max-age=30`). Crawlers that
do not run JavaScript still see the current ranks in JSON-LD and a noscript
list; the same numbers live at `/api/v1/public/leaderboard`. A failed board
read never substitutes sample ranks — the static shell is served instead.

Hash routes (`/#/leaderboard`) still work. Crawlable path aliases
(`/leaderboard`, `/benchmark`, `/pipeline`, `/operations`, `/submissions`,
`/ath`, `/overview`) are what the sitemap advertises.
