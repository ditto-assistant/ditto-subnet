# Release pipeline speed

The running log for making `release.yml` faster. Each PR changes one lever.
After each one merges, the next real release is measured. Wall clock runs
from the run's creation to its completion; the critical path is the job that
gates `release`. Runner variance is about ±1 min per shard, so judge
deletion PRs cumulatively.

## Baseline (2026-10-27/28, 11 release runs)

Typical Platform-affecting release: 20–27 min. Verification ends at about
20.5 min and everything after it (release, images, deploys) takes 3–6 min.

| Job (release `verify-*`) | Median | Max |
|---|---|---|
| verify-platform / pytest (other-endpoints-remaining) | 20.1 min | 20.8 |
| verify-platform / pytest (data-contract-integration) | 9.4 | 10.1 |
| verify-platform / pytest (other-endpoints-large) | 6.1 | 6.9 |
| verify-model-relay | 3.5 | 3.7 |
| verify-root-run / tests (0..2) | 3.5 | 3.8 |

## Log

| PR | Lever | Release run | Wall | Platform shards | Verify ends |
|---|---|---|---|---|---|
| baseline | 6 hand-listed Platform shards | 40721447774 (full stack) | 28 min | 4–20 min | 21.4 |
| #3249 | 8 uniform round-robin shards; deleted a timing-flaky log test | 40746407609 (full stack) | 15.2 | 5.2–7.4 | 8.0 |
| #3251 | Observability: Backroom `get_validator_request_outcomes` | superseded by #3250's 40749567071 | 12.7 (4.3 queued) | — | 11.9 |
| #3252 | Test harness reuses built routes and the OpenAPI document | 40753670081 | 10.0 | 3.5–5.5 | 6.4 |
| #3253 | −28 frozen migration round-trips | 40755689847 | 7.7 | 3.9–5.8 | 7.1 |
| #3254 | −113 screener endpoint cases | 40756921212 | 7.3 | 4.1–5.9 | 6.7 |
| #3255 | −47 public endpoint cases | 40758127908 | 6.8 | 3.6–5.3 | 6.3 |
| #3256 | −61 validator endpoint cases, dead retry helpers | 40759166633 | 6.2 | 3.4–5.2 | 5.8 |
| #3257 | −182 admin and identity cases | 40760193228 | 6.4 | 3.5–5.2 | 5.9 |
| #3258 | −197 db-query/scoring/script cases | 40761221762 | **failed** verify-backroom | 3.2–4.9 | 5.5 |
| #3259 | Restore the ATH replay fixture Backroom parses | 40764201044 | 9.9 (with deploys) | 3.4–5.0 | 5.8 |
| #3260 | −611 unit cases; Backroom CI guard on Platform fixtures | 40769470667 | 6.9 | 3.5–5.5 | 6.5 |
| #3261 | −145 bounty/receipt/canary cases | 40770499743 | 6.6 | 3.1–5.0 | 5.6 |
| #3262 | −139 admin/outlier/query cases | 40771465114 | 7.1 | 3.1–5.0 | 6.0 |
| #3263 | −672 root cases (retired coding track, workflow pins) | 40772533091 | 7.2 | 2.9–4.7 | 5.8 |

Totals: Platform 8,692 → 7,178 collected tests (−1,514); root 4,655 →
3,983 (−672). A test-only release now takes 6–7 min; one that deploys Platform
and Backroom takes about 10 min.

Operator steer (2026-10-28 18:40): no more sharding. The primary lever is
deleting tests, keeping live known-vector/determinism, wire/OpenAPI, Alembic
single-head, security/anti-tamper/treasury guards and this week's
incident regressions. Caching, PR-CI reuse and supersession fixes are
still fine.

## Lessons

- Search for references from the repository root. #3258 deleted a Platform
  fixture that Backroom's schema test also parses; a `git grep` run from
  `apps/platform` missed it, and Backroom CI did not trigger on that path.
  #3260 adds `apps/platform/ditto/tests/fixtures/**` to Backroom CI.
- PR-CI reuse by comparing component trees is unsound here, because Platform
  tests read files across the repository (docker-compose, services,
  research), so the trigger paths are not the input set.
- Supersession is inherent: `release` pushes a `chore(release)` commit onto
  main, so it must refuse a stale head. Shorter verification is the fix.

## Next levers

- Each Platform shard spends about 2 min on setup and collection (checkout, uv,
  Postgres, collecting about 7k tests in each of 4 xdist workers). Collection cost
  scales with imported test modules.
- After Platform, the critical path is model-relay at 3.6 min and root tests
  at 3.5 min. Root tests spend most of their time in real sleeps (confirmation
  lane 3–4 s per test, `test_generator_activation.py` 36 s,
  `TestLegacyLanesWaitForTheLedgerAnchor` 11 s per test).
- Retire the coding track's remaining tooling: `coding-native-release.yml`
  (still PR-triggered), `coding-hosted-image.yml`, the dormant coding ansible
  roles, the default-off validator coding modules, and the unconsumed
  `build-coding-executor-scorer` release job.
