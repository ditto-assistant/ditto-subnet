# Ditto Subnet Monorepo

This repository owns the public SN118 product and runtime as one release unit.
Use the repository agent skills instead of reconstructing cross-repository
context.

## Start every substantial task

```bash
python3 .agents/skills/ditto-subnet-context/scripts/lookup-context.py "<task>"
```

Load only the returned anchors and the relevant specialized skill:

- `.agents/skills/mine` for miner setup, local rehearsal vs on-chain scoring,
  packaging, and upload;
- `.agents/skills/ditto-subnet-platform` for Platform, migrations, dashboard,
  and Backroom;
- `.agents/skills/ditto-subnet-benchmark` for validator, scoring, DittoBench,
  datagen, and adapters;
- `.agents/skills/ditto-subnet-bench-version-bump` for shipping, activating, or
  auditing a `bench_version` contract across every layer;
- `.agents/skills/ditto-subnet-release-ops` for releases, deployments,
  screeners, Targon/GCE, GCP, Cloudflare, Terraform, and Ansible;
- `.agents/skills/ditto-subnet-preview` for preview plan validation, the local
  mock control/fault harness, and `.github/workflows/preview.yml`;
- `.agents/skills/wandb-ops` for live W&B run, metric, and table diagnosis;
- `.agents/skills/gcloud-ditto-readonly` for production DB reads and Targon
  rental logs;
- `.agents/skills/ditto-subnet-worktree` plus `.agents/skills/github` for
  isolation and stacked PRs;
- `.agents/skills/ditto-subnet-dependabot-review` for Dependabot and other
  dependency-PR security review;
- `.agents/skills/backroom-review` for quarantine triage, high-score ATH
  review, and precedents.

## Ownership map

| Surface | Path |
|---|---|
| Miner starter kit and local rehearsal | `miners/dittobench-starter-kit/`, `.agents/skills/mine/` |
| Miner CLI, validator, chain client | `ditto/` |
| Validator Compose and updater | `docker-compose.yml`, `scripts/` |
| Platform API, DB, dashboard | `apps/platform/` |
| Public subnet Backroom | `apps/backroom/` |
| DittoBench API and adapters | `services/dittobench-api/` |
| Screener capacity controller | `services/screener-orchestrator/` |
| Screener worker | `workers/screener/` |
| Shared screening protocol | `packages/ditto-screening-protocol/` |
| Benchmark datagen research | `research/dittobench-datagen/` |
| Shadow coding-repair research | `research/dittobench-coding-datagen/` |
| Terraform and Ansible | `infra/` |
| Release ownership graph | `release/components.toml` |
| Backroom quarantine and ATH review | `.agents/skills/backroom-review/` |
| Dependabot / dependency-PR review | `.agents/skills/ditto-subnet-dependabot-review/` |
| Preview channels and mock engine | `ditto/preview/`, `preview/` |

## Cross-component changes

Change every producer, consumer, generated artifact, migration, test, release
selector, and operational contract in one monorepo stack. Do not create a
repository dispatch, version-bump PR, or bot sync between directories.

The validator remains stateless; Platform owns durable queues, leases, policy,
and persistence. DittoBench owns versioned execution/scoring. Research adapters
must use normal score paths. Backroom is the public SN118 operator console and
must keep privileged tokens and OAuth state server-side.

When a live diagnosis cannot see a production knob through Backroom MCP, add
the read tool. Do not leave operator settings, review budgets, last-applied
worker state, or audit trails only on the UI. Err toward more Backroom
visibility.

## Delivery

Conventional commits drive semantic releases for affected components. Platform
API changes affect Backroom; dashboard-only changes do not. Application deploys
may follow releases automatically. The preview workflow (`ditto.preview`) does
not publish releases or `compat-2`; ordinary merged package changes still use
the release graph. Infrastructure always uses protected
Terraform plan/apply. Verify source, CI, merge, release, deployment, migration,
and live behavior as separate facts.

Never read or print provider secrets. Use Secret Manager or encrypted provider
bindings through consumers that do not return secret values.
