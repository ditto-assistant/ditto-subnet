# Dormant V13 private verifier infrastructure. No secret versions, protected
# objects, provider grants, service deployment, or replay are created here.
# Enable only after reviewing the exact plan and independent bank custody.
variable "enable_v13_private_verifier" {
  description = "Stage the isolated V13 verifier VM, empty protected bank, and empty ticket/provider secret containers. Does not activate a verifier."
  type        = bool
  default     = false
}

variable "v13_private_verifier_subnet_cidr" {
  description = "Dedicated verifier subnet outside the Platform/Postgres ingress source range."
  type        = string
  default     = "10.32.0.0/24"

  validation {
    condition     = var.v13_private_verifier_subnet_cidr != var.subnet_cidr && var.v13_private_verifier_subnet_cidr != var.validator_prod_subnet_cidr
    error_message = "The V13 verifier subnet must differ from Platform and validator subnets."
  }
}

variable "v13_private_verifier_boot_disk_gb" {
  description = "Boot disk for the staged verifier host and temporary sandbox images."
  type        = number
  default     = 100

  validation {
    condition     = var.v13_private_verifier_boot_disk_gb >= 100
    error_message = "The V13 verifier host needs at least 100 GB for the staged sandbox profile."
  }
}

variable "v13_private_verifier_operators" {
  description = "Explicit human OS Login and IAP custodians for this one host. Empty until approved."
  type        = set(string)
  default     = []
}

locals {
  v13_private_verifier_count = var.enable_v13_private_verifier ? 1 : 0
  v13_private_verifier_tag   = "v13-private-verifier"
}

resource "google_compute_subnetwork" "v13_private_verifier" {
  count                    = local.v13_private_verifier_count
  project                  = var.project
  name                     = "ditto-v13-private-verifier-${var.region}"
  region                   = var.region
  network                  = module.network.network_id
  ip_cidr_range            = var.v13_private_verifier_subnet_cidr
  private_ip_google_access = true
}

# Deny host and escaped-container access to private peers, including
# Platform/Postgres, even if their inbound policy changes. The host has no
# public IP or HTTP ingress; Google APIs and provider relay use public egress.
resource "google_compute_firewall" "v13_private_verifier_deny_private_egress" {
  count              = local.v13_private_verifier_count
  project            = var.project
  name               = "ditto-v13-private-verifier-deny-private-egress"
  network            = module.network.network_id
  direction          = "EGRESS"
  priority           = 900
  destination_ranges = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
  target_tags        = [local.v13_private_verifier_tag]

  deny {
    protocol = "all"
  }
}

resource "google_compute_firewall" "v13_private_verifier_iap_ssh" {
  count         = local.v13_private_verifier_count
  project       = var.project
  name          = "ditto-v13-private-verifier-allow-iap-ssh"
  network       = module.network.network_id
  direction     = "INGRESS"
  source_ranges = ["35.235.240.0/20"]
  target_tags   = [local.v13_private_verifier_tag]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

resource "google_service_account" "v13_private_verifier" {
  count        = local.v13_private_verifier_count
  project      = var.project
  account_id   = "ditto-v13-verifier"
  display_name = "Ditto V13 protected verifier runtime"
}

resource "google_project_iam_member" "v13_private_verifier_observability" {
  for_each = var.enable_v13_private_verifier ? toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
  ]) : toset([])
  project = var.project
  role    = each.value
  member  = "serviceAccount:${google_service_account.v13_private_verifier[0].email}"
}

module "v13_private_verifier_vm" {
  source   = "../../modules/compute/gcp"
  count    = local.v13_private_verifier_count
  project  = var.project
  name     = "ditto-v13-private-verifier-prod"
  size     = "validator"
  image    = "debian-13"
  location = var.zone

  subnetwork       = google_compute_subnetwork.v13_private_verifier[0].id
  network_tags     = [local.v13_private_verifier_tag]
  assign_public_ip = false
  boot_disk_gb     = var.v13_private_verifier_boot_disk_gb

  service_account_email       = google_service_account.v13_private_verifier[0].email
  enable_secure_boot          = true
  enable_vtpm                 = true
  enable_integrity_monitoring = true
  labels = {
    env     = "prod"
    role    = "v13_private_verifier"
    managed = "terraform"
  }
}

