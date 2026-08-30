###############################################################################
# SN118 dedicated screener VM (optional; gated by var.enable_screener).
#
# A private GCE VM in the platform VPC that runs ONLY the screener (the infra
# `screener_worker` Ansible role via infra/ansible/playbooks/gcp-screener.yml):
#
#   ditto-screener: the stateless pre-benchmark gate that drains the platform's
#                    `uploaded` queue, runs `docker build` + a `/health` serve
#                    check on each submitted crate, and posts a signed pass/fail
#                    verdict. It BUILDS + RUNS untrusted miner code in Docker.
#
# Why a dedicated VM and not the platform-api host: the screener executes
# untrusted crate builds, so it must not share a host with the public API and
# its secrets. This box is sacrificial; isolation of the build is Docker's job.
#
# No public IP: egress via the platform Cloud NAT, SSH via IAP. Not part of the
# public HTTP ingress.
#
# OFF by default so a routine `terraform apply` doesn't create it. Turn it on
# deliberately (keep enable_validator=true so the screener's run-SA access to
# validator-hotkey-mnemonic + validator-gh-token stays granted):
#   terraform apply -var=enable_validator=true -var=enable_screener=true \
#     -target=module.screener_vm
# then converge with infra/ansible/playbooks/gcp-screener.yml. See the screener section
# of docs/validator-deploy.md for the runbook.
###############################################################################

variable "enable_screener" {
  description = "Create the dedicated SN118 screener VM. Off by default; turn on to stand up the platform-operated screener on its own isolated host."
  type        = bool
  default     = false
}

variable "screener_boot_disk_gb" {
  description = "Boot disk for the screener VM. Holds the ditto-subnet checkout, the uv venv, and Docker images + cargo target dirs for the untrusted miner builds it gates."
  type        = number
  default     = 80
}

variable "screener_prod_boot_disk_gb" {
  description = "Prod screener boot disk. Temporarily enlarged for the policy-v7 rescreen backlog and its Docker build cache; dev keeps the smaller shared default."
  type        = number
  default     = 160
}

locals {
  screener_count = var.enable_screener ? 1 : 0
}

# Submission workers never inherit the Platform API identity. Their runtime
# authority is limited to the exact signing, bearer, source-review, and deploy
# key secrets required by the worker bootstrap.
resource "google_service_account" "screener_worker" {
  project      = var.project
  account_id   = "ditto-screener-worker"
  display_name = "Ditto Screener Worker"
}

resource "google_secret_manager_secret_iam_member" "screener_dev_mnemonic_access" {
  count     = local.screener_count
  project   = var.project
  secret_id = "validator-hotkey-mnemonic"
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.screener_worker.email}"
  depends_on = [
    google_secret_manager_secret.validator_hotkey_mnemonic,
  ]
}

resource "google_secret_manager_secret_iam_member" "screener_source_review_access" {
  count     = (var.enable_screener || var.enable_screener_prod || var.enable_screener_fleet) ? 1 : 0
  project   = var.project
  secret_id = "validator-openrouter-key"
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.screener_worker.email}"
  depends_on = [
    google_secret_manager_secret.validator_openrouter_key,
  ]
}

# --- The VM (private; label role=screener → Ansible group role_screener). ---
#
# Reuses the platform runtime SA (run_sa_email), which validator.tf already
# grants secretAccessor on validator-hotkey-mnemonic + validator-gh-token (the
# two secrets the screener_worker role reads at converge time). That grant is
# gated by enable_validator, so keep the validator secrets enabled while the
# screener runs; a dedicated screener SA is a follow-up if the validator VM is
# fully retired.
module "screener_vm" {
  source   = "../../modules/compute/gcp"
  count    = local.screener_count
  project  = var.project
  name     = "ditto-screener-dev"
  size     = "validator" # same build-capable class: Docker + cargo builds
  image    = "debian-13"
  location = var.zone

  subnetwork       = module.network.subnetwork_id
  network_tags     = [module.network.ssh_target_tag] # IAP-SSH only; no public ingress
  assign_public_ip = false                           # egress via Cloud NAT
  boot_disk_gb     = var.screener_boot_disk_gb

  service_account_email = google_service_account.screener_worker.email
  labels                = { env = "dev", role = "screener", managed = "terraform" }
}

output "screener_vm_name" {
  description = "Name of the screener VM (empty when enable_screener = false)."
  value       = var.enable_screener ? module.screener_vm[0].hostname : ""
}

output "screener_vm_internal_ip" {
  description = "Private IP of the screener VM (reachability is via IAP)."
  value       = var.enable_screener ? module.screener_vm[0].internal_ip : ""
}

