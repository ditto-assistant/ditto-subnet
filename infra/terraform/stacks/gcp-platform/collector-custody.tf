# Collector custody only. No mnemonic/version payload in Terraform, no chain
# operation, no collector unit or activation marker. Independent protected plans
# advance each host bootstrap -> armed -> locked -> sealed; never skip bootstrap.
variable "enable_collector_custody" {
  type        = bool
  default     = false
  description = "Provision two isolated collector delegate custody hosts after reviewed plan/approval."
}

variable "collector_custody_phases" {
  type        = object({ registration = string, transfer = string })
  default     = { registration = "bootstrap", transfer = "bootstrap" }
  description = "Independent bootstrap/armed/locked/sealed phases. Locked removes writer authority before a separate sealed apply grants version 1 access."
  validation {
    condition     = alltrue([for phase in values(var.collector_custody_phases) : contains(["bootstrap", "armed", "locked", "sealed"], phase)])
    error_message = "Only bootstrap, armed, locked and sealed custody phases are supported."
  }
}

variable "collector_custody_revision" {
  type        = string
  default     = ""
  description = "Exact reviewed main commit with the collector ceremony; dependencies use its frozen lock."
  validation {
    condition     = var.collector_custody_revision == "" || can(regex("^[0-9a-f]{40}$", var.collector_custody_revision))
    error_message = "Revision must be empty while disabled or a full SHA."
  }
}

variable "collector_custody_operator" {
  type        = string
  default     = ""
  description = "Explicit signed-in operator email for per-instance IAP/OS Admin Login."
}

variable "collector_custody_offline_addresses" {
  type        = list(string)
  default     = []
  description = "Five distinct public collector cold/hot and holding addresses; never secret input."
  validation {
    condition = length(var.collector_custody_offline_addresses) == 0 || (
      length(var.collector_custody_offline_addresses) == 5 &&
      length(toset(var.collector_custody_offline_addresses)) == 5 &&
      alltrue([for address in var.collector_custody_offline_addresses : can(regex("^5[1-9A-HJ-NP-Za-km-z]{47}$", address))])
    )
    error_message = "Supply five distinct SS58 public addresses or an empty disabled input."
  }
}

variable "collector_custody_image" {
  type        = string
  default     = "projects/debian-cloud/global/images/debian-13-trixie-v20260817"
  description = "Exact Google Debian image resource; never a floating family."
  validation {
    condition     = can(regex("^projects/debian-cloud/global/images/debian-13-trixie-v[0-9]{8}$", var.collector_custody_image))
    error_message = "Pin an exact Debian 13 image."
  }
}

locals {
  collector_roles     = var.enable_collector_custody ? var.collector_custody_phases : {}
  collector_bootstrap = anytrue([for phase in values(local.collector_roles) : phase == "bootstrap"])
}

check "collector_custody_explicit_inputs" {
  assert {
    condition = !var.enable_collector_custody || (
      can(regex("^[0-9a-f]{40}$", var.collector_custody_revision)) &&
      can(regex("^[^@]+@[^@]+$", var.collector_custody_operator)) &&
      length(var.collector_custody_offline_addresses) == 5 &&
      length(toset(var.collector_custody_offline_addresses)) == 5 &&
      alltrue([for address in var.collector_custody_offline_addresses : can(regex("^5[1-9A-HJ-NP-Za-km-z]{47}$", address))]) &&
      !var.enable_treasury_host
    )
    error_message = "Custody needs exact reviewed SHA/operator/five public identities and must not use the legacy signer."
  }
}

data "google_project" "collector_custody" {
  count      = var.enable_collector_custody ? 1 : 0
  project_id = var.project
}

