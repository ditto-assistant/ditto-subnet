# Release and operations index

## Ownership

| Concern | Canonical paths |
|---|---|
| Affected-component graph | `release/components.toml`, `scripts/release-plan.py` |
| Semantic release and images | `.github/workflows/release.yml` |
| Platform deployment | `.github/workflows/platform-deploy.yml` |
| Screener controller deployment | `.github/workflows/screener-controller-deploy.yml` |
| Hosted DittoBench checks/build | `.github/workflows/dittobench.yml` |
| Capacity controller | `services/screener-orchestrator/` |
| GCP and Cloudflare state | `infra/terraform/stacks/` |
| Host convergence | `infra/ansible/` |
| Validator updater | `scripts/validator-stack-auto-update.sh` |

## Release graph expectations

- `platform_api` affects aggregate `platform` and dependent `backroom`.
- `platform_dashboard` affects aggregate `platform`, not `backroom`.
- `screener_orchestrator` depends on `screener` and deploys controller plus trusted builder behavior from the exact release commit.
- `validator_stack` binds validator, sandbox Docker, and DittoBench images into one signed descriptor.
- Infrastructure paths produce plan/apply work, not an application semantic release shortcut.

Verify rather than memorize:

```bash
python3 scripts/release-plan.py --help
rg -n '^\[component\.|depends_on|paths' release/components.toml
rg -n 'permissions:|environment:|workload_identity|needs\.|if:' .github/workflows
```

## Validation

```bash
uv run pytest ditto/tests/test_release_plan.py ditto/tests/test_release_workflow.py -q
uv run --project services/screener-orchestrator pytest services/screener-orchestrator/tests -q
terraform -chdir=infra/terraform/stacks/gcp-platform fmt -check
terraform -chdir=infra/terraform/stacks/gcp-platform validate
terraform -chdir=infra/terraform/stacks/cloudflare-dittobench validate
```

Validate shell syntax for every changed operational script and parse every changed workflow as YAML.

## Live proof ladder

1. Exact source and PR head.
2. Current required checks and reviews.
3. Merge commit and semantic release/tag.
4. Built artifact digest and provenance.
5. Deployed revision on the owning runtime.
6. Health plus functional/client-visible behavior.
7. Rollback rehearsal or a bounded, reviewed rollback command.

Do not collapse these into “green” or “deployed.”

## Secret boundary

Use GitHub environments and WIF for CI identity, Secret Manager for provider/runtime secrets, and encrypted Worker bindings for Backroom. Tests may invoke `gcloud secrets versions access` only inside a consumer pipeline that never echoes, logs, returns, or stores the value beyond a mode-0600 temporary file with cleanup.
