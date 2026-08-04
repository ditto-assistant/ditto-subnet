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
plus create/read access only under the private
`gs://ditto-app-dev-tfstate/ci-plans/` prefix.

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
