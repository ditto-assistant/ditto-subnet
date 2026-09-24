provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  count    = var.enable_v13_private_project ? 1 : 0
  host_tag = "v13-private-verifier"
  services = toset([
    "compute.googleapis.com",
    "iam.googleapis.com",
    "iap.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
  ])
}

resource "google_project" "verifier" {
  count               = local.count
  project_id          = var.project_id
  name                = "Ditto V13 private verifier"
  org_id              = var.organization_id
  billing_account     = var.billing_account_id
  auto_create_network = false

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_project_service" "required" {
  for_each           = var.enable_v13_private_project ? local.services : toset([])
  project            = google_project.verifier[0].project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_compute_network" "verifier" {
  count                   = local.count
  project                 = google_project.verifier[0].project_id
  name                    = "ditto-v13-private-net"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
  depends_on              = [google_project_service.required]
}

resource "google_compute_subnetwork" "verifier" {
  count                    = local.count
  project                  = google_project.verifier[0].project_id
  name                     = "ditto-v13-private-${var.region}"
  region                   = var.region
  network                  = google_compute_network.verifier[0].id
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = true
}

resource "google_compute_router" "verifier" {
  count   = local.count
  project = google_project.verifier[0].project_id
  name    = "ditto-v13-private-router"
  region  = var.region
  network = google_compute_network.verifier[0].id
}

resource "google_compute_router_nat" "verifier" {
  count                              = local.count
  project                            = google_project.verifier[0].project_id
  name                               = "ditto-v13-private-nat"
  router                             = google_compute_router.verifier[0].name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}

resource "google_compute_firewall" "iap_ssh" {
  count         = local.count
  project       = google_project.verifier[0].project_id
  name          = "ditto-v13-private-allow-iap-ssh"
  network       = google_compute_network.verifier[0].id
  direction     = "INGRESS"
  source_ranges = ["35.235.240.0/20"]
  target_tags   = [local.host_tag]
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

resource "google_compute_firewall" "deny_private_egress" {
  count              = local.count
  project            = google_project.verifier[0].project_id
  name               = "ditto-v13-private-deny-rfc1918"
  network            = google_compute_network.verifier[0].id
  direction          = "EGRESS"
  priority           = 900
  destination_ranges = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
  target_tags        = [local.host_tag]
  deny {
    protocol = "all"
  }
}

resource "google_service_account" "verifier" {
  count        = local.count
  project      = google_project.verifier[0].project_id
  account_id   = "ditto-v13-verifier"
  display_name = "Ditto V13 protected verifier runtime"
  depends_on   = [google_project_service.required]
}

resource "google_project_iam_member" "observability" {
  for_each = var.enable_v13_private_project ? toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
  ]) : toset([])
  project = google_project.verifier[0].project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.verifier[0].email}"
}

module "verifier_vm" {
  source   = "../../modules/compute/gcp"
  count    = local.count
  project  = google_project.verifier[0].project_id
  name     = "ditto-v13-private-verifier-prod"
  size     = "validator"
  image    = "debian-13"
  location = var.zone

  subnetwork       = google_compute_subnetwork.verifier[0].id
  network_tags     = [local.host_tag]
  assign_public_ip = false
  boot_disk_gb     = var.boot_disk_gb

  service_account_email       = google_service_account.verifier[0].email
  enable_secure_boot          = true
  enable_vtpm                 = true
  enable_integrity_monitoring = true
  labels = {
    env     = "prod"
    role    = "v13_private_verifier"
    managed = "terraform"
  }
}

resource "google_storage_bucket" "bank" {
  count                       = local.count
  project                     = google_project.verifier[0].project_id
  name                        = "${var.project_id}-bank"
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
  depends_on = [google_project_service.required]
}

resource "google_storage_bucket_iam_member" "bank_read" {
  count  = local.count
  bucket = google_storage_bucket.bank[0].name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.verifier[0].email}"
}

# Empty containers only. An approved operator installs versions separately.
resource "google_secret_manager_secret" "ticket_key" {
  count     = local.count
  project   = google_project.verifier[0].project_id
  secret_id = "v13-private-ticket-key"
  replication {
    auto {}
  }
  lifecycle {
    prevent_destroy = true
  }
  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_iam_member" "ticket_platform_read" {
  count     = local.count
  project   = google_project.verifier[0].project_id
  secret_id = google_secret_manager_secret.ticket_key[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.platform_api_service_account}"
}

resource "google_secret_manager_secret_iam_member" "ticket_verifier_read" {
  count     = local.count
  project   = google_project.verifier[0].project_id
  secret_id = google_secret_manager_secret.ticket_key[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.verifier[0].email}"
}

resource "google_secret_manager_secret" "provider_key" {
  count     = local.count
  project   = google_project.verifier[0].project_id
  secret_id = "v13-private-provider-key"
  replication {
    auto {}
  }
  lifecycle {
    prevent_destroy = true
  }
  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_iam_member" "provider_platform_read" {
  count     = local.count
  project   = google_project.verifier[0].project_id
  secret_id = google_secret_manager_secret.provider_key[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.platform_api_service_account}"
}

resource "google_compute_instance_iam_member" "operator_osadmin" {
  for_each      = var.enable_v13_private_project ? var.operators : toset([])
  project       = google_project.verifier[0].project_id
  zone          = var.zone
  instance_name = module.verifier_vm[0].hostname
  role          = "roles/compute.osAdminLogin"
  member        = each.value
}

resource "google_project_iam_member" "operator_iap" {
  for_each = var.enable_v13_private_project ? var.operators : toset([])
  project  = google_project.verifier[0].project_id
  role     = "roles/iap.tunnelResourceAccessor"
  member   = each.value
  condition {
    title      = "v13_verifier_exact_instance"
    expression = "resource.name.extract('/instances/{name}') == '${module.verifier_vm[0].hostname}'"
  }
  depends_on = [google_project_service.required]
}

resource "google_project_iam_member" "operator_compute_viewer" {
  for_each = var.enable_v13_private_project ? var.operators : toset([])
  project  = google_project.verifier[0].project_id
  role     = "roles/compute.viewer"
  member   = each.value
}

resource "google_service_account_iam_member" "operator_actas" {
  for_each           = var.enable_v13_private_project ? var.operators : toset([])
  service_account_id = google_service_account.verifier[0].name
  role               = "roles/iam.serviceAccountUser"
  member             = each.value
}

output "project_id" {
  value = var.enable_v13_private_project ? google_project.verifier[0].project_id : ""
}

output "verifier_vm_name" {
  value = var.enable_v13_private_project ? module.verifier_vm[0].hostname : ""
}

output "bank_bucket" {
  value = var.enable_v13_private_project ? google_storage_bucket.bank[0].name : ""
}
