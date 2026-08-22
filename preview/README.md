# SN118 preview control harness

This directory provides preview plan validation plus a loopback-only mock
control and inference fault proxy. The same checks run locally and in GitHub
Actions. It does **not** launch or deploy Platform, dashboard, Backroom, a
chain, scorer, or validator, and it never publishes a release or `compat-2`.

## Profiles

| Profile | Contract |
|---|---|
| `dashboard` | Public dashboard plan; may attach to production Platform |
| `stack` | Isolated-stack requirements, including one localnet validator |
| `stack-copy` | `stack` requirements plus a sanitized Postgres snapshot |

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

## GitHub Actions

`.github/workflows/preview.yml` checks out one exact SHA, resolves a plan, and
runs the mock-control tests with read-only repository permissions. For a
dashboard-only PR it also builds an unprivileged exact-SHA static artifact.

After that workflow completes, the default-branch
`.github/workflows/preview-dashboard-publish.yml` downloads the artifact and
publishes it to the Terraform-owned `ditto-subnet-dashboard-preview` Cloudflare
Pages project. The privileged job never checks out PR code, replaces any
artifact-supplied Worker with the trusted
`apps/platform/dashboard/preview/cloudflare-pages-worker.mjs`, and comments the
API-reported immutable `pages.dev` deployment URL on the PR. The stable Pages
branch is `pr-<number>`; each update replaces its branch alias while preserving
exact-SHA deployment metadata.

The preview proxy permits only `GET`/`HEAD` under `/api/v1/public/` and strips
cookies and authorization. Trusted response headers restrict connections,
forms, frames, referrers, and browser capabilities, but the page still contains
untrusted PR JavaScript and must never receive credentials. Miner
authentication, disputes, Backroom, and all other writes are deliberately
unavailable against production. Closing the PR publishes a trusted 410
tombstone to an existing stable branch alias, deletes the older deployments,
and leaves only that non-interactive tombstone because Cloudflare does not
permit deleting the latest deployment for a branch. Closing a PR that never
had a dashboard preview is a no-op. A trusted `pull_request_target` path from
the default branch retires the previous URL on every PR update before a new
artifact can publish. It never checks out or executes PR code, so changing,
breaking, or deleting the PR-owned source workflow cannot strand an old
preview. If publication wins the queue first, the reconciler recognizes the
already-current exact SHA and leaves it live.

Activation requires a protected `preview` GitHub environment with
`CLOUDFLARE_ACCOUNT_ID` as a variable and a Pages-only
`CLOUDFLARE_API_TOKEN` secret. Apply the `cloudflare-dittobench` Terraform stack
first. This slice uses Cloudflare's native `pages.dev` preview URLs; the custom
`*.preview.dittobench.ai` router is a separate layer and must not be created
until it has this live origin to route to.
