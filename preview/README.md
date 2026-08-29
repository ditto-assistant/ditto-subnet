# SN118 preview control harness

This directory provides two related surfaces. Local `preview up` remains a
loopback-only mock control and inference fault proxy; it does **not** launch
Platform, dashboard, Backroom, a chain, scorer, or validator. Pull requests can
also receive bounded cloud `stack` and `stack-copy` deployments through the
trusted controller in `.github/workflows/preview-stack.yml`. Neither surface
publishes a release or `compat-2`.

## Profiles

| Profile | Contract |
|---|---|
| `dashboard` | Public dashboard plan; may attach to production Platform |
| `stack` | Isolated-stack requirements, including one localnet validator |
| `stack-copy` | `stack` requirements plus a sanitized Postgres snapshot |

Cloud stack previews run on credential-empty ephemeral GCE VMs. Eight atomic
lease objects are the global admission limit: an update reuses its PR's slot, a
ninth distinct PR fails closed, and close/TTL reconciliation deletes the VM.
The controller comments dashboard, Platform, and isolated Backroom URLs only
after readiness succeeds.

Backroom is an authenticated write control plane. It is only part of an
isolated `stack` plan and must never receive production OAuth, session, MCP, or
Platform admin credentials from preview code.

```bash
./scripts/preview compose dashboard
./scripts/preview compose stack
./scripts/preview compose stack --attach-prod-api   # exits 2
./scripts/preview compose backroom                   # exits 2
```

`compose` reports required/planned URLs. It does not claim they are live.

## Local mock controls

Start localstack separately if healthy forwarding is needed, then run:

```bash
uv run python -m ditto.preview up stack \
  --sha "$(git rev-parse HEAD)" \
  --upstream http://127.0.0.1:11434
```

`up` stays in the foreground and starts only preview-control and the fault
proxy. Press Ctrl-C to stop them. The worktree writes the random bearer token
to a mode-0600 state file, so a second terminal in the same worktree can run:

```bash
./scripts/preview ctl register \
  --hotkey 5EexQS8UxChmkZ6vGeacAkwcf3TARR1Go5rd684Mf69dwgTY --permit
./scripts/preview ctl warp_block --n 20
./scripts/preview ctl inject_provider --status 429
./scripts/preview ctl inject_provider --clear
./scripts/preview ctl align_from_db --json-path preview/fixtures/hotkeys.json
```

Set `FAULT_PROXY_URL` to the printed fault-proxy URL before starting the
localstack harness when inference should pass through injected faults.

## Snapshot guard

The helper only permits the checked-in loopback Postgres identity:

```bash
docker compose -f preview/compose.yml up -d
PREVIEW_DATABASE_URL=postgres://ditto:preview@127.0.0.1:5433/ditto_platform_preview \
  ./scripts/preview-restore-snapshot.sh /path/to/sanitized.dump
```

It runs destructive `pg_restore --clean`, so other hosts, ports, users, and
database names fail closed. Restoring a snapshot does not launch a stack or
connect the in-memory overlay to Platform/validator behavior.

Cloud `stack-copy` never reads production itself. The main-only
`preview-snapshot.yml` workflow exports into a disposable Postgres, applies the
explicit policy in `preview/cloud/sanitize.sql`, and uploads only the sanitized
custom-format dump. Authentication, payments, artifacts, source-review
evidence, bearer material, signatures, and operator identity are stripped. The
controller supplies the VM a two-hour signed URL; the source dump is never
uploaded and is removed from the runner even on failure.

## GitHub Actions

`.github/workflows/preview.yml` checks out one exact SHA, resolves a plan, and
runs the mock-control tests with read-only repository permissions. For a
dashboard-only PR it also builds an unprivileged exact-SHA static artifact and
calls `.github/workflows/preview-dashboard-publish.yml` in the same run. The
publisher checks out only the default branch, sanitizes the artifact, replaces
any artifact-supplied Worker with the trusted
`apps/platform/dashboard/preview/cloudflare-pages-worker.mjs`, and comments the
API-reported immutable `pages.dev` deployment URL on the PR. The stable Pages
branch is `pr-<number>`; each update replaces its branch alias while preserving
exact-SHA deployment metadata. Fork PRs never enter the publisher.

Same-repository PRs selected as `stack` or `stack-copy` use the narrowly
allowlisted `pull_request_target` controller. It checks out only the default
branch, re-reads the current PR head, acquires one of eight slots, then starts
the exact SHA on a separate VM. PR code never runs on the privileged runner.
The VM identity has no project roles and RFC1918 egress is denied. Close,
profile changes, failed readiness, and the 24-hour TTL all tear down the VM and
release its slot.

The preview proxy permits only `GET`/`HEAD` under `/api/v1/public/` and strips
cookies and authorization. Trusted response headers restrict connections,
forms, frames, referrers, and browser capabilities, but the page still contains
untrusted PR JavaScript and must never receive credentials. Miner
authentication, disputes, Backroom, and all other writes are deliberately
unavailable against production. Closing the PR publishes a trusted 410
tombstone to an existing stable branch alias, deletes the older deployments,
and leaves only that non-interactive tombstone because Cloudflare does not
permit deleting the latest deployment for a branch. Closing a PR that never
had a dashboard preview is a no-op. Closing, failing, or leaving the dashboard
profile retires any existing branch alias from the same run. If publication
wins the per-PR Pages queue first, the reconciler recognizes the already-current
exact SHA and leaves it live. Privileged jobs still refuse to execute PR code:
they check out the default branch for Wrangler, the Worker, and the tombstone.
A PR can still edit the caller workflow, so the `preview` environment token
must stay Pages-only.

Activation requires a protected `preview` GitHub environment whose deployment
branches include pull-request refs from this repository, with
`CLOUDFLARE_ACCOUNT_ID` as a variable and a Pages-only
`CLOUDFLARE_API_TOKEN` secret. Apply the `cloudflare-dittobench` Terraform stack
first. This publisher cannot prove itself from its own PR because inspect
copies the Worker from the current default branch. Open a fresh dashboard-only
PR after merge. This slice uses Cloudflare's native `pages.dev` preview URLs;
the custom `*.preview.dittobench.ai` router is a separate layer and must not be
created until it has this live origin to route to.
