variable "project" {
  description = "GCP project ID (ditto-app-dev)."
  type        = string
  default     = "ditto-app-dev"
}

variable "region" {
  description = "GCP region."
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "GCP zone for the Postgres + app VMs."
  type        = string
  default     = "us-central1-a"
}

variable "subnet_cidr" {
  description = "Primary CIDR for the platform subnet. A /24 holds the PG VM + both app VMs comfortably. Kept off 10.10.0.0/24 (gcp-prod's ditto-net) to avoid collisions if both VPCs ever peer."
  type        = string
  default     = "10.30.0.0/24"
}

variable "cloudflare_api_token" {
  description = "Cloudflare API token with Zone:DNS:Edit on heyditto.ai (provide via TF_VAR_cloudflare_api_token). Only needed when manage_dns = true; empty is fine for a no-DNS apply."
  type        = string
  sensitive   = true
  default     = ""
}

variable "cloudflare_zone_id" {
  description = "Cloudflare Zone ID for heyditto.ai. Only needed when manage_dns = true."
  type        = string
  default     = ""
}

# --- Postgres VM ---

variable "pg_data_disk_gb" {
  description = "Postgres data disk size in GB (separate, survives instance replacement)."
  type        = number
  default     = 50
}

variable "vm_service_account_email" {
  description = "Runtime SA for the Postgres VM (Secret Manager, logging). Empty uses the default compute SA."
  type        = string
  default     = ""
}

# --- App VMs ---

variable "app_boot_disk_gb" {
  description = "Boot disk size in GB for the app VMs (checkout, uv cache, Docker, pm2 logs, relay trace spool). 30G filled production; keep at least 100G. google provider 6.x treats inline boot-disk size as ForceNew — grow live disks first, then pin this value."
  type        = number
  default     = 100
}

variable "run_service_account_email" {
  description = "Optional override for the runtime SA the app VMs run as. Empty (default) uses the ditto-platform SA this stack creates in identity.tf — leave it empty in normal use."
  type        = string
  default     = ""
}

variable "platform_dedicated_identity_attached" {
  description = "Attach the inference-capable dedicated platform API identity to app VMs. Stage its IAM first; enabling this may restart the VMs."
  type        = bool
  default     = true
}

variable "validator_dedicated_identity_attached" {
  description = "Attach the provider-secret-free identity to the optional reference/dev GCE validator after legacy v6 work is drained. This flag does not mutate the production DigitalOcean validator. Enabling it may restart the GCE VM."
  type        = bool
  default     = false
}

# --- Identity (the github WIF pool already exists; we only reference it) ---

variable "platform_deploy_repos" {
  description = "owner/repo list allowed to impersonate the platform deploy SA via the existing `github` WIF pool. The monorepo is the sole default; temporarily add a legacy repo only for an explicitly bounded rollback window."
  type        = list(string)
  default     = ["ditto-assistant/ditto-subnet"]
}

variable "dittobench_cloud_run_service_name" {
  description = "Existing hosted DittoBench Cloud Run service whose deploy identity receives service-scoped run.admin."
  type        = string
  default     = "dittobench-api"
}

variable "platform_deploy_sa_roles" {
  description = "Project roles on github-actions-platform-deploy. Minimal IAP/OS-Login set: the deploy IAP-SSHes into the app VMs and runs scripts/update.sh."
  type        = list(string)
  default = [
    "roles/iap.tunnelResourceAccessor",
    "roles/compute.osAdminLogin",
    "roles/compute.viewer",
  ]
}

variable "screener_bake_sa_roles" {
  description = "Project roles on github-actions-screener-bake. Packer spins a temporary build VM and writes an image: instanceAdmin.v1 (create/delete VM + image) and iap.tunnelResourceAccessor (IAP SSH to the build VM). actAs on the build VM's default compute SA is granted separately, scoped to that one SA (not project-wide serviceAccountUser). No Secret Manager — the bake stores no credentials in the image."
  type        = list(string)
  default = [
    "roles/compute.instanceAdmin.v1",
    "roles/iap.tunnelResourceAccessor",
  ]
}

variable "wif_pool_id" {
  description = "Existing Workload Identity Pool id used for GitHub OIDC (created out of band; managed in envs/gcp-shared)."
  type        = string
  default     = "github"
}

variable "wif_provider_id" {
  description = "Existing WIF provider id under the pool (for the wif_provider output the deploy workflow consumes)."
  type        = string
  default     = "github-actions"
}

variable "platform_deploy_environment" {
  description = "GitHub Actions environment the deploy workflows run in (their OIDC `sub` is `repo:<owner>/<repo>:environment:<name>`). The WIF binding is scoped to this environment so only prod-environment jobs — gated by that environment's deployment-branch protection (main only) — can impersonate the sudo-capable deploy SA, not every branch in the repo. Each repo MUST configure this environment with deployment-branch restrictions."
  type        = string
  default     = "prod"
}

# --- Object storage ---

variable "bucket_location" {
  description = "Location for the agent-tarball GCS buckets."
  type        = string
  default     = "US"
}

# --- Database / app secrets (sensitive; provide via TF_VAR_*) ---

variable "db_password" {
  description = "Password for the `ditto` Postgres app role on the platform PG VM (provide via TF_VAR_db_password). Same role serves both databases."
  type        = string
  sensitive   = true
}

variable "pylon_open_access_token" {
  description = "Per-env Pylon open-access token shared by the API and the Pylon sidecar (PYLON_OPEN_ACCESS_TOKEN)."
  type        = map(string)
  sensitive   = true
}

# --- DNS ---

variable "api_domain_prod" {
  description = "Prod API hostname (A record -> prod app VM public IP)."
  type        = string
  default     = "platform-api.heyditto.ai"
}

variable "api_domain_dev" {
  description = "Dev API hostname (A record -> dev app VM public IP)."
  type        = string
  default     = "platform-api-dev.heyditto.ai"
}

variable "manage_dns" {
  description = "When true, create the Cloudflare A records pointing the platform hostnames at the app VM IPs. Set false to stand up the VMs first and point DNS later."
  type        = bool
  default     = true
}

variable "ssh_users" {
  description = "Team members granted sudo SSH (OS Login + IAP) on the platform VMs. Full IAM members. See README.md for how to connect. NOTE: members outside the org domain (e.g. @resilabs.ai) ALSO need org-level roles/compute.osLoginExternalUser (an org-admin grant) before OS Login works for them."
  type        = list(string)
  default = [
    "user:nickanderson@omniaura.ai",
    "user:peyton@omniaura.ai",
    "user:omar@omniaura.ai",
    "user:brian@omniaura.ai",
  ]
}

variable "debug_operators" {
  description = "Operators granted the minimum GCP IAM used by repo debug skills: unconditioned compute.viewer (gcloud compute ssh needs compute.projects.get), IAP sudo SSH on platform-dev/prod plus leftover screener/validator/fleet VMs (query_prod_db.sh, py-spy, pprofctl), and secretAccessor on TARGON_API_KEY (query_targon.sh). Not project Editor, not postgres SSH, not other secrets. Platform SSH already covered by ssh_users is not duplicated."
  type        = list(string)
  default = [
    "user:brian@omniaura.ai",
  ]
}
