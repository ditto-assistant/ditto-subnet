# Native v2 belongs to Platform, not the legacy k=3 validator/scorer cohort.
# This foundation provisions no daemon, worker, secret, image, or assignment.
locals {
  name      = "ditto-coding-hosted-v2"
  operators = var.enabled ? var.operators : toset([])
}

# A separate VPC inherits no Platform DB firewall rules. By default there is no
# peering; postgres.tf adds only a separately reviewed, explicitly gated path.
resource "google_compute_network" "host" {
  count                   = var.enabled ? 1 : 0
  project                 = var.project
  name                    = local.name
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
}

resource "google_compute_subnetwork" "host" {
  count                    = var.enabled ? 1 : 0
  project                  = var.project
  name                     = "${local.name}-${var.region}"
  region                   = var.region
  network                  = google_compute_network.host[0].id
  ip_cidr_range            = "10.33.0.0/24"
  private_ip_google_access = true
}

# Host provisioning can download packages over HTTP(S) through NAT. This is
# NOT a candidate egress policy and must never authorize an untrusted run.
resource "google_compute_router" "host" {
  count   = var.enabled ? 1 : 0
  project = var.project
  name    = local.name
  region  = var.region
  network = google_compute_network.host[0].id
}

resource "google_compute_router_nat" "host" {
  count                              = var.enabled ? 1 : 0
  project                            = var.project
  name                               = local.name
  region                             = var.region
  router                             = google_compute_router.host[0].name
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"
  subnetwork {
    name                    = google_compute_subnetwork.host[0].id
    source_ip_ranges_to_nat = ["PRIMARY_IP_RANGE"]
  }
}

resource "google_compute_firewall" "iap_ssh" {
  count         = var.enabled ? 1 : 0
  project       = var.project
  name          = "${local.name}-iap-ssh"
  network       = google_compute_network.host[0].id
  direction     = "INGRESS"
  priority      = 1000
  source_ranges = ["35.235.240.0/20"]
  target_tags   = [local.name]
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

# GCP's own metadata server is exempt from VPC firewall enforcement. A later
# candidate-side host firewall must block it; this rule is not that proof.
resource "google_compute_firewall" "deny_private" {
  count              = var.enabled ? 1 : 0
  project            = var.project
  name               = "${local.name}-deny-private"
  network            = google_compute_network.host[0].id
  direction          = "EGRESS"
  priority           = 900
  destination_ranges = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"]
  target_tags        = [local.name]
  deny {
    protocol = "all"
  }
}

resource "google_compute_firewall" "host_web" {
  count              = var.enabled ? 1 : 0
  project            = var.project
  name               = "${local.name}-host-web"
  network            = google_compute_network.host[0].id
  direction          = "EGRESS"
  priority           = 1000
  destination_ranges = ["0.0.0.0/0"]
  target_tags        = [local.name]
  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
  }
}

resource "google_compute_firewall" "deny_other_egress" {
  count              = var.enabled ? 1 : 0
  project            = var.project
  name               = "${local.name}-deny-other-egress"
  network            = google_compute_network.host[0].id
  direction          = "EGRESS"
  priority           = 1100
  destination_ranges = ["0.0.0.0/0"]
  target_tags        = [local.name]
  deny {
    protocol = "all"
  }
}

resource "google_service_account" "host" {
  count        = var.enabled ? 1 : 0
  project      = var.project
  account_id   = local.name
  display_name = "Platform Coding native v2 qualification host"
}

resource "google_project_iam_member" "telemetry" {
  for_each = var.enabled ? toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
  ]) : toset([])
  project = var.project
  role    = each.value
  member  = "serviceAccount:${google_service_account.host[0].email}"
}

module "host" {
  source   = "../compute/gcp"
  count    = var.enabled ? 1 : 0
  project  = var.project
  name     = local.name
  size     = "validator-prod"
  image    = "debian-13"
  location = var.zone

  subnetwork                  = google_compute_subnetwork.host[0].id
  network_tags                = [local.name]
  assign_public_ip            = false
  boot_disk_gb                = var.boot_disk_gb
  service_account_email       = google_service_account.host[0].email
  enable_os_login             = true
  enable_secure_boot          = true
  enable_vtpm                 = true
  enable_integrity_monitoring = true
  labels = {
    env     = "prod"
    role    = "coding_hosted"
    owner   = "platform"
    managed = "terraform"
  }

  # Do not boot a host in the window before its explicit egress policy exists.
  depends_on = [
    google_compute_firewall.deny_private,
    google_compute_firewall.host_web,
    google_compute_firewall.deny_other_egress,
  ]
}

resource "google_compute_instance_iam_member" "osadmin" {
  for_each      = local.operators
  project       = var.project
  zone          = var.zone
  instance_name = module.host[0].hostname
  role          = "roles/compute.osAdminLogin"
  member        = each.value
}

# The stack apply service account cannot administer instance-level IAP IAM.
# IAP TCP conditions expose destination.ip/port, not the Compute resource.name.
# Bind to the host's actual primary private address; no hostname inference.
resource "google_project_iam_member" "ssh" {
  for_each = local.operators
  project  = var.project
  role     = "roles/iap.tunnelResourceAccessor"
  member   = each.value

  condition {
    title       = "only_${replace(local.name, "-", "_")}_ssh"
    description = "IAP SSH only to the qualification host's private destination IP."
    expression  = "destination.ip == '${module.host[0].internal_ip}' && destination.port == 22"
  }
}

resource "google_service_account_iam_member" "actas" {
  for_each           = local.operators
  service_account_id = google_service_account.host[0].name
  role               = "roles/iam.serviceAccountUser"
  member             = each.value
}
