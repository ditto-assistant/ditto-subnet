###############################################################################
# Production SN118 validator on GCP.
#
# This is deliberately separate from validator.tf's historical dev rehearsal.
# The host has no public IP, accepts SSH only through IAP + OS Login, runs under
# a dedicated service account, and receives no platform/provider credentials.
# The coldkey never enters GCP. Only the new hotkey mnemonic is stored in Secret
# Manager; its version is populated out of band and disabled after the wallet
# has been materialized and verified on the host.
###############################################################################

variable "enable_validator_prod" {
  description = "Create the private production GCP SN118 validator host and hotkey secret container. Runtime activation and the on-chain hotkey swap remain explicit operator steps."
  type        = bool
  default     = false
}

variable "validator_prod_boot_disk_gb" {
  description = "Production validator boot disk. Must leave at least 160 GB free for eight concurrent sandbox slots after host and image installation."
  type        = number
  default     = 250

  validation {
    condition     = var.validator_prod_boot_disk_gb >= 200
    error_message = "validator_prod_boot_disk_gb must be at least 200 GB for the eight-slot production profile."
  }
}

variable "validator_prod_subnet_cidr" {
  description = "Dedicated production validator subnet. It must remain outside the Platform/Postgres source range."
  type        = string
  default     = "10.31.0.0/24"

  validation {
    condition     = var.validator_prod_subnet_cidr != var.subnet_cidr
    error_message = "validator_prod_subnet_cidr must differ from the Platform subnet CIDR."
  }
}

variable "validator_prod_operators" {
  description = "Small explicit set of human custodians with sudo OS Login/IAP access to the production validator. Sudo can read the materialized hotkey, so do not reuse the broad platform SSH list."
  type        = set(string)
  default = [
    "user:omar@omniaura.ai",
    "user:peyton@omniaura.ai",
  ]
}

locals {
  validator_prod_count       = var.enable_validator_prod ? 1 : 0
  validator_prod_network_tag = "validator-prod"
}

resource "google_compute_subnetwork" "validator_prod" {
  count                    = local.validator_prod_count
  project                  = var.project
  name                     = "ditto-validator-prod-${var.region}"
  region                   = var.region
  network                  = module.network.network_id
  ip_cidr_range            = var.validator_prod_subnet_cidr
  private_ip_google_access = true
}

# Hostile benchmark workloads must not have a network path to the Platform
# subnet even after a container or Docker-daemon escape. The dedicated subnet
# is already outside the Postgres ingress source range; this explicit egress
# deny makes the isolation independent of that allow rule's future shape.
resource "google_compute_firewall" "validator_prod_deny_platform_egress" {
  count              = local.validator_prod_count
  project            = var.project
  name               = "ditto-validator-prod-deny-platform-egress"
  network            = module.network.network_id
  direction          = "EGRESS"
  priority           = 900
  destination_ranges = [var.subnet_cidr]
  target_tags        = [local.validator_prod_network_tag]

  deny {
    protocol = "all"
  }
}

resource "google_service_account" "validator_prod" {
  count        = local.validator_prod_count
  project      = var.project
  account_id   = "ditto-validator-prod"
  display_name = "Ditto production validator runtime"
}

resource "google_project_iam_member" "validator_prod_observability" {
  for_each = var.enable_validator_prod ? toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
  ]) : toset([])
  project = var.project
  role    = each.value
  member  = "serviceAccount:${google_service_account.validator_prod[0].email}"
}

module "validator_prod_vm" {
  source   = "../../modules/compute/gcp"
  count    = local.validator_prod_count
  project  = var.project
  name     = "ditto-validator-prod"
  size     = "validator-prod"
  image    = "debian-13"
  location = var.zone

  subnetwork       = google_compute_subnetwork.validator_prod[0].id
  network_tags     = [module.network.ssh_target_tag, local.validator_prod_network_tag]
  assign_public_ip = false
  boot_disk_gb     = var.validator_prod_boot_disk_gb

  service_account_email       = google_service_account.validator_prod[0].email
  enable_secure_boot          = true
  enable_vtpm                 = true
  enable_integrity_monitoring = true
  labels = {
    env     = "prod"
    role    = "validator_prod"
    managed = "terraform"
  }
}

