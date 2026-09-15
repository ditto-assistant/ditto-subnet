# Infrastructure CI bootstrap

The public workflow cannot create the identity that authorizes its own first
run. Before enabling plans, an organization administrator must complete the
one-time private bootstrap in `ditto-assistant/infra` PR #70 (or an equivalent
audited setup) and configure these protected GitHub environments:

Before the first public plan, complete the single-writer state handoff in
[`tfstate-gcs-migration.md`](tfstate-gcs-migration.md). In particular, disable
the private repository's plan/apply workflows before enabling this repository;
GCS locking prevents concurrent state writes but cannot make plans from two
different configurations safe.

## `infra-plan`

- deployment branches: protected `main` only
- `GCP_WIF_PROVIDER`
- `GCP_TF_PLAN_SA`
- `PLATFORM_DB_PASSWORD`
- `PLATFORM_PYLON_OPEN_ACCESS_TOKEN_JSON`
- `CLOUDFLARE_API_TOKEN`
- `CLOUDFLARE_ACCOUNT_ID`
- `CLOUDFLARE_HEYDITTO_ZONE_ID`
- `CLOUDFLARE_DITTOBENCH_ZONE_ID`

The plan identity needs read access to Terraform state and managed resources,
create-only access under the private
`gs://ditto-app-dev-tfstate/ci-plans/` prefix, and create/delete access only to
the exact `gcp-platform/default.tflock`, `gcp-preview/default.tflock`, and
`cloudflare-dittobench-ai/default.tflock` objects. It also has create-only
access to the exact initial `gcp-preview/default.tfstate` object because the GCS
backend creates an empty state during its first initialization; it cannot
overwrite or delete that object. The preview grants are managed by the
already-live `gcp-platform` root so the preview root does not require an
out-of-band bootstrap.

## `infra-apply`

- deployment branches: protected `main` only
- required reviewer(s)
- the same Cloudflare and non-secret variables
- `GCP_WIF_PROVIDER`
- `GCP_TF_APPLY_SA`
- `CLOUDFLARE_API_TOKEN`

The apply identity needs the scoped mutation roles represented by the selected
Terraform root and read/delete access to the private plan prefix. Do not grant
either identity Secret Manager payload access merely to run Terraform; values
needed by configuration enter through protected environment secrets.

To apply, copy the exact SHA and run id printed by a successful plan workflow.
The apply workflow rejects a plan unless that SHA is still the current `main`,
verifies the private plan checksum, and applies the saved binary rather than
replanning. A successful apply deletes the consumed plan objects.

The first post-cutover `gcp-platform` plan must show removal of the legacy
`ditto-platform` and `ditto-screener` WIF principals. The default
`platform_deploy_repos` contains only `ditto-subnet`; do not restore archived
repository principals except for a time-bounded rollback, with the removal in
the same change plan.

## `preview`

- deployment branches must include pull-request refs from this repository
  (do not restrict to protected `main`; the publisher is a same-run
  `pull_request` job). Fork pull requests never receive environment secrets.
- optional required reviewer(s) for the first activation
- variable `CLOUDFLARE_ACCOUNT_ID`
- secret `CLOUDFLARE_API_TOKEN`

This environment belongs only to the dashboard publisher jobs. Its token needs
Cloudflare Pages Write for the Ditto account and no Workers Scripts, KV, DNS,
or zone permissions. Do not add it to unprivileged `plan`, `cheatcodes`, or
`dashboard-bundle` jobs. A Pages-only token is the blast-radius limit: a
collaborator can edit the PR-owned caller workflow.

Activate dashboard URLs in this order: merge the publisher, review and apply
the `cloudflare-dittobench` Terraform plan that creates the Pages project,
create this environment, then open or synchronize a fresh dashboard-only PR.
The publisher cannot prove itself from its own PR because inspect copies the
trusted Worker from the current default branch.

## `coding-hippius-probe`

The manual Hippius capability probe uses its own environment and GCP identity,
never `infra-apply` or `GCP_TF_APPLY_SA`. `ai-mountain` and Peyton can approve
their own runs here. The environment allows only the `main` branch (not tags).
Keep all Terraform and other protected infrastructure approvals unchanged.

The `gcp-platform` root owns `hippius-probe.tf`: a separate federation pool,
provider, service account, and a custom role bound separately to the six probe
secrets. Its only Secret Manager permissions are `versions.list` and
`versions.access`. The identity has no project roles, Terraform state access,
or service-account impersonation grants. Federation requires the exact repo
and owner IDs, workflow path, main branch, manual event, environment subject,
and Peyton or ai-mountain's immutable actor ID.

Before enabling the environment, apply the independent review ruleset in
`infra/github/hippius-probe-ruleset.json`. It requires one approval from the
existing `admin` team for changes to the workflow, executable probe files,
isolated dependency lock, and delegation configuration. Admins retain emergency
bypass; ai-mountain has no ruleset bypass. GitHub owns this configuration; the
JSON files record the exact reproducible API payloads. Do not change the
repository-wide CODEOWNERS or review requirements for unrelated files.

Activation order:

