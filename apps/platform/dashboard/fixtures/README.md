# Recorded public API fixtures

Captured 2026-07-31 (the two `*-summary.json` files 2026-08-03, with #648's
endpoint) from the production public read API
(`https://platform-api.heyditto.ai/api/v1`), which serves only public,
aggregate-only data. Used by the vitest suite (via `src/test-fixtures.ts`) and
by the monolith golden renderer that gated the SPA port. Both leaderboard
snapshots are trimmed to their first 12 entries to keep the checkout small;
every other payload is verbatim.

| file | endpoint |
|---|---|
| `health.json` | `/public/health` |
| `operations.json` | `/public/operations` |
| `leaderboard.json` | `/public/leaderboard` (v7 current; entries[:12]) |
| `leaderboard-v6.json` | `/public/leaderboard?bench_version=6` (entries[:12]) |
| `weights.json` | `/public/weights` |
| `validator-names.json` | `/public/validator-names` |
| `screeners.json` | `/public/screeners` |
| `bench-glossary.json` | `/public/bench/glossary` |
| `bench-config.json` | `/public/bench/config` |
| `bench-rollout.json` | `/public/bench/rollout` |
| `bench-timeline.json` | `/public/bench/timeline` |
| `activity.json` | `/public/activity?page=1&limit=50` |
| `activity-ath.json` | `/public/activity?review=ath&status=under_review&limit=200&page=1` |
| `activity-single.json` | `/public/activity?page=1&limit=1&q=<top agent id>` |
| `agent-top-summary.json` | `/public/agent/<top agent id>/summary` |
| `agent-top-pipeline.json` | `/public/agent/<top agent id>/pipeline` |
| `agent-top-scores.json` | `/public/agent/<top agent id>/scores` |
| `agent-rejected-summary.json` | `/public/agent/<rejected agent id>/summary` |
| `agent-rejected-pipeline.json` | `/public/agent/<rejected agent id>/pipeline` |

Refresh by re-running the same GETs (see `src/test-fixtures.ts` for the two
recorded agent ids) and re-applying the entries[:12] trim to both leaderboard
files.
