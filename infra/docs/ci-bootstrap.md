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

AWS roots (`aws-generator-scorer` and `aws-screener`) use the same `infra-plan` and
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
inline policies. The plan role reads each AWS root's state, takes its lock and
reads its resources. The apply role can also write those state objects, make
region-scoped EC2 network/instance changes on resources carrying an
`aws-generator-scorer` or `aws-screener` `stack` tag, read the
`ditto-generator-scorer` and `ditto-screener-node` roles and instance profiles,
pass those two roles to EC2 only, and manage only the scorer's SSM transfer
bucket. It can write no IAM role or instance profile. Widen it in this root,
reviewed, when a new AWS root is added.

### Generator scorer identity

`aws-bootstrap` (`generator-scorer-identity.tf`), not the CI-applied
`aws-generator-scorer` root, owns the `ditto-generator-scorer` instance role
(EC2 trust, only `AmazonSSMManagedInstanceCore` attached) and its instance
profile. GCP federation hands the scorer's Platform bearer to every session of
that role name, so the CI apply role is explicitly denied every IAM write on
the role and profile (create, delete, update, retrust, tag, policy and
permissions-boundary changes, instance-profile membership); it cannot replace
the role with one it trusts. The security groups it can change are only the
ones it created with a stack tag: it can tag a security group only in the
call that creates it, so neither AWS root manages its VPC's default group.

Create the identity before the first `aws-generator-scorer` apply, with an SSO
administrator session:

```bash
terraform -chdir=infra/terraform/stacks/aws-bootstrap plan -out=bootstrap.tfplan \
  -var enable_generator_scorer_identity=true
terraform -chdir=infra/terraform/stacks/aws-bootstrap apply bootstrap.tfplan
```

Expect only the role, its SSM policy attachment and the instance profile. Keep
the value in every later bootstrap plan (or the identity is destroyed). The
scorer root reads the profile by name and fails its plan unless the profile
wraps `ditto-generator-scorer`.

### On-demand screener identities

`aws-bootstrap` (`screener.tf`), not the CI-applied `aws-screener` root, owns
the screener's two IAM roles, so no workflow run can change which instance role
GCP trusts or which Google identity may start the instance. The CI roles are
denied `iam:UpdateAssumeRolePolicy` on both roles and every IAM write on them.

- `ditto-screener-node` and its instance profile (`enable_aws_screener`): EC2
  trust, only `AmazonSSMManagedInstanceCore` attached. GCP's `screener-aws`
  pool (`gcp-platform/screener-aws.tf`) maps its sessions to a principal set
  that reads the screener review keys and the `screener-aws-registration-token`
  grant.
- `ditto-screener-capacity-wake` (`screener_capacity_controller_unique_id`):
  trusts `accounts.google.com` for exactly the capacity controller service
  account (`:aud` = its unique ID, the token's `azp`; `:sub` = the same ID;
  `:oaud` = `ditto-screener-capacity`, the token's audience). It may call
  `ec2:DescribeInstances` and `ec2:StartInstances` on `stack = aws-screener`
  instances in `us-east-1`, nothing else.

Enabling the screener is a sequence of reviewed steps; each is inert until the
next:

1. Protected `gcp-platform` apply with `enable_screener_aws_identity = true`.
   Read outputs `screener_capacity_controller_unique_id` and
   `screener_aws_wif_provider`. An operator adds the single-use Platform
   registration grant as the one version of `screener-aws-registration-token`.
2. Re-apply this bootstrap locally with an SSO administrator session (the CI
   roles cannot create these identities):

   ```bash
   terraform -chdir=infra/terraform/stacks/aws-bootstrap plan -out=bootstrap.tfplan \
     -var enable_aws_screener=true \
     -var screener_capacity_controller_unique_id=<numeric unique id>
   terraform -chdir=infra/terraform/stacks/aws-bootstrap apply bootstrap.tfplan
   ```

   Expect only the two roles, the instance profile, the SSM attachment, the
   wake role's inline policy, and in-place updates to the two CI role policies.
   Keep both values in every later bootstrap plan (or the roles are destroyed),
   together with `enable_generator_scorer_identity=true` once the generator
   scorer identity exists.
3. Protected `aws-screener` plan/apply with `enable_aws_screener = true`,
   `screener_fleet_release_sha` (a semantic-release commit),
   `screener_fleet_builder_image` (digest-pinned) and
   `screener_fleet_aws_wif_provider` set in that root's reviewed tfvars. First
   boot checks out that exact commit and runs
   `infra/ansible/playbooks/aws-screener-node.yml` against localhost; progress
   is in `/var/log/ditto-screener-bootstrap.log` (reach it over SSM).

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