# Terraform owns only the container and access policy. The seed is generated on
# the separately armed disposable admin and streamed to a new version over
# stdin, so it never enters configuration, a plan, state, or an operator host.
resource "google_secret_manager_secret" "validator_prod_hotkey_mnemonic" {
  count     = local.validator_prod_count
  project   = var.project
  secret_id = "validator-prod-hotkey-mnemonic"

  replication {
    auto {}
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_secret_manager_secret_iam_member" "validator_prod_hotkey_access" {
  count     = local.validator_prod_count
  project   = var.project
  secret_id = google_secret_manager_secret.validator_prod_hotkey_mnemonic[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.validator_prod[0].email}"
}

# The production validator publishes aggregate telemetry to W&B. Grant only
# payload access on the existing validator-wandb-key secret; its value and
# versions remain outside Terraform state.
resource "google_secret_manager_secret_iam_member" "validator_prod_wandb_access" {
  count     = local.validator_prod_count
  project   = var.project
  secret_id = google_secret_manager_secret.validator_wandb_key[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.validator_prod[0].email}"
}

# Custodians can lifecycle-manage the one recovery version without reading its
# payload, adding a replacement, or destroying it. Only the armed disposable
# generator can add the first version. Payload access remains exclusive to the
# validator VM service account while that numeric version is enabled.
resource "google_project_iam_custom_role" "validator_prod_hotkey_custodian" {
  count       = local.validator_prod_count
  project     = var.project
  role_id     = "validatorProdHotkeyCustodian"
  title       = "Validator production hotkey custodian"
  description = "Lifecycle-manage the validator hotkey version without adding, reading, or destroying payloads."
  permissions = [
    "secretmanager.secrets.get",
    "secretmanager.versions.disable",
    "secretmanager.versions.enable",
    "secretmanager.versions.get",
    "secretmanager.versions.list",
  ]
}

resource "google_secret_manager_secret_iam_member" "validator_prod_hotkey_custodian" {
  for_each  = var.enable_validator_prod ? var.validator_prod_operators : toset([])
  project   = var.project
  secret_id = google_secret_manager_secret.validator_prod_hotkey_mnemonic[0].secret_id
  role      = google_project_iam_custom_role.validator_prod_hotkey_custodian[0].name
  member    = each.value
}

# Every sudo user on this host is a hotkey custodian. Keep this list separate
# from ssh_users/debug_operators and scope all mutable access to this instance.
resource "google_compute_instance_iam_member" "validator_prod_operator_osadmin" {
  for_each      = var.enable_validator_prod ? var.validator_prod_operators : toset([])
  project       = var.project
  zone          = var.zone
  instance_name = module.validator_prod_vm[0].hostname
  role          = "roles/compute.osAdminLogin"
  member        = each.value
}

resource "google_iap_tunnel_instance_iam_member" "validator_prod_operator_iap" {
  for_each   = var.enable_validator_prod ? var.validator_prod_operators : toset([])
  project    = var.project
  zone       = var.zone
  instance   = module.validator_prod_vm[0].hostname
  role       = "roles/iap.tunnelResourceAccessor"
  member     = each.value
  depends_on = [google_project_service.iap]
}

resource "google_project_iam_member" "validator_prod_operator_compute_viewer" {
  for_each = var.enable_validator_prod ? var.validator_prod_operators : toset([])
  project  = var.project
  role     = "roles/compute.viewer"
  member   = each.value
}

resource "google_service_account_iam_member" "validator_prod_operator_actas" {
  for_each           = var.enable_validator_prod ? var.validator_prod_operators : toset([])
  service_account_id = google_service_account.validator_prod[0].name
  role               = "roles/iam.serviceAccountUser"
  member             = each.value
}

output "validator_prod_vm_name" {
  description = "Production validator VM name (empty when disabled)."
  value       = var.enable_validator_prod ? module.validator_prod_vm[0].hostname : ""
}

output "validator_prod_vm_internal_ip" {
  description = "Production validator private IP; reachability is through IAP."
  value       = var.enable_validator_prod ? module.validator_prod_vm[0].internal_ip : ""
}

output "validator_prod_hotkey_secret_id" {
  description = "Secret Manager container for the production hotkey mnemonic. The value is never managed by Terraform."
  value       = var.enable_validator_prod ? google_secret_manager_secret.validator_prod_hotkey_mnemonic[0].secret_id : ""
}
