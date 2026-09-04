# SN118 PR preview infrastructure

This stack supplies the bounded cloud control plane for `stack` and
`stack-copy` PR previews. It creates no preview VM during Terraform apply.
The trusted default-branch controller creates at most eight ephemeral VMs by
atomically leasing one of the object names `slots/0.json` through
`slots/7.json`.

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

After apply, create a protected GitHub environment named `preview-stack`
which permits only this repository's pull request refs. Configure these
environment variables from the outputs:

- `GCP_PREVIEW_CONTROLLER_SERVICE_ACCOUNT`
- `GCP_WORKLOAD_IDENTITY_PROVIDER`
- `GCP_PREVIEW_LEASE_BUCKET`
- `GCP_PREVIEW_SNAPSHOT_BUCKET`
- `GCP_PREVIEW_NETWORK`
- `GCP_PREVIEW_SUBNETWORK`
- `GCP_PREVIEW_RUNTIME_SERVICE_ACCOUNT`
- `GCP_PREVIEW_ZONE`

The `prod` environment also needs `GCP_PREVIEW_SNAPSHOT_BUCKET` for the
scheduled sanitizer. Infrastructure application and the first production
snapshot remain explicit protected operations; application workflows never
run Terraform.
