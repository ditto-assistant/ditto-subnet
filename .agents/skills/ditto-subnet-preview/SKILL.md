---
name: ditto-subnet-preview
description: >
  Resolve SN118 preview plans, run Foundry-style loopback cheatcodes and
  inference-fault injection, and drive the local DittoBench simulator
  (localstack / phase1) to score a harness. Use for overlay tests, warp_block,
  inject_provider, snapshot alignment, scored v12 local runs, mock controls, or
  .github/workflows/preview.yml. Preview `up` does not launch Platform,
  Backroom, a chain, or a validator.
---

# SN118 local simulator and Foundry overlay

Two local stacks, not one. Pick the surface, then load its reference.

| Need | Surface | Starts |
|---|---|---|
| Plan composition, overlay metagraph, warp time, leases, 429s | Preview-control | preview-control HTTP + fault proxy only |
| Composite / `tool_mean` / `memory_mean` on a real harness | Sessionless localstack | `dittobench-api` + `localstack-relay` |
| Scored v12 with `model_dependence` firing | Phase-1 localstack | Postgres + model-relay + TLS terminator + Linux broker container |
| Miner rehearsal of *their* kit | `$mine` `uv run ditto practice` | Miner process, not this skill |

Local `preview up` never launches Platform, dashboard, Backroom, a chain,
scorer, or validator. `compose` reports required URLs; it does not claim they
are live. PR `stack` and `stack-copy` plans are separately provisioned by the
trusted cloud controller after its Terraform stack is activated. No preview
workflow mints GitHub Releases, `v*` images, or `compat-2`.

Read [`references/cheatcodes.md`](references/cheatcodes.md) for the overlay.
Read [`references/localstack.md`](references/localstack.md) to score a harness.
Canonical command docs stay in `preview/README.md` and `localstack/README.md`.

## Compose first

```bash
./scripts/preview compose dashboard
./scripts/preview compose stack
./scripts/preview compose stack --attach-prod-api   # exits 2
./scripts/preview compose backroom                   # exits 2
```

| Selection | Isolated? | Validator |
|---|---|---|
| `dashboard` | no | may use prod Platform; this tool does not launch the SPA |
| `stack` | required | plan requires one localnet validator; not launched here |
| `stack-copy` | required | `stack` plus guarded snapshot alignment |

`stack` implies dashboard + Backroom URLs pointed at the *preview* Platform.
Backroom is never attached to production Platform. Refuse `--attach-prod-api`
with `stack` or `stack-copy`.

## Overlay (Foundry analog)

```bash
uv run python -m ditto.preview up stack --sha "$(git rev-parse HEAD)" --upstream http://127.0.0.1:11434
# stays foreground; writes a mode-0600 state file in the worktree
```

Second terminal in the **same worktree**:

```bash
./scripts/preview ctl register --hotkey 5EexQS8UxChmkZ6vGeacAkwcf3TARR1Go5rd684Mf69dwgTY --permit
./scripts/preview ctl warp_block --n 20
./scripts/preview ctl inject_provider --status 429
./scripts/preview ctl inject_provider --clear
```

Route localstack inference through those faults by exporting the printed
fault-proxy URL from `up`:

```bash
FAULT_PROXY_URL=<printed-fault-proxy-url> make localstack-up
```

Full catalog: [`references/cheatcodes.md`](references/cheatcodes.md).

## Score a harness

```bash
make localstack-smoke
make localstack-relay-check
make localstack-up
SCORED=0 AGENT_URL=http://127.0.0.1:7070 BENCH=12 RUN_SIZE=small SEED=42 make localstack-bench
```

Sessionless `SCORED=1` v9+ **cannot complete** (no broker session). Use
`SCORED=0` for tool/memory/composite on the v12 dataset, or phase-1 for
scored v12 gates. Procedure: [`references/localstack.md`](references/localstack.md).

## GitHub

Dispatch `.github/workflows/preview.yml` with `profiles` and an optional exact
SHA. It validates composition and the mock controls at that SHA. Dashboard-only
same-repo PRs may also publish a native `pages.dev` URL after the Pages project
and `preview` environment are activated; that publisher checks out the default
branch and never uses `workflow_run` or `pull_request_target`. PR selection
fails closed to `stack` for unknown runtime paths.

Same-repository PRs selected as `stack` or `stack-copy` are handled by
`.github/workflows/preview-stack.yml`. It posts dashboard, Platform, and
Backroom URLs after readiness. Eight atomic lease slots cap global active
previews; updates reuse a slot, while close and hourly TTL reconciliation tear
it down. `stack-copy` consumes only the latest artifact made by the protected
main-only sanitizer workflow. Applying `infra/terraform/stacks/gcp-preview`
and configuring the protected `preview-stack` and `prod` environments are
separate activation steps.

## Invariants

- Engine and control server accept only loopback chain endpoints and binds
  (`local` / `localnet` / `preview` / `dev`, `ws(s)://` loopback).
- `align_from_db` after a snapshot restore; the helper permits only the
  checked-in loopback Postgres identity in `preview/compose.yml`.
- Backroom never receives production OAuth/admin credentials in a preview.
- Do not put `OPENROUTER_API_KEY`, host Docker sockets, or Platform tokens
  into an untrusted miner image. Point the image at `localstack-relay`.
- The proof job stays green without cloud secrets.

## Validate

```bash
uv run pytest ditto/tests/preview -q
uv run python -m ditto.preview compose dashboard
make localstack-smoke
```
