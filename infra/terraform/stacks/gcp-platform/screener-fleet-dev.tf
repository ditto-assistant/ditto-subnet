###############################################################################
# Disposable screener-host rehearsal VM.
#
# This is deliberately separate from the production GCE overflow MIG. It is a
# 64 GB Intel L1 host with nested KVM, used to rehearse the public Hetzner
# Ansible role before touching the Robot server. It receives no screener
# identity or Platform secret and is absent by default.
###############################################################################

variable "enable_screener_fleet_dev_host" {
  description = "Create the disposable nested-KVM host used to rehearse the dedicated screener Ansible role. False is the sealed/default state."
  type        = bool
  default     = false
}

variable "screener_fleet_dev_host_name" {
  description = "Name of the disposable dedicated-host rehearsal VM."
  type        = string
  default     = "subnet-screener-dev-1"

  validation {
    condition     = can(regex("^subnet-screener-dev-[1-9][0-9]*$", var.screener_fleet_dev_host_name))
    error_message = "screener_fleet_dev_host_name must match subnet-screener-dev-N."
  }
}

variable "screener_fleet_dev_host_machine_type" {
  description = "Intel 64 GB rehearsal shape. N2 is used because nested KVM is not available on the production N2D overflow shape."
  type        = string
  default     = "n2-standard-16"
}

variable "screener_fleet_dev_host_boot_disk_gb" {
  description = "Disposable boot disk for packages, guest base images, and KVM overlays."
  type        = number
  default     = 100

  validation {
    condition     = var.screener_fleet_dev_host_boot_disk_gb >= 100
    error_message = "screener_fleet_dev_host_boot_disk_gb must be at least 100 GB."
  }
}

locals {
  screener_fleet_dev_host_count = var.enable_screener_fleet_dev_host ? 1 : 0
  screener_fleet_dev_ssh_users  = var.enable_screener_fleet_dev_host ? toset(var.ssh_users) : toset([])
}

resource "google_compute_instance" "screener_fleet_dev_host" {
  count        = local.screener_fleet_dev_host_count
  project      = var.project
  zone         = var.zone
  name         = var.screener_fleet_dev_host_name
  machine_type = var.screener_fleet_dev_host_machine_type

  # N2's minimum platform is Cascade Lake; it exceeds GCE's Haswell-or-newer
  # nested-virtualization requirement without requesting a rarer CPU tier.
  min_cpu_platform = "Intel Cascade Lake"

  advanced_machine_features {
    enable_nested_virtualization = true
  }

  boot_disk {
    auto_delete = true
    initialize_params {
      image = "projects/debian-cloud/global/images/family/debian-12"
      size  = var.screener_fleet_dev_host_boot_disk_gb
      type  = "pd-balanced"
    }
  }

  network_interface {
    subnetwork = module.network.subnetwork_id
    # No access_config: egress uses Cloud NAT and SSH uses IAP.
  }

  tags = [module.network.ssh_target_tag]
  labels = {
    env       = "dev"
    role      = "screener-fleet-dev"
    managed   = "terraform"
    ephemeral = "true"
  }

  metadata = {
    enable-oslogin = "TRUE"
  }

  scheduling {
    automatic_restart   = true
    on_host_maintenance = "MIGRATE"
  }

  deletion_protection = false
}

resource "google_compute_instance_iam_member" "screener_fleet_dev_host_osadmin" {
  for_each      = local.screener_fleet_dev_ssh_users
  project       = var.project
  zone          = var.zone
  instance_name = google_compute_instance.screener_fleet_dev_host[0].name
  role          = "roles/compute.osAdminLogin"
  member        = each.value
}

// The stack apply service account cannot administer instance-level IAP IAM.
// Keep this project-level binding as narrow as the discarded VM resource by
// conditioning it on the exact rehearsal hostname.
resource "google_project_iam_member" "screener_fleet_dev_host_iap" {
  for_each = local.screener_fleet_dev_ssh_users
  project  = var.project
  role     = "roles/iap.tunnelResourceAccessor"
  member   = each.value

  condition {
    title       = "only_${replace(var.screener_fleet_dev_host_name, "-", "_")}"
    description = "IAP access only to the disposable screener fleet rehearsal host."
    expression  = "resource.name.extract('/instances/{name}') == '${var.screener_fleet_dev_host_name}'"
  }

  depends_on = [google_project_service.iap]
}

output "screener_fleet_dev_host" {
  description = "Disposable rehearsal host name; empty in the sealed/default state."
  value       = var.enable_screener_fleet_dev_host ? google_compute_instance.screener_fleet_dev_host[0].name : ""
}

output "screener_fleet_dev_host_internal_ip" {
  description = "Private address of the disposable rehearsal host; use IAP for SSH."
  value       = var.enable_screener_fleet_dev_host ? google_compute_instance.screener_fleet_dev_host[0].network_interface[0].network_ip : ""
}