# Dedicated VPC; deny inter-host/RFC1918 access even during bootstrap. No shared
# Platform network, external IP, project SSH keys or default compute identity.
resource "google_compute_network" "collector_custody" {
  count                   = var.enable_collector_custody ? 1 : 0
  project                 = var.project
  name                    = "sn118-collector-custody"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "collector_custody" {
  count                    = var.enable_collector_custody ? 1 : 0
  project                  = var.project
  name                     = "sn118-collector-custody"
  region                   = var.region
  network                  = google_compute_network.collector_custody[0].id
  ip_cidr_range            = "10.81.118.0/24"
  private_ip_google_access = true
}

resource "google_compute_router" "collector_custody" {
  count   = local.collector_bootstrap ? 1 : 0
  project = var.project
  name    = "sn118-collector-bootstrap"
  region  = var.region
  network = google_compute_network.collector_custody[0].id
}

resource "google_compute_router_nat" "collector_custody" {
  count                              = local.collector_bootstrap ? 1 : 0
  project                            = var.project
  name                               = "sn118-collector-bootstrap"
  region                             = var.region
  router                             = google_compute_router.collector_custody[0].name
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"
  subnetwork {
    name                    = google_compute_subnetwork.collector_custody[0].id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }
}

resource "google_compute_firewall" "collector_iap" {
  count         = var.enable_collector_custody ? 1 : 0
  project       = var.project
  name          = "sn118-collector-iap"
  network       = google_compute_network.collector_custody[0].id
  direction     = "INGRESS"
  source_ranges = ["35.235.240.0/20"]
  target_tags   = ["collector-custody"]
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

resource "google_compute_firewall" "collector_deny_private" {
  count              = var.enable_collector_custody ? 1 : 0
  project            = var.project
  name               = "sn118-collector-deny-private"
  network            = google_compute_network.collector_custody[0].id
  direction          = "EGRESS"
  priority           = 600
  target_tags        = ["collector-custody"]
  destination_ranges = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
  deny { protocol = "all" }
}

resource "google_compute_firewall" "collector_bootstrap" {
  for_each           = { for role, phase in local.collector_roles : role => phase if phase == "bootstrap" }
  project            = var.project
  name               = "sn118-collector-${each.key}-bootstrap"
  network            = google_compute_network.collector_custody[0].id
  direction          = "EGRESS"
  priority           = 700
  target_tags        = ["collector-${each.key}-bootstrap"]
  destination_ranges = ["0.0.0.0/0"]
  allow { protocol = "all" }
}

resource "google_compute_firewall" "collector_googleapis" {
  count              = var.enable_collector_custody ? 1 : 0
  project            = var.project
  name               = "sn118-collector-restricted-googleapis"
  network            = google_compute_network.collector_custody[0].id
  direction          = "EGRESS"
  priority           = 800
  target_tags        = ["collector-custody"]
  destination_ranges = ["199.36.153.4/30"]
  allow {
    protocol = "tcp"
    ports    = ["443"]
  }
}

resource "google_compute_firewall" "collector_deny_other" {
  count              = var.enable_collector_custody ? 1 : 0
  project            = var.project
  name               = "sn118-collector-deny-other"
  network            = google_compute_network.collector_custody[0].id
  direction          = "EGRESS"
  priority           = 900
  target_tags        = ["collector-custody"]
  destination_ranges = ["0.0.0.0/0"]
  deny { protocol = "all" }
}

resource "google_service_account" "collector_delegate" {
  for_each     = local.collector_roles
  project      = var.project
  account_id   = "sn118-collector-${each.key}"
  display_name = "Isolated SN118 ${each.key} delegate"
}

resource "google_secret_manager_secret" "collector_delegate" {
  for_each  = local.collector_roles
  project   = var.project
  secret_id = "sn118-collector-${each.key}-delegate"
  replication {
    auto {}
  }
  lifecycle { prevent_destroy = true }
}

resource "google_project_iam_custom_role" "collector_generator" {
  count       = var.enable_collector_custody ? 1 : 0
  project     = var.project
  role_id     = "sn118CollectorDelegateGenerator"
  title       = "Collector delegate first-version writer"
  permissions = ["secretmanager.versions.add", "secretmanager.versions.list"]
}

resource "google_compute_instance" "collector_delegate" {
  for_each            = local.collector_roles
  project             = var.project
  zone                = var.zone
  name                = "sn118-collector-${each.key}-signer"
  machine_type        = "e2-standard-2"
  tags                = ["collector-custody", "collector-${each.key}-${each.value}"]
  labels              = { role = "collector-${each.key}", managed = "terraform" }
  deletion_protection = true
  boot_disk {
    auto_delete = false
    initialize_params {
      image = var.collector_custody_image
      size  = 30
      type  = "pd-balanced"
    }
  }
  network_interface { subnetwork = google_compute_subnetwork.collector_custody[0].id }
  service_account {
    email  = google_service_account.collector_delegate[each.key].email
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  }
  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }
  metadata = {
    enable-oslogin         = "TRUE"
    block-project-ssh-keys = "TRUE"
    startup-script = templatefile("${path.module}/files/collector-custody-startup.sh.tpl", {
      project           = var.project, role = each.key, git_revision = var.collector_custody_revision,
      offline_addresses = join(" ", var.collector_custody_offline_addresses),
    })
  }
  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = can(regex("^[0-9a-f]{40}$", var.collector_custody_revision)) && length(var.collector_custody_offline_addresses) == 5 && can(regex("^[^@]+@[^@]+$", var.collector_custody_operator)) && can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project)) && !var.enable_treasury_host
      error_message = "Never boot custody without exact source and offline public bindings."
    }
  }
  depends_on = [google_compute_firewall.collector_deny_private, google_compute_firewall.collector_deny_other, google_compute_firewall.collector_googleapis, google_compute_firewall.collector_iap]
}

