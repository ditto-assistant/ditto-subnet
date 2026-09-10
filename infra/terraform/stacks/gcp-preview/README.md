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
- `GCP_PREVIEW_IMAGE_FAMILY` and `GCP_PREVIEW_IMAGE_PROJECT` (default stock
  `ubuntu-2404-lts-amd64` from `ubuntu-os-cloud`); see below

The `prod` environment also needs `GCP_PREVIEW_SNAPSHOT_BUCKET` for the
scheduled sanitizer. Infrastructure application and the first production
snapshot remain explicit protected operations; application workflows never
run Terraform.

## The `sn118-preview-base` image

**Bake preview base image** (`.github/workflows/preview-base-bake.yml`) runs
nightly and on dispatch. It boots one throwaway VM on stock Ubuntu, lets
`preview/cloud/startup.sh` install the toolchain and hand off to
`preview/cloud/bake.sh`, then captures the boot disk as a custom image in the
`sn118-preview-base` family and prunes all but the newest three.

Two properties are deliberate and should not be "simplified":

- The bake VM runs the **same** `startup.sh` a preview does. There is no second
  install path to keep in sync, so the image cannot drift from what a preview
  boot expects, and `startup.sh`'s own toolchain guard is what makes the baked
  image skip apt on the next boot.
- The bake VM runs with `--no-service-account --no-scopes`. The image is
  captured from outside by the workflow's identity, so nothing on the guest
  needs cloud access; the bake identity therefore needs no `actAs` grant and the
  preview subnet stays credential-empty.

After apply, create a second protected environment named `preview-bake`, also
scoped to **only `main`**, for exactly the reason given above -- its OIDC
subject is `repo:<repo>:environment:preview-bake`, so a wider policy would let
any branch run modified bake code with an identity that can create VMs and
images. Set `GCP_PREVIEW_BAKE_SERVICE_ACCOUNT` from the `bake_service_account`
output, plus `GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_PREVIEW_NETWORK`,
`GCP_PREVIEW_SUBNETWORK`, and `GCP_PREVIEW_ZONE`.

To put previews on the baked image, set `GCP_PREVIEW_IMAGE_FAMILY` to
`sn118-preview-base` and `GCP_PREVIEW_IMAGE_PROJECT` to this project on the
`preview-stack` environment. Unsetting them reverts to stock Ubuntu with no
other change.

**A baked image raises the floor on `GCP_PREVIEW_DISK_SIZE`.** A custom image
reports `diskSizeGb` equal to the disk it was captured from, and
`gcloud compute instances create --boot-disk-size` below that is a hard error,
not the informational warning stock Ubuntu's 10GB image produces. The bake disk
defaults to 32GB (`PREVIEW_BAKE_DISK_SIZE` on the `preview-bake` environment),
so keep `GCP_PREVIEW_DISK_SIZE` at or above it. The other bake knobs are
`PREVIEW_BAKE_MACHINE_TYPE` (default `e2-standard-8`) and
`PREVIEW_BAKE_KEEP_IMAGES` (default `3`).
