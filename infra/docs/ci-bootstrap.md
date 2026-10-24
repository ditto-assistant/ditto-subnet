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

The dedicated subnet recovery root follows the same pattern: two exact-object
grants for `gcp-subnet-recovery/default.tflock` and its initial `default.tfstate`
are owned by `gcp-platform`. Its new-project CI authority requires the separate
owner bootstrap documented in
[`subnet-recovery-project.md`](subnet-recovery-project.md). Recovery secret
containers and the restore identity then use protected `gcp-subnet-recovery`
plan/apply. The owner bootstrap is not a public workflow root choice.

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

## AWS roots

AWS roots (today only `aws-generator-scorer`) use the same `infra-plan` and
`infra-apply` environments. GCP auth still seals and fetches the saved plan in
`gs://ditto-app-dev-tfstate/ci-plans/`; the plan and apply jobs then assume
`ditto-subnet-infra-plan` or `ditto-subnet-infra-apply` in account
`339713011188` through GitHub OIDC. The trust policies accept only the exact
`repo:ditto-assistant/ditto-subnet:environment:infra-plan` and
`...:environment:infra-apply` subjects. There are no AWS access keys and no new
environment secrets: the role ARNs are literal in the workflow. AWS roots get
none of the Platform, Cloudflare, fleet or hotkey inputs.

The state bucket, OIDC provider and both roles are created once by an account
administrator, never by the workflow. Use an SSO AdministratorAccess session,
not the root user:

```bash
infra/terraform/stacks/aws-bootstrap/create-state-bucket.sh          # prints the commands
infra/terraform/stacks/aws-bootstrap/create-state-bucket.sh --apply  # runs them
terraform -chdir=infra/terraform/stacks/aws-bootstrap init
terraform -chdir=infra/terraform/stacks/aws-bootstrap plan -out=bootstrap.tfplan
terraform -chdir=infra/terraform/stacks/aws-bootstrap apply bootstrap.tfplan
```

The plan should import the five bucket resources and create the bucket
lifecycle and TLS-only policy, the OIDC provider, and the two roles with their
inline policies. The plan role reads state, takes the lock and reads
the scorer's resources. The apply role can also write that state object, make
region-scoped EC2 network/instance changes, manage only the
`ditto-generator-scorer` role and instance profile (attaching only
`AmazonSSMManagedInstanceCore`), and manage only the scorer's SSM transfer
bucket. Widen it in this root, reviewed, when a new AWS root is added.

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
never `infra-apply` or `GCP_TF_APPLY_SA`. Peyton approves runs here. The
environment allows only the `main` branch (not tags).
Keep all Terraform and other protected infrastructure approvals unchanged.

The `gcp-platform` root owns `hippius-probe.tf`: a separate federation pool,
provider, service account, and a custom role bound separately to the six probe
secrets. Its only Secret Manager permissions are `versions.list` and
`versions.access`. The identity has no project roles, Terraform state access,
or service-account impersonation grants. Federation requires the exact repo
and owner IDs, workflow path, main branch, manual event, environment subject,
and Peyton's immutable actor ID.

Before enabling the environment, apply the independent review ruleset in
`infra/github/hippius-probe-ruleset.json`. It requires one approval from the
existing `admin` team for changes to the workflow, executable probe files,
isolated dependency lock, and delegation configuration. Admins retain emergency
bypass. GitHub owns this configuration; the
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

To revoke delegated access, remove the operator from the probe environment's
reviewers and remove the matching actor ID from the reviewed provider condition.
A provider disable revokes federation entirely. Neither action requires
widening `infra-apply`.