resource "google_compute_instance_iam_member" "v13_private_verifier_operator_osadmin" {
  for_each      = var.enable_v13_private_verifier ? var.v13_private_verifier_operators : toset([])
  project       = var.project
  zone          = var.zone
  instance_name = module.v13_private_verifier_vm[0].hostname
  role          = "roles/compute.osAdminLogin"
  member        = each.value
}

resource "google_project_iam_member" "v13_private_verifier_operator_iap" {
  for_each = var.enable_v13_private_verifier ? var.v13_private_verifier_operators : toset([])
  project  = var.project
  role     = "roles/iap.tunnelResourceAccessor"
  member   = each.value

  condition {
    title      = "v13_private_verifier_exact_instance"
    expression = "resource.name.extract('/instances/{name}') == '${module.v13_private_verifier_vm[0].hostname}'"
  }

  depends_on = [google_project_service.iap]
}

resource "google_project_iam_member" "v13_private_verifier_operator_compute_viewer" {
  for_each = var.enable_v13_private_verifier ? var.v13_private_verifier_operators : toset([])
  project  = var.project
  role     = "roles/compute.viewer"
  member   = each.value
}

resource "google_service_account_iam_member" "v13_private_verifier_operator_actas" {
  for_each           = var.enable_v13_private_verifier ? var.v13_private_verifier_operators : toset([])
  service_account_id = google_service_account.v13_private_verifier[0].name
  role               = "roles/iam.serviceAccountUser"
  member             = each.value
}

# Terraform never manages protected objects or their values. Upload of an
# independently approved, exact-generation bank is a later guarded operation.
resource "google_storage_bucket" "v13_private_bank" {
  count                       = local.v13_private_verifier_count
  project                     = var.project
  name                        = "${var.project}-v13-private-bank"
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_storage_bucket_iam_member" "v13_private_bank_read" {
  count  = local.v13_private_verifier_count
  bucket = google_storage_bucket.v13_private_bank[0].name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.v13_private_verifier[0].email}"
}

# The random ticket key version is added out of band only after both Platform
# and scorer deployments are ready. The verifier receives no provider key.
resource "google_secret_manager_secret" "v13_private_ticket_key" {
  count     = local.v13_private_verifier_count
  project   = var.project
  secret_id = "v13-private-ticket-key"
  replication {
    auto {}
  }
  lifecycle {
    prevent_destroy = true
  }
}

resource "google_secret_manager_secret_iam_member" "v13_private_ticket_platform_read" {
  count     = local.v13_private_verifier_count
  project   = var.project
  secret_id = google_secret_manager_secret.v13_private_ticket_key[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${local.platform_api_sa_email}"
}

resource "google_secret_manager_secret_iam_member" "v13_private_ticket_verifier_read" {
  count     = local.v13_private_verifier_count
  project   = var.project
  secret_id = google_secret_manager_secret.v13_private_ticket_key[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.v13_private_verifier[0].email}"
}

# A separate provider credential container is Platform-owned. No value or
# grant is installed by this plan; the future private provider endpoint must
# bind model, case, source, budget, and expiry before the verifier may use it.
resource "google_secret_manager_secret" "v13_private_provider_key" {
  count     = local.v13_private_verifier_count
  project   = var.project
  secret_id = "v13-private-provider-key"
  replication {
    auto {}
  }
  lifecycle {
    prevent_destroy = true
  }
}

resource "google_secret_manager_secret_iam_member" "v13_private_provider_platform_read" {
  count     = local.v13_private_verifier_count
  project   = var.project
  secret_id = google_secret_manager_secret.v13_private_provider_key[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${local.platform_api_sa_email}"
}

output "v13_private_verifier_name" {
  description = "Staged verifier VM name, empty when disabled."
  value       = var.enable_v13_private_verifier ? module.v13_private_verifier_vm[0].hostname : ""
}

output "v13_private_bank_bucket" {
  description = "Empty protected bank bucket name, empty when disabled."
  value       = var.enable_v13_private_verifier ? google_storage_bucket.v13_private_bank[0].name : ""
}