# Per-secret, add/list only; no access, lifecycle, admin or impersonation. The
# host tag update finishes before the add grant. Bootstrap egress rules are also
# reconciled first; an armed role has no internet allow even if its peer boots.
resource "google_secret_manager_secret_iam_member" "collector_generator" {
  for_each   = { for role, phase in local.collector_roles : role => phase if phase == "armed" }
  project    = var.project
  secret_id  = google_secret_manager_secret.collector_delegate[each.key].secret_id
  role       = google_project_iam_custom_role.collector_generator[0].name
  member     = "serviceAccount:${google_service_account.collector_delegate[each.key].email}"
  depends_on = [google_compute_instance.collector_delegate, google_compute_firewall.collector_bootstrap]
}

# An intermediate locked apply removes the add grant. Sealed identity can read only its own explicit
# first version. Pin numeric PROJECT NUMBER, the resource.name IAM contract.
resource "google_secret_manager_secret_iam_member" "collector_reader" {
  for_each  = { for role, phase in local.collector_roles : role => phase if phase == "sealed" }
  project   = var.project
  secret_id = google_secret_manager_secret.collector_delegate[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.collector_delegate[each.key].email}"
  condition {
    title      = "own-first-version-only"
    expression = "resource.name == 'projects/${data.google_project.collector_custody[0].number}/secrets/sn118-collector-${each.key}-delegate/versions/1'"
  }
  depends_on = [google_secret_manager_secret_iam_member.collector_generator, google_compute_instance.collector_delegate]
}

resource "google_compute_instance_iam_member" "collector_operator" {
  for_each      = local.collector_roles
  project       = var.project
  zone          = var.zone
  instance_name = google_compute_instance.collector_delegate[each.key].name
  role          = "roles/compute.osAdminLogin"
  member        = "user:${var.collector_custody_operator}"
}

# OS Login on a VM with an attached identity also requires actAs on that exact
# service account. This does not grant token creation or project-level access.
resource "google_service_account_iam_member" "collector_operator" {
  for_each           = local.collector_roles
  service_account_id = google_service_account.collector_delegate[each.key].name
  role               = "roles/iam.serviceAccountUser"
  member             = "user:${var.collector_custody_operator}"
}

resource "google_iap_tunnel_instance_iam_member" "collector_operator" {
  for_each = local.collector_roles
  project  = var.project
  zone     = var.zone
  instance = google_compute_instance.collector_delegate[each.key].name
  role     = "roles/iap.tunnelResourceAccessor"
  member   = "user:${var.collector_custody_operator}"
}

output "collector_custody" {
  value = { for role, host in google_compute_instance.collector_delegate : role => {
    host              = host.name, service_account = google_service_account.collector_delegate[role].email,
    secret            = google_secret_manager_secret.collector_delegate[role].secret_id,
    phase             = var.collector_custody_phases[role], version = 1,
    runtime_activated = false,
  } }
}
