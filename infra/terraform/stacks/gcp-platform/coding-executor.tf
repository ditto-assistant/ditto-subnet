###############################################################################
# Dedicated shadow-coding executor cohort.
#
# This is a structural boundary only. The default count is zero, the runtime
# identity has no Secret Manager or provider access, and this file does not
# install a Docker daemon, start a validator, issue a ticket, or enable any
# coding gate. A later reviewed Ansible role provisions the rootless executor
# on a host created by this optional cohort.
###############################################################################

variable "coding_executor_host_count" {
  description = "Number of isolated shadow-coding executor hosts. Zero keeps the cohort absent; contract v1 requires exactly three hosts for a future k=3 shadow canary."
  type        = number
  default     = 0

  validation {
    condition     = var.coding_executor_host_count == 0 || var.coding_executor_host_count == 3
    error_message = "coding_executor_host_count must be 0 or 3."
  }
}

variable "coding_executor_boot_disk_gb" {
  description = "Boot disk for each future coding executor. The rootless daemon, immutable runtime images, and bounded outbox require at least 200 GB."
  type        = number
  default     = 200

  validation {
    condition     = var.coding_executor_boot_disk_gb >= 200
    error_message = "coding_executor_boot_disk_gb must be at least 200 GB."
  }
}

variable "coding_executor_subnet_cidr" {
  description = "Dedicated subnet for the optional coding executor cohort. It must not overlap the Platform or production-validator source ranges."
  type        = string
  default     = "10.32.0.0/24"

  validation {
    condition = (
      var.coding_executor_subnet_cidr != var.subnet_cidr &&
      var.coding_executor_subnet_cidr != var.validator_prod_subnet_cidr
    )
    error_message = "coding_executor_subnet_cidr must differ from Platform and production-validator subnets."
  }
}

variable "coding_executor_operators" {
  description = "Small explicit set of sudo OS Login/IAP custodians for the isolated coding executor hosts. These hosts carry no wallet or cloud secret access."
  type        = set(string)
  default = [
    "user:omar@omniaura.ai",
    "user:peyton@omniaura.ai",
  ]
}

locals {
  coding_executor_enabled     = var.coding_executor_host_count == 3
  coding_executor_network_tag = "coding-executor"
  coding_executor_vms = {
    for index, host in module.coding_executor_vm : tostring(index) => host.hostname
  }
  coding_executor_operator_instances = {
    for pair in setproduct(var.coding_executor_operators, keys(local.coding_executor_vms)) :
    "${pair[0]}::${pair[1]}" => { member = pair[0], index = pair[1] }
  }
}

resource "google_compute_subnetwork" "coding_executor" {
  count                    = local.coding_executor_enabled ? 1 : 0
  project                  = var.project
  name                     = "ditto-coding-executor-${var.region}"
  region                   = var.region
  network                  = module.network.network_id
  ip_cidr_range            = var.coding_executor_subnet_cidr
  private_ip_google_access = true
}

# A hostile coding container must never obtain an internal route to Platform or
# the production-validator subnet after a future daemon/container escape. The
# later rootless-executor role adds its narrower candidate egress policy; this
# VM-level rule keeps the private-plane boundary independent of it.
resource "google_compute_firewall" "coding_executor_deny_private_egress" {
  count              = local.coding_executor_enabled ? 1 : 0
  project            = var.project
  name               = "ditto-coding-executor-deny-private-egress"
  network            = module.network.network_id
  direction          = "EGRESS"
  priority           = 900
  destination_ranges = [var.subnet_cidr, var.validator_prod_subnet_cidr]
  target_tags        = [local.coding_executor_network_tag]

  deny {
    protocol = "all"
  }
}

# This identity intentionally receives only telemetry permissions. In
# particular it has no Secret Manager, Artifact Registry, Platform, provider,
# storage, or service-account impersonation grant. Future execution material is
# delivered through narrowly scoped, ticket-bound capabilities rather than VM
# identity credentials.
resource "google_service_account" "coding_executor" {
  count        = local.coding_executor_enabled ? 1 : 0
  project      = var.project
  account_id   = "ditto-coding-executor"
  display_name = "Ditto shadow coding executor runtime"
}

resource "google_project_iam_member" "coding_executor_observability" {
  for_each = local.coding_executor_enabled ? toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
  ]) : toset([])
  project = var.project
  role    = each.value
  member  = "serviceAccount:${google_service_account.coding_executor[0].email}"
}

module "coding_executor_vm" {
  source   = "../../modules/compute/gcp"
  count    = var.coding_executor_host_count
  project  = var.project
  name     = "ditto-coding-executor-${count.index + 1}"
  size     = "validator-prod"
  image    = "debian-13"
  location = var.zone

  subnetwork       = google_compute_subnetwork.coding_executor[0].id
  network_tags     = [module.network.ssh_target_tag, local.coding_executor_network_tag]
  assign_public_ip = false
  boot_disk_gb     = var.coding_executor_boot_disk_gb

  service_account_email       = google_service_account.coding_executor[0].email
  enable_secure_boot          = true
  enable_vtpm                 = true
  enable_integrity_monitoring = true
  labels = {
    env     = "prod"
    role    = "coding_executor"
    managed = "terraform"
  }
}

# The executor cohort has no wallet or secret to custody, but its operators are
# scoped to exactly these instances and to the otherwise-empty runtime identity.
resource "google_compute_instance_iam_member" "coding_executor_operator_osadmin" {
  for_each      = local.coding_executor_operator_instances
  project       = var.project
  zone          = var.zone
  instance_name = local.coding_executor_vms[each.value.index]
  role          = "roles/compute.osAdminLogin"
  member        = each.value.member
}

resource "google_project_iam_member" "coding_executor_operator_iap" {
  for_each = local.coding_executor_operator_instances
  project  = var.project
  role     = "roles/iap.tunnelResourceAccessor"
  member   = each.value.member

  condition {
    title       = "coding_executor_exact_instance_${each.value.index}"
    description = "IAP access only to one dedicated coding executor host."
    expression  = "resource.name.extract('/instances/{name}') == '${local.coding_executor_vms[each.value.index]}'"
  }

  depends_on = [google_project_service.iap]
}

resource "google_project_iam_member" "coding_executor_operator_compute_viewer" {
  for_each = local.coding_executor_enabled ? var.coding_executor_operators : toset([])
  project  = var.project
  role     = "roles/compute.viewer"
  member   = each.value
}

resource "google_service_account_iam_member" "coding_executor_operator_actas" {
  for_each           = local.coding_executor_enabled ? var.coding_executor_operators : toset([])
  service_account_id = google_service_account.coding_executor[0].name
  role               = "roles/iam.serviceAccountUser"
  member             = each.value
}

output "coding_executor_vm_names" {
  description = "Dedicated shadow-coding executor VM names. Empty unless the explicit three-host cohort is enabled."
  value       = local.coding_executor_vms
}

output "coding_executor_vm_internal_ips" {
  description = "Dedicated shadow-coding executor private IPs. Reachability is through IAP only."
  value = {
    for index, host in module.coding_executor_vm : tostring(index) => host.internal_ip
  }
}
