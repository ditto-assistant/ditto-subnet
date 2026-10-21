# CI cost controls (SN-120)

Standalone component CI and preview creation are manual-only. Their jobs and
verification steps remain available for an explicit operator dispatch after
re-enabling the workflow in GitHub Actions settings. They do not run on PRs,
main pushes, or daily preview schedules.

GitHub workflow settings additionally disable the 24 standalone workflows
listed in SN-120. One retired reranker workflow exists only in GitHub metadata;
there is no current source file to change. Preview publishing is reusable and
its only automatic caller is now manual-only.

Release remains the automatic affected-component verifier/build/deployment
pipeline on main. Its reusable root and Platform verification workflows are
unchanged. Required migration-order/merge-result, independent PR approval,
thread resolution, operational recovery/backup, and preview cleanup remain.

Tradeoff: standalone pre-merge test feedback is no longer automatic. Explicit
verification remains available; release verification must succeed before its
artifacts can publish or deploy. This does not claim every standalone test is
duplicated by a release job.

To restore automatic CI, review a change restoring the event triggers and
re-enable the corresponding workflow through GitHub Actions settings.

Screener deployment contract tests assert the manual-only trigger and retained
lint/type/provenance collection plus the exact-merge release gate. For a
starter-kit-only change, explicitly dispatch the screener workflow to check
starter provenance; automatic PR path triggers remain disabled.
