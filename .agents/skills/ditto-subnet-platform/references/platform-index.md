# Platform and Backroom index

## Ownership

| Concern | Canonical paths |
|---|---|
| HTTP API and control plane | `apps/platform/ditto/api_server/` |
| Wire models | `apps/platform/ditto/api_models/` |
| Durable state and queries | `apps/platform/ditto/db/` |
| Schema history | `apps/platform/alembic/versions/` |
| Platform dashboard | `apps/platform/dashboard/` |
| Public operator console | `apps/backroom/` |
| Local OpenAPI generation | `apps/backroom/scripts/platform-contract/generate.sh` |
| Shadow coding harness launch | `apps/platform/ditto/api_server/endpoints/validator_coding_harness.py`, `apps/platform/docs/coding-harness-launch-authority.md` |
| Shadow coding revocation adapter | `services/dittobench-api/internal/codinggrantrevoke/`, `services/dittobench-api/docs/coding-private-runtime-adapters-shadow.md` |
| Affected-component graph | `release/components.toml` |
| Production DB and Targon logs (read-only) | `.agents/skills/gcloud-ditto-readonly/` |

## High-value lookups

```bash
rg -n 'APIRouter|@router|create_api_server' apps/platform/ditto/api_server
rg -n 'down_revision|safe_add_column|safe_drop_column' \
  apps/platform/alembic apps/platform/ditto/db
rg -n 'admin.service|platform-api|DITTO_ADMIN_API_TOKEN' apps/backroom/src
rg -n 'createRoute|useQuery|fetch\(' apps/platform/dashboard/src
```

Read `apps/platform/CLAUDE.md` for migration and test invariants and `apps/backroom/AGENTS.md` for Worker/auth boundaries.

## Validation

API or DB:

```bash
cd apps/platform
python scripts/check_migration_order.py origin/main
make lint lint-copy typecheck test
```

Dashboard:

```bash
cd apps/platform/dashboard
pnpm check
pnpm test
pnpm build
```

Backroom:

```bash
cd apps/backroom
pnpm check
pnpm test
pnpm build
```

Contract change:

```bash
apps/backroom/scripts/platform-contract/generate.sh
git diff --check
```

## Invariants

- Platform owns queueing, leases, admission, retries, administrative policy, and persistence.
- Validator and screener processes remain outside `apps/platform` and call it over explicit protocols.
- Alembic has one head. Hot tables use the repository safe migration helpers and application lock order.
- Backroom is public subnet operations only; private Ditto app operations stay in the private product Backroom.
- Authenticated Worker responses are `no-store`; writes require same-origin protection.
- API changes mark `platform_api`, `platform`, and `backroom` affected. Dashboard changes mark `platform_dashboard` and `platform` only.
