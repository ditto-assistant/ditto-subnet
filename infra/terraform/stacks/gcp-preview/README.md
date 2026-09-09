# SN118 PR preview infrastructure

This stack supplies the bounded cloud control plane for `stack` and
`stack-copy` PR previews. It creates no preview VM during Terraform apply.
The controller is dispatch-only -- a maintainer runs it by hand -- and creates
at most eight ephemeral VMs by atomically leasing one of the object names
`slots/0.json` through `slots/7.json`.

The VM runtime service account has no project roles. PR code therefore cannot
read Secret Manager, mutate GCP, reach production private addresses, or turn a
preview into a deployment identity. `stack-copy` receives a short-lived signed
URL for an already-sanitized dump; it never receives production database
credentials.

After this change reaches `main`, dispatch **Infrastructure plan or apply**
with operation `plan` and root `gcp-preview`. Record the printed plan SHA and
run id, then dispatch it again with operation `apply`, root `gcp-preview`, and
those exact values. The apply uses the sealed binary plan and the protected
`infra-apply` environment; do not apply this root from a workstation.

After apply, create a protected GitHub environment named `preview-stack` whose
deployment branch policy permits **only `main`**. This is load-bearing, in both
directions. The controller is dispatched manually and its OIDC subject is
`repo:<repo>:environment:preview-stack` -- scoped to the environment, not to a
ref -- so a policy any wider than `main` would let a dispatch from an arbitrary
branch run modified controller code with the cloud identity. A policy still
scoped to pull request refs, as earlier revisions of this file instructed, will
block every dispatch instead. Configure these environment variables from the
outputs:

- `GCP_PREVIEW_CONTROLLER_SERVICE_ACCOUNT`
- `GCP_WORKLOAD_IDENTITY_PROVIDER`
- `GCP_PREVIEW_LEASE_BUCKET`
- `GCP_PREVIEW_SNAPSHOT_BUCKET`
- `GCP_PREVIEW_NETWORK`
- `GCP_PREVIEW_SUBNETWORK`
- `GCP_PREVIEW_RUNTIME_SERVICE_ACCOUNT`
- `GCP_PREVIEW_ZONE`

These are optional and tune the fleet without a code change; every consumer
falls back to the default when the variable is absent or empty:

- `GCP_PREVIEW_MACHINE_TYPE` (default `e2-standard-8`, about $0.28/hour with
  the boot disk)
- `GCP_PREVIEW_DISK_SIZE` (default `100GB`)
- `PREVIEW_LEASE_TTL_SECONDS` (default `86400`, the absolute lease cap)
- `PREVIEW_CLOSED_GRACE_SECONDS` (default `14400`, how long a preview outlives
  its PR closing; `0` restores immediate retirement)

The `prod` environment also needs `GCP_PREVIEW_SNAPSHOT_BUCKET` for the
scheduled sanitizer. Infrastructure application and the first production
snapshot remain explicit protected operations; application workflows never
run Terraform.
