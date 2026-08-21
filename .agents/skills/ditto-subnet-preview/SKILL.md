---
name: ditto-subnet-preview
description: Spin isolated SN118 preview channels from a worktree or PR — dashboard/backroom against prod Platform, or stack/stack-copy with one localnet validator and Foundry-style cheatcodes. Use when the user wants a preview URL, localstack with injected faults, stack-copy migrations, or to trigger .github/workflows/preview.yml.
---

# SN118 preview channels

Same cranks locally and in GitHub Actions. Previews never mint GitHub Releases, `v*` images, or `compat-2`.

## Compose first

```bash
./scripts/preview compose dashboard,backroom
./scripts/preview compose stack
```

| Selection | Isolated? | Validator |
|---|---|---|
| `dashboard` and/or `backroom` | no | none; frontends use prod Platform |
| `stack` | yes | one localnet validator |
| `stack-copy` | yes | `stack` plus snapshot + `align_from_db` |

Refuse preview Platform + prod validators (`--attach-prod-api` with `stack`).

## Local

```bash
uv run python -m ditto.preview up stack --sha "$(git rev-parse HEAD)"
export PREVIEW_CONTROL_URL=…   # printed under urls.control
./scripts/preview ctl register --hotkey 5EexQS8UxChmkZ6vGeacAkwcf3TARR1Go5rd684Mf69dwgTY --permit
./scripts/preview ctl warp_block --n 10
./scripts/preview ctl inject_provider --status 429
```

Point localstack at the fault proxy: `HARNESS_GATEWAY_URL=$FAULT_PROXY_URL make localstack-up`.

## GitHub

Dispatch `.github/workflows/preview.yml` with `profiles` and optional `sha`. Path-based PR defaults: dashboard/backroom files → those profiles against prod; API/alembic/validator → `stack` (alembic → `stack-copy`).

## Invariants

- Engine refuses `finney` and OpenTensor endpoints.
- `align_from_db` after a snapshot restore; do not copy prod onto a public unauthenticated hostname.
- Tear down on PR close; cheatcode proof job must stay green without cloud secrets.
