# infra/terraform/stacks/gcp-platform

Persistent GCP infrastructure for the Bittensor Subnet 118 Platform API in
[`ditto-assistant/ditto-subnet`](https://github.com/ditto-assistant/ditto-subnet)
under `apps/platform`.

Stands on its own (its own VPC + dedicated Postgres VM), independent of the
unapplied `gcp-prod` backend migration. The app runs as a **host process under
pm2/uv with a Pylon Docker sidecar** (not Cloud Run); host config + the first
boot are handled by the `platform_app` Ansible role.

## What this stack owns

| Resource | Notes |
| --- | --- |
| VPC `ditto-platform-net` + subnet (`10.30.0.0/24`) + Cloud NAT | `modules/network/gcp` |
| `allow-http` firewall (80/443 → `platform-http` tag) | app VM public ingress (Caddy) |
| `ditto-pg-platform` VM (`db-staging`, private) | holds `ditto_platform_dev` + `ditto_platform_prod` |
| `ditto-platform-dev` / `ditto-platform-prod` app VMs (`app-small`, public IP) | one per deploy env |
| `ditto-platform-agents-{dev,prod}` GCS buckets + one HMAC key | S3-interop blob storage |
| default-off private coding input + sealed evidence GCS buckets | isolated S3-compatible byte authorities with dedicated HMAC identities |
| Secret Manager: `platform-db-password`, `platform-storage-hmac-secret`, `platform-{dev,prod}-pylon-open-access-token`, `platform-github-deploy-key`, `platform-taostats-api-key` | values from `TF_VAR_*` (deploy key and optional Taostats key set out of band) |
| _(not here)_ `DITTO_UPLOAD_PAYMENT_ADDRESS` | deploy-time from the ditto-subnet repo's GitHub env secret |
| A records `platform-api[-dev].heyditto.ai` → app VM IPs (Cloudflare, DNS-only) | only when `manage_dns = true` |
| Hosted deploy identities (`identity.tf`, `trusted-builds.tf`) | exact protected `dev`/`prod` environment principals for `ditto-subnet` |
| Public builder/runtime registries | reviewed Kaniko executor and immutable screener release images |
| `ditto-validator-prod` (optional production GCP validator) | private Shielded VM, dedicated runtime SA, isolated hotkey secret container; activated only through `infra/docs/validator-gcp-production.md` |
| disposable validator hotkey admin | Terraform phases `absent` → `bootstrap` → `armed` → `absent`; no generator VM, disk, principal, or binding exists while idle |

**Self-contained:** the two platform SAs are created by this stack (`identity.tf`),
referencing the already-existing `github` WIF pool by name. Nothing in
`gcp-shared` (or any other env) needs to be applied first. State is **GCS-native**
(`gs://ditto-app-dev-tfstate`, prefix `gcp-platform`) — auth is ADC/WIF, no B2.

### Agent artifact retention

The agent buckets retain every current object without an age-based expiry.
This is intentional: source archives provide the legacy build fallback, and a
current screened image can be needed again while its agent is evaluating or
remains eligible for top-agent rescoring. Do not add a blanket current-object
TTL.

Artifact eligibility is owned by `ditto-platform`. The application deletes the
exact screened-image object when it becomes ineligible or is superseded; with
bucket versioning enabled, that deletion archives the previous generation.
Bucket lifecycle then provides storage hygiene without making eligibility
decisions:

- incomplete XML/S3 multipart uploads are aborted after one day;
- archived/noncurrent generations are deleted 30 days after becoming
  noncurrent (then remain recoverable for the bucket's GCS soft-delete window);
- current source archives and screened images have no lifecycle expiry.

Roll out the platform cleanup behavior before, or together with, applying this
Terraform change. A Terraform merge alone does not alter either bucket; the
`gcp-platform` stack must be deliberately planned and applied.

## Apply

Applied **deliberately** (not on every merge). Two paths:

The private coding authorities remain absent while
`enable_coding_s3_authorities = false`. Their first protected plan must review
the globally unique bucket names, retention periods, custom roles, HMAC secret
custody, project-wide Cloud Storage Data Access audit cost, and the project-wide
`storage.secureHttpTransport` policy. Do not lock the retention policies in the
same apply that first creates them; verify the provider behavior and recovery
runbook before a separately approved, irreversible lock operation.

- **Operator, locally (recommended for the first apply):** authenticate with
  your own ADC (`gcloud auth application-default login`), `cp
  terraform.tfvars.example terraform.tfvars`, fill it in (and export
  `TF_VAR_cloudflare_api_token` / `TF_VAR_db_password` /
  `TF_VAR_pylon_open_access_token`), then `terraform init && plan && apply`.
  Set `manage_dns = false` to apply everything except DNS before Cloudflare
  creds are ready (cloudflare vars then default to empty and the provider is
  never called).
- **CI dispatch:** `terraform-apply.yml` → run with env `gcp-platform`. This needs
  `GCP_WIF_PROVIDER` + `GCP_TF_DEPLOY_SA` GitHub secrets pointing at a SA with
  compute / network / secretmanager / storage admin, plus `PLATFORM_DB_PASSWORD`
  and `PLATFORM_PYLON_OPEN_ACCESS_TOKEN_JSON` (JSON, e.g. `{"dev":"…","prod":"…"}`).
  (The upload payment address is NOT here — it's deploy-time from the
  ditto-subnet repo's GitHub environment secret.)

## After apply

1. Note the outputs (`terraform output`): `pg_internal_ip`, `app_vm_names`,
   `agent_bucket_names`, `storage_hmac_access_id`, plus `wif_provider` +
   `platform_deploy_sa_email` (→ the ditto-subnet repo's `GCP_WIF_PROVIDER` /
   `GCP_PLATFORM_DEPLOY_SA` secrets).
   When coding storage is deliberately enabled, also retain the coding bucket
   and non-secret HMAC access-ID outputs. Secret material remains in Secret
   Manager and Terraform state and must never transit terminal output.
2. Provision the VMs with Ansible: `gcp-platform-pg.yml` (creates the two
   databases) and `gcp-platform-app.yml` (Docker + Pylon, Node/pm2, clones the
   repo, renders `.env`, Caddy). The app is **not** started here — the first
   deploy starts it (it needs the deploy-time payment address).

   The app play consumes several of the outputs above as environment variables
   and rewrites the whole `.env` on every run, so pass them all — a converge
   missing them writes placeholder/empty values and still reports success (it
   did, on prod, 2026-07-23). `infra/ansible/scripts/platform-app-env.sh` exports them
   from these outputs:

   ```sh
   export GCP_OSLOGIN_USER=…            # gcloud compute os-login describe-profile
   source infra/ansible/scripts/platform-app-env.sh
   ansible-playbook -i infra/ansible/inventory/gcp.yml \
     infra/ansible/playbooks/gcp-platform-app.yml --limit ditto-platform-prod
   ```

   `gcp-platform-pg.yml` additionally needs `DITTO_PG_PASSWORD` (the
   `platform-db-password` secret value); it asserts on it.
3. From then on, pushes to the ditto-subnet repo's `dev`/`main` branches deploy
   via that repo's `deploy.yml` (IAP SSH → `scripts/update.sh`). The update
   script reads the optional `platform-taostats-api-key` directly through the
   VM runtime service account; it does not transit GitHub Actions or SSH.

## SSH access (team)

SSH uses **OS Login over IAP** — each person logs in with their own Google
identity (sudo), no local accounts or passwords, and `:22` is never exposed to
the internet (the firewall allows it only from Google's IAP range).

**Grant access:** add the person to `ssh_users` (in `terraform.tfvars`) and
`terraform apply`:

```hcl
ssh_users = [
  "user:peyton@omniaura.ai",
  "user:nickanderson@omniaura.ai",
]
```

This grants `roles/compute.osAdminLogin` + `roles/iap.tunnelResourceAccessor`
on the three platform VMs (`ditto-pg-platform`, `ditto-platform-dev`,
`ditto-platform-prod`) — scoped to those instances, not the whole project.

**Connect** (the user needs `gcloud` + project `ditto-app-dev`):

```sh
gcloud compute ssh ditto-platform-dev \
  --project ditto-app-dev --zone us-central1-a --tunnel-through-iap
# sudo works (osAdminLogin); same for ditto-platform-prod / ditto-pg-platform
```

First connection provisions the POSIX account and SSH key automatically. No
keys to distribute, no passwords to rotate; revoke by removing the member from
`ssh_users` and re-applying.
