###############################################################################
# Team SSH access to the platform VMs — OS Login + IAP, with each member's own
# Google identity. No local accounts, no passwords. Grants are scoped to THESE
# three VMs (per-instance), not the whole project, and carry sudo (osAdminLogin).
#
# Populate var.ssh_users with "user:<email>" members; empty (default) = no grants.
# How to connect is documented in README.md ("SSH access").
###############################################################################

# The per-instance IAP tunnel IAM bindings need the IAP API enabled.
resource "google_project_service" "iap" {
  project            = var.project
  service            = "iap.googleapis.com"
  disable_on_destroy = false
}

locals {
  ssh_vms = {
    pg   = module.pg_vm.hostname
    dev  = module.app["dev"].hostname
    prod = module.app["prod"].hostname
  }

  # member × VM cartesian product, keyed uniquely.
  ssh_grants = {
    for pair in setproduct(var.ssh_users, keys(local.ssh_vms)) :
    "${pair[0]}::${pair[1]}" => { member = pair[0], vm = local.ssh_vms[pair[1]] }
  }
}

# Sudo SSH login on the instance (OS Login).
resource "google_compute_instance_iam_member" "ssh_osadmin" {
  for_each      = local.ssh_grants
  project       = var.project
  zone          = var.zone
  instance_name = each.value.vm
  role          = "roles/compute.osAdminLogin"
  member        = each.value.member
}

# Permission to open the IAP tunnel to the instance (SSH is IAP-only — the
# network module allows :22 only from the IAP range).
resource "google_iap_tunnel_instance_iam_member" "ssh_iap" {
  for_each   = local.ssh_grants
  project    = var.project
  zone       = var.zone
  instance   = each.value.vm
  role       = "roles/iap.tunnelResourceAccessor"
  member     = each.value.member
  depends_on = [google_project_service.iap]
}

# gcloud compute ssh --tunnel-through-iap to a VM with an attached service
# account needs actAs on that SA. App VMs run as platform_api; postgres has none.
resource "google_service_account_iam_member" "ssh_user_actas_platform_api" {
  for_each           = toset(var.ssh_users)
  service_account_id = google_service_account.platform_api.name
  role               = "roles/iam.serviceAccountUser"
  member             = each.value
}