1. Merge the reviewed change and create a protected exact-current-main plan
   for the resources in `hippius-probe.tf`. Inspect it before applying the
   saved binary through `infra-apply`; never create this IAM out of band.
2. Install the scoped ruleset, then create/update the environment using
   `infra/github/hippius-probe-environment.json`; add a deployment branch
   policy with `name=main`, `type=branch`.
3. Set environment variables `GCP_HIPPIUS_PROBE_SA` and
   `GCP_HIPPIUS_PROBE_WIF_PROVIDER` to the corresponding Terraform outputs.
   These identifiers are not secrets. Do not copy infrastructure credentials.
4. Dispatch the main workflow with its exact confirmation, approve only the
   probe environment, and verify the synthetic-only, weight-ineligible
   receipt and provider profile. Each successful run retains two synthetic
   4 KiB objects. This does not activate a worker, release, or scoring.

The job installs only hash-locked wheels and copies the reviewed probe module
into a temporary package. Python isolated mode prevents checkout packages,
package initializers, and sitecustomize from executing. The ordinary Platform
project and its build hooks are not installed. Artifact upload requires the
probe step to succeed, including checking all three output files for secret
bytes. Failed probes or sanitization produce no uploaded artifact.

To revoke delegated access, remove ai-mountain from the probe environment's
reviewers and remove his actor ID from the reviewed provider condition. A
provider disable revokes federation entirely. Neither action requires widening
`infra-apply`.

## `coding-hosted-operate`

The manual native coding host workflow runs fixed, reviewed operations on
`ditto-coding-hosted-v2` only. Its sole operation today is `verify`, a read-only
check piped from the exact checked-out revision: host identity, egress unit,
inactive rootful Docker, the rootless daemon through the preinstalled non-root
`host-policy.py verify`, the custody public-key SPKI, and lstat-only metadata for
the private key, key receipt, runtime install, image imports and the not yet
provisioned custody-owned and worker-owned PostgreSQL environment copies (each
reader requires its own `0600` file in a `0700` directory it owns). It never opens private material and has
no command, path, host, project or revision inputs.

`host-policy.py verify` is the first-provisioning check and requires an empty
daemon, so it can only pass before images are imported. Once a root-owned mode
`0600` import receipt exists under a root-sealed
`/opt/ditto-coding-hosted-images/<approval>/`, the verifier reports that check as
not applicable instead of failing. Ownership and mode of the daemon home, socket
directory and socket stay required in both phases. Imported image identities are
the custodian's post-import preflight
([coding-native-host-preflight-v2.md](coding-native-host-preflight-v2.md)), not
this workflow. Any receipt that isn't sealed that way counts as no import, so the
empty-daemon check stays required.

The identity is root-capable on that one host (OS Login admin), so the job always
uses the `coding-hosted-operate` environment with `prevent_self_review: true`:
whoever dispatches cannot approve. The same identity must never serve an
approval-free job. An approval-free verify would need a separate genuinely
non-root identity calling a preinstalled bounded verifier. Mutating host
operations (runtime install, custody convergence, PostgreSQL environment
materialization) are separate reviewed changes and are not in this workflow.

The `gcp-platform` root owns `coding-hosted-operate.tf`: a separate federation
pool, provider and service account. The account has no Secret Manager, storage,
or Terraform state access. Its only grants come from the `coding-hosted-host`
module's `workflow_operator` input: instance OS Login admin, IAP tunnel access
conditioned on the host's private IP and port 22, actAs on the host's
telemetry-only service account, and the custom project role
`codingHostedWorkflowProjectGet`. That role holds only `compute.projects.get`.
Google requires that permission at project level for `gcloud compute ssh` when
OS Login is granted per instance. It is not `roles/compute.viewer`, and it reads
project metadata only: no instance listing, start/stop, or SSH to other hosts. Federation requires the exact repo and
owner IDs, workflow path, main branch, manual event, environment subject, and
Peyton or ai-mountain's immutable actor ID.

Activation order:

1. Install `infra/github/coding-hosted-operate-ruleset.json`. It requires one
   `admin` team approval, from someone other than the last pusher, for the
   workflow, verifier, delegation JSON, and the host module and stack files.
2. Merge the reviewed change. Set `enable_coding_hosted_operate_workflow = true`
   in a separate reviewed change, then create a protected exact-current-main
   plan and inspect the IAM before applying through `infra-apply`. Never create
   this IAM out of band.
3. Create the environment from
   `infra/github/coding-hosted-operate-environment.json` and add a deployment
   branch policy with `name=main`, `type=branch`.
4. Set environment variables `GCP_CODING_HOSTED_OPERATE_SA` and
   `GCP_CODING_HOSTED_OPERATE_WIF_PROVIDER` to the Terraform outputs. They are
   not secrets.
5. Dispatch `verify` with `OPERATE CODING HOST verify`; the other reviewer
   approves. The job summary publishes only pass/fail, counts and path
   metadata, never host identity records, revisions or approvals.

To revoke, set the flag back to false and apply (removing the host grants and
the project-read role; GCP soft-deletes the role and Terraform undeletes it on
re-enable),
disable the provider, or remove the reviewer from the environment.
