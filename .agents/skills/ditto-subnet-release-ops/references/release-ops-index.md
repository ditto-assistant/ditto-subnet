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
| Screening worker runtime | `workers/screener/` |
| Production DB / Targon logs | `.agents/skills/gcloud-ditto-readonly/` |
| GCP and Cloudflare state | `infra/terraform/stacks/` |
| Hippius Coding secret containers | `infra/terraform/stacks/gcp-platform/main.tf`, `infra/ansible/roles/platform_app/` |
| Host convergence | `infra/ansible/` |
| Platform app VM disk | `.agents/skills/ditto-subnet-release-ops/references/platform-host-disk.md`, `app_boot_disk_gb` |
| Validator updater | `scripts/validator-stack-auto-update.sh` |
| Subnet liveness (incident triage) | `apps/platform/ditto/api_server/subnet_liveness.py`, Backroom MCP `get_subnet_liveness`, `apps/backroom/docs/mcp.md` |

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

## Subnet-wide incident first read

When every miner seems stuck at once, call Backroom MCP `get_subnet_liveness`
(`GET /api/v1/admin/subnet-liveness`) before reading logs. It turns durable
Platform state into seven ok/warn/breach signals, each with `since` and a hint
naming the next read:

- `screening_admission` breach: a node at `screening_concurrency` 0, not
  ready, or on the wrong policy while uploads wait (#2474). Read
  `get_screener_capacity` node controls before touching the controller.
- `scoring_throughput` breach with `v13_scorer_cohort_pin` breach: read the
  pin `detail`. `members_with_different_packet` with `fresh_packets_agree`
  is the #2490 stale pin: rotate it, do not restart validators.
  `members_without_fresh_packet` is an offline or restarting validator:
  bring it back, because rotation is refused for a member with no packet.
- `lease_overrun` breach: no expiry sweep ran. Validator tickets expire only
  when a `/job` poll reaches ticket issuance, so either nothing polls or every
  poll is declined first (pin, pause, allocator, provider outage). Check
  validator heartbeats, dispatch declines and the screener fleet, not the
  lease settings.
- `source_emission_collector` breach: the finalized-block cursor stopped
  (#2231), usually after a chain runtime upgrade.

Disk and database headroom (#1745) is not in the read; use
[`platform-host-disk.md`](platform-host-disk.md). The read pages nobody, and
alert delivery is a #2600 follow-up. Record the signal values and `since` in
the incident note so recovery is measured from the same clock.

## Secret boundary

Use GitHub environments and WIF for CI identity, Secret Manager for provider/runtime secrets, and encrypted Worker bindings for Backroom. Tests may invoke `gcloud secrets versions access` only inside a consumer pipeline that never echoes, logs, returns, or stores the value beyond a mode-0600 temporary file with cleanup.