###############################################################################
# Prod screener VM (env=prod). Same shape as the dev screener above, but points
# at the prod platform (netuid 118, platform-api.heyditto.ai) and signs with the
# screener hotkey (SS58 5G6fG...KekTtR). Kept as a separate block, not
# a for_each over the dev one, so turning prod on never disturbs the running dev
# screener VM's state address.
###############################################################################

variable "enable_screener_prod" {
  description = "Create the prod SN118 screener VM + its finney hotkey secret. Off by default; turn on to stand up the prod screener."
  type        = bool
  default     = false
}

variable "screener_prod_zone" {
  description = "Zone for the prod screener VM. This remains independent of var.zone because the deploy workflow targets us-central1-c."
  type        = string
  default     = "us-central1-c"
}

locals {
  screener_prod_count = var.enable_screener_prod ? 1 : 0
}

# The screener signing hotkey mnemonic (for SS58 5G6fG...KekTtR). VALUE is added
# out of band (`gcloud secrets versions add screener-hotkey-mnemonic-prod`),
# never through Terraform state. prevent_destroy: losing it means re-registering
# the hotkey (permit + stake) from scratch.
resource "google_secret_manager_secret" "screener_hotkey_mnemonic_prod" {
  count     = local.screener_prod_count
  project   = var.project
  secret_id = "screener-hotkey-mnemonic-prod"
  replication {
    auto {}
  }
  lifecycle {
    prevent_destroy = true
  }
}

# Let the platform runtime SA (which the prod screener VM runs as) read the
# finney mnemonic. The gh-token (validator-gh-token) grant to the same SA is
# already covered by validator.tf's validator_access binding.
resource "google_secret_manager_secret_iam_member" "screener_prod_mnemonic_access" {
  count     = local.screener_prod_count
  project   = var.project
  secret_id = google_secret_manager_secret.screener_hotkey_mnemonic_prod[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.screener_worker.email}"
}

# The screener API bearer token, shared with the platform's /screener/* endpoints
# (the dedicated-credential auth that replaced the validator-permit gate). VALUE
# is added out of band (`gcloud secrets versions add screener-api-token-prod`),
# never through Terraform state. prevent_destroy: it must stay in sync with the
# platform's SCREENER_API_TOKEN or screening auth breaks.
resource "google_secret_manager_secret" "screener_api_token_prod" {
  count     = local.screener_prod_count
  project   = var.project
  secret_id = "screener-api-token-prod"
  replication {
    auto {}
  }
  lifecycle {
    prevent_destroy = true
  }
}

# Both the prod screener VM (sends the token) and the prod platform app VM
# (verifies it) run as the platform runtime SA and read this secret.
resource "google_secret_manager_secret_iam_member" "screener_prod_api_token_access" {
  count     = local.screener_prod_count
  project   = var.project
  secret_id = google_secret_manager_secret.screener_api_token_prod[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.screener_worker.email}"
}


# The prod API verifies the same bearer after its VM moves to the dedicated
# identity. Keep the shared binding during the bounded rollback window.
resource "google_secret_manager_secret_iam_member" "platform_api_screener_prod_api_token_access" {
  count     = local.screener_prod_count
  project   = var.project
  secret_id = google_secret_manager_secret.screener_api_token_prod[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${local.platform_api_sa_email}"
}

# Temporary policy-v7 rescreen capacity. Keep this explicit so returning to the
# steady-state validator class is a reviewed IaC change after the queue drains;
# horizontal scaling is provided by the autoscaled fleet in screener-fleet.tf.
#
# This VM is superseded by the screener fleet (screener-fleet.tf); decommission
# it (enable_screener_prod=false + manual delete, it has deletion_protection)
# once the fleet is live — see docs/screener-scaling.md.
module "screener_vm_prod" {
  source   = "../../modules/compute/gcp"
  count    = local.screener_prod_count
  project  = var.project
  name     = "ditto-screener-prod"
  size     = "screener-burst-n2d"
  image    = "debian-13"
  location = var.screener_prod_zone

  subnetwork       = module.network.subnetwork_id
  network_tags     = [module.network.ssh_target_tag] # IAP-SSH only; no public ingress
  assign_public_ip = false                           # egress via Cloud NAT
  boot_disk_gb     = var.screener_prod_boot_disk_gb

  service_account_email = google_service_account.screener_worker.email
  labels                = { env = "prod", role = "screener", managed = "terraform" }
}

output "screener_prod_vm_name" {
  description = "Name of the prod screener VM (empty when enable_screener_prod = false)."
  value       = var.enable_screener_prod ? module.screener_vm_prod[0].hostname : ""
}

output "screener_prod_vm_internal_ip" {
  description = "Private IP of the prod screener VM (reachability is via IAP)."
  value       = var.enable_screener_prod ? module.screener_vm_prod[0].internal_ip : ""
}
