---
name: ditto-subnet-preview
description: Resolve SN118 preview plans and run the loopback-only mock control/fault harness from a worktree or PR. Use for Foundry-style overlay tests, localstack fault injection, guarded snapshot alignment, or .github/workflows/preview.yml. This skill does not deploy a clickable Platform, Backroom, validator, or cloud preview.
---

# SN118 preview control harness

Same plan and mock-control checks locally and in GitHub Actions. The workflow
does not deploy resources or mint GitHub Releases, `v*` images, or `compat-2`.

## Compose first

```bash
./scripts/preview compose dashboard
./scripts/preview compose stack
```

| Selection | Isolated? | Validator |
|---|---|---|
| `dashboard` | no | may use prod Platform; this tool does not launch the SPA |
| `stack` | required | plan requires one localnet validator; not launched here |
| `stack-copy` | required | `stack` requirements plus guarded snapshot alignment |

Backroom is never attached to production Platform. Refuse `--attach-prod-api`
with `stack` or `stack-copy`.

## Local

```bash
uv run python -m ditto.preview up stack --sha "$(git rev-parse HEAD)"
# `up` starts only preview-control and the fault proxy; it stays foreground.
./scripts/preview ctl register --hotkey 5EexQS8UxChmkZ6vGeacAkwcf3TARR1Go5rd684Mf69dwgTY --permit
./scripts/preview ctl warp_block --n 10
./scripts/preview ctl inject_provider --status 429
```

Point localstack at the fault proxy: `HARNESS_GATEWAY_URL=$FAULT_PROXY_URL make localstack-up`.

## GitHub

Dispatch `.github/workflows/preview.yml` with `profiles` and an optional exact
SHA. It validates composition and the mock controls at that SHA; it does not
publish a URL. PR selection fails closed to `stack` for unknown runtime paths.

## Invariants

- Engine and control server accept only loopback chain endpoints and binds.
- `align_from_db` after a snapshot restore; do not copy prod onto a public unauthenticated hostname.
- Backroom never receives production OAuth/admin credentials in a preview.
- The proof job stays green without cloud secrets.
