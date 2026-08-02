# Token efficiency for benchmark v7 and later

> **Current contract:** do not run a 60-run starter-kit calibration campaign.
> That workflow is retired. Its runner, transfer model, and baseline generator
> have been removed. The completed v7 campaign is historical evidence only; it
> is not a prerequisite for v8 or any later benchmark.

The deterministic scorer is quality-only for benchmark v7 and later. It records
trusted provider usage but applies a neutral token multiplier of `1.0`. Changing
the dataset, starter kit, or benchmark version therefore does not require a new
absolute starter-kit token budget.

Token efficiency is instead a platform-owned, dynamic relative bonus:

1. The ticket-scoped platform proxy fixes the permitted model and route while
   retaining the upstream credential outside the miner sandbox.
2. The broker records complete provider-response usage. Missing, partial, or
   mixed-identity accounting fails closed as validator infrastructure rather
   than becoming a miner score or bonus input.
3. The platform's model-use rule checks that the metered calls and prompt volume
   show meaningful use of the locked model. The rule can be observed in shadow
   mode before enforcement.
4. Only integrity-qualified, quality-qualified submissions enter an
   epoch-frozen cohort. The platform derives robust live token frontiers from
   that cohort and awards a bounded upside-only efficiency bonus.
5. Frozen cohort snapshots and insert-once bonus rows keep published results
   reproducible even though future cohorts adapt to current miner behavior.

The authoritative implementation and rollout controls live in
`ditto-platform`; the scorer only supplies signed quality and trusted usage
inputs. A benchmark rollout remains separately Backroom-gated.

## v7 compatibility record

The embedded v7 manifest retains the completed July 2026 campaign identity so
already-deployed v7 validators continue advertising the same capability digest.
Its p90 references and 75% allowances never enter v7 scoring. They must not be
copied forward, refreshed, or interpreted as budgets awaiting activation.

The historical public-safe campaign evidence remains in
`docs/v7-hosted-embedding-calibration-evidence.json`. Prompts, responses,
credentials, and provider keys are intentionally absent.

## v8 readiness

V8 uses `internal/efficiency/contract_v8.json`, a small quality-only contract
that binds:

- the exact dataset known vector;
- the locked GPT-OSS model and OpenRouter route profile;
- the hosted embedding model, profile, dimensions, and catalog identity;
- `scorer_token_policy: quality-only`; and
- `efficiency_authority: ditto-platform-relative-cohort-v1`.

It intentionally contains no starter baseline, calibration grid, token
allowance, or scoring-enabled phase gate. Technical capability advertisement
does not activate v8; the platform rollout state remains authoritative.
