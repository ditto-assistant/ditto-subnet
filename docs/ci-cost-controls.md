# CI cost controls (SN-120)

Release remains the automatic affected-component verifier, build and
deployment pipeline on main. Its reusable root and Platform verification
workflows are unchanged. Required migration-order/merge-result, independent PR
approval, thread resolution, operational recovery/backup, and preview cleanup
remain.

## Pull-request checks (restored 2026-10-23)

Main had gone red on trivial breaks (an unformatted test, a catalog-size
budget) that no PR check caught, which held a release. These path-filtered
standalone workflows run on pull requests again:

| Workflow | Typical PR runtime |
|---|---|
| `conventional-pr.yml` (drives semantic release) | < 1 min |
| `ci.yml` (root validator, compose, root verify) | 2 min |
| `platform-ci.yml` (coding-contract paths removed) | 13 min |
| `platform-dashboard-ci.yml` | 1 min |
| `screener-ci.yml` | 2 min |
| `backroom-ci.yml` | 1 min |
| `starter-kit-ci.yml` | 1 min |
| `router-starter-kit-ci.yml` (only on its own kit) | 2 min |
| `dittobench.yml` (coding-driver jobs skip on PRs) | 4 min |
| `model-relay.yml` | 3 min |
| `collector-custody.yml` | < 1 min |

## Still operator-dispatched

- **Coding competition:** `coding-datagen-ci.yml`, `coding-hosted-image.yml`,
  `coding-hosted-runtime.yml`, `coding-starter-kit-ci.yml`, and the eight
  coding-driver jobs inside `dittobench.yml`. (`coding-native-release.yml`
  keeps its narrow, coding-file-only PR trigger.)
- **Heavy or research CI:** `datagen-ci.yml` (about 20 min) and `infra-ci.yml`
  (infrastructure goes through protected Terraform plan/apply; past PR runs
  hung for hours).
- **Previews:** `preview*.yml` creation stays manual; `pylon-epoch-schedule.yml`
  stays off; `reranker-ci.yml` is retired.

Each restored workflow also has to be re-enabled in GitHub Actions settings
(`gh workflow enable <file>`); a trigger in source does nothing while the
workflow is disabled there.
