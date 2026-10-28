# Release pipeline speed

The running log for making `release.yml` faster. Each PR changes one lever.
After each one merges, the next real release is measured. Wall clock runs
from the run's creation to its completion; the critical path is the job that
gates `release`.

## Baseline (2026-10-27/28, 11 release runs)

Typical Platform-affecting release: 20–27 min. Verification ends at about
20.5 min and everything after it (release, images, deploys) takes 3–6 min.

| Job (release `verify-*`) | Median | Max |
|---|---|---|
| verify-platform / pytest (other-endpoints-remaining) | 20.1 min | 20.8 |
| verify-platform / pytest (data-contract-integration) | 9.4 | 10.1 |
| verify-platform / pytest (other-endpoints-large) | 6.1 | 6.9 |
| verify-platform / pytest (screener-inference-endpoints) | 5.3 | 5.6 |
| verify-platform / pytest (public-validator-endpoints) | 4.8 | 7.0 |
| verify-platform / pytest (api-server) | 4.0 | 4.2 |
| verify-model-relay | 3.5 | 3.7 |
| verify-root-run / tests (0..2) | 3.5 | 3.8 |

The remaining shard ran 1,941 tests in 18m47s on a 4-vCPU runner, while
the other five shards finished in 4–10 min. The Platform suite has 8,692
tests. About 1 min per shard is setup (checkout, uv, Chromium, Postgres).

## Log

| PR | Lever | Release before | Release after | Critical path after |
|---|---|---|---|---|
| #3249 | Platform pytest: 6 hand-listed shards → 8 uniform round-robin shards; deleted one timing-flaky log test | 20–27 min; full-stack 28 min (40721447774, verify ends 21.4 min) | 15.2 min full-stack (40746407609, verify ends 8.0 min) | Platform shard 7.4 min; full-stack image/deploy tail ~7 min |
| #3251 | Observability only: Backroom `get_validator_request_outcomes` | — | pending | — |
| #3252 | Test harness: reuse built routes and the OpenAPI document across per-test apps | — | pending | — |
| #3253 | Delete 28 round-trip tests of migrations already applied in prod | — | pending | — |

Operator steer (2026-10-28 18:40): no more sharding. The primary lever is
deleting tests, keeping live known-vector/determinism, wire/OpenAPI, Alembic
single-head, security/anti-tamper/treasury guards and this week's
incident regressions. Caching, PR-CI reuse and supersession fixes are
still fine.

## Next levers

- Delete low-value Platform tests file by file (`--durations=25` prints in every shard).
- Reuse PR CI results when the release tree equals a verified PR merge tree.
- Release concurrency that queues instead of superseding an in-flight publish.
