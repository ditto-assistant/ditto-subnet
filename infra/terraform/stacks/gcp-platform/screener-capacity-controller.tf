###############################################################################
# Federated screener capacity controller.
#
# Platform is the control plane and audit store; this private VM is the single
# provider mutator. It tries capability-gated Targon Rentals first, then resizes
# the GCE MIG for the residual demand. Provider and controller credentials are
# read from Secret Manager at converge time and remain mode-0600 files.
###############################################################################

variable "enable_screener_capacity_controller" {
  description = "Create the private Targon-first screener capacity controller VM and its least-privilege identities."
  type        = bool
  default     = false
}

variable "targon_api_key_secret_id" {
  description = "Existing Secret Manager secret containing the Targon API key. Terraform reads metadata only, never a secret version."
  type        = string
  default     = "TARGON_API_KEY"
}

locals {
  screener_capacity_controller_count = var.enable_screener_capacity_controller ? 1 : 0
}

check "screener_capacity_requires_fleet" {
  assert {
    condition     = !var.enable_screener_capacity_controller || var.enable_screener_fleet
    error_message = "enable_screener_capacity_controller requires enable_screener_fleet=true."
  }
}

# The value is populated out of band. Both Platform and the controller read the
# same bearer, while ordinary screener workers cannot access it.
resource "google_secret_manager_secret" "screener_controller_api_token" {
  count     = local.screener_capacity_controller_count
  project   = var.project
  secret_id = "screener-controller-api-token-prod"
  replication {
    auto {}
  }
  lifecycle {
    prevent_destroy = true
  }
}

# The user's existing provider credential. This data source resolves only the
# secret resource metadata; no version payload enters Terraform state or logs.
data "google_secret_manager_secret" "targon_api_key" {
  count     = local.screener_capacity_controller_count
  project   = var.project
  secret_id = var.targon_api_key_secret_id
}

resource "google_service_account" "screener_capacity_controller" {
  count        = local.screener_capacity_controller_count
  project      = var.project
  account_id   = "ditto-screener-capacity"
  display_name = "Ditto Screener Capacity Controller"
}

# Targon workers receive a 30-minute access token for this identity. It can
# access only the source-review credential required by the trusted worker; no
# long-lived GCP key or broad Platform identity crosses provider boundaries.
resource "google_service_account" "screener_worker_bootstrap" {
  count        = local.screener_capacity_controller_count
  project      = var.project
  account_id   = "ditto-screener-bootstrap"
  display_name = "Ditto Federated Screener Bootstrap"
}

resource "google_secret_manager_secret_iam_member" "screener_controller_token_controller_access" {
  count     = local.screener_capacity_controller_count
  project   = var.project
  secret_id = google_secret_manager_secret.screener_controller_api_token[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.screener_capacity_controller[0].email}"
}

resource "google_secret_manager_secret_iam_member" "screener_controller_token_platform_access" {
  count     = local.screener_capacity_controller_count
  project   = var.project
  secret_id = google_secret_manager_secret.screener_controller_api_token[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${local.platform_api_sa_email}"
}

resource "google_secret_manager_secret_iam_member" "targon_api_key_controller_access" {
  count     = local.screener_capacity_controller_count
  project   = var.project
  secret_id = data.google_secret_manager_secret.targon_api_key[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.screener_capacity_controller[0].email}"
}

resource "google_secret_manager_secret_iam_member" "screener_bootstrap_source_review_access" {
  count     = local.screener_capacity_controller_count
  project   = var.project
  secret_id = "validator-openrouter-key"
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.screener_worker_bootstrap[0].email}"
}

resource "google_service_account_iam_member" "screener_controller_mint_bootstrap_tokens" {
  count              = local.screener_capacity_controller_count
  service_account_id = google_service_account.screener_worker_bootstrap[0].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.screener_capacity_controller[0].email}"
}

# Only the two permissions used by `gcloud compute instance-groups managed
# describe/list-instances/resize`; the controller cannot create VMs, images,
# disks, networks, or mutate any other fleet.
resource "google_project_iam_custom_role" "screener_controller_fleet_reconciler" {
  count   = local.screener_capacity_controller_count
  project = var.project
  role_id = "dittoScreenerFleetReconciler"
  title   = "Ditto screener fleet reconciler"
  permissions = [
    "compute.instanceGroupManagers.get",
    "compute.instanceGroupManagers.update",
  ]
}

resource "google_project_iam_member" "screener_controller_fleet_reconciler" {
  count   = local.screener_capacity_controller_count
  project = var.project
  role    = google_project_iam_custom_role.screener_controller_fleet_reconciler[0].name
  member  = "serviceAccount:${google_service_account.screener_capacity_controller[0].email}"

  condition {
    title       = "only_ditto_screener_fleet"
    description = "Restrict controller mutations to the production screener MIG."
    expression  = "resource.name.endsWith('/regions/${var.region}/instanceGroupManagers/ditto-screener-fleet')"
  }
}

module "screener_capacity_controller_vm" {
  source   = "../../modules/compute/gcp"
  count    = local.screener_capacity_controller_count
  project  = var.project
  name     = "ditto-screener-capacity-prod"
  size     = "controller-small"
  image    = "debian-13"
  location = var.zone

  subnetwork       = module.network.subnetwork_id
  network_tags     = [module.network.ssh_target_tag]
  assign_public_ip = false
  boot_disk_gb     = 20

  service_account_email = google_service_account.screener_capacity_controller[0].email
  labels = {
    env     = "prod"
    role    = "screener-capacity"
    managed = "terraform"
  }
}

output "screener_capacity_controller_vm_name" {
  description = "Private capacity-controller VM name (empty when disabled)."
  value       = var.enable_screener_capacity_controller ? module.screener_capacity_controller_vm[0].hostname : ""
}

output "screener_capacity_controller_sa_email" {
  description = "Least-privilege provider-mutator service account."
  value       = var.enable_screener_capacity_controller ? google_service_account.screener_capacity_controller[0].email : ""
}

output "screener_worker_bootstrap_sa_email" {
  description = "Identity used only for 30-minute federated worker bootstrap tokens."
  value       = var.enable_screener_capacity_controller ? google_service_account.screener_worker_bootstrap[0].email : ""
}
