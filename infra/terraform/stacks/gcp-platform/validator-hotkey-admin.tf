###############################################################################
# Disposable production-hotkey generator.
#
# Terraform configuration is the template. `bootstrap` creates one private VM
# long enough to fetch the exact reviewed main revision and frozen dependencies.
# A second protected apply changes only its network tag and attaches the
# add-only Secret Manager grant (`armed`). The armed tag removes general egress:
# only Secret Manager through Google's restricted API VIP remains reachable.
# `absent` deletes the VM, auto-delete boot disk, service account, bindings, and
# per-instance operator access. The firewall policy and empty custom-role
# definition are inert and have no compute cost while no VM carries their tags.
###############################################################################

variable "validator_hotkey_admin_phase" {
  description = "Lifecycle of the disposable hotkey generator: absent, bootstrap dependencies with no secret grant, or armed with restricted egress and add-only access. Never commit a value other than absent."
  type        = string
  default     = "absent"

  validation {
    condition     = contains(["absent", "bootstrap", "armed"], var.validator_hotkey_admin_phase)
    error_message = "validator_hotkey_admin_phase must be absent, bootstrap, or armed."
  }
}

variable "validator_hotkey_admin_revision" {
  description = "Exact reviewed main SHA installed on the disposable generator. Required in bootstrap/armed phases and verified against canonical origin/main before generator execution."
  type        = string
  default     = ""

  validation {
    condition     = var.validator_hotkey_admin_revision == "" || can(regex("^[0-9a-f]{40}$", var.validator_hotkey_admin_revision))
    error_message = "validator_hotkey_admin_revision must be empty or a full lowercase commit SHA."
  }
}

variable "validator_hotkey_admin_image" {
  description = "Exact Google-published Debian image for the disposable generator. Update only through review; image families are forbidden."
  type        = string
  default     = "projects/debian-cloud/global/images/debian-13-trixie-v20260817"

  validation {
    condition     = can(regex("^projects/debian-cloud/global/images/debian-13-trixie-v[0-9]{8}$", var.validator_hotkey_admin_image))
    error_message = "validator_hotkey_admin_image must be an exact Debian 13 image resource, never a family."
  }
}

locals {
  validator_hotkey_admin_active        = var.validator_hotkey_admin_phase != "absent"
  validator_hotkey_admin_armed         = var.validator_hotkey_admin_phase == "armed"
  validator_hotkey_admin_count         = local.validator_hotkey_admin_active ? 1 : 0
  validator_hotkey_admin_base_tag      = "validator-hotkey-admin"
  validator_hotkey_admin_bootstrap_tag = "validator-hotkey-admin-bootstrap"
  validator_hotkey_admin_armed_tag     = "validator-hotkey-admin-armed"
}

check "validator_hotkey_admin_requires_validator_prod" {
  assert {
    condition     = !local.validator_hotkey_admin_active || var.enable_validator_prod
    error_message = "The disposable hotkey admin requires enable_validator_prod=true so its isolated subnet and empty secret already exist."
  }
}

check "validator_hotkey_admin_uses_exact_revision" {
  assert {
    condition     = !local.validator_hotkey_admin_active || can(regex("^[0-9a-f]{40}$", var.validator_hotkey_admin_revision))
    error_message = "bootstrap/armed hotkey admin phases require validator_hotkey_admin_revision at an exact reviewed main SHA."
  }
}

# No dormant principal remains after teardown. The service account is created
# only for the active window and receives no project role or user-managed key.
resource "google_service_account" "validator_hotkey_admin" {
  count        = local.validator_hotkey_admin_count
  project      = var.project
  account_id   = "validator-hotkey-admin"
  display_name = "Disposable validator hotkey generator"
  description  = "Ephemeral add-only identity; Terraform deletes it after hotkey generation."
}

# This role deliberately cannot read payloads, enable/disable/destroy versions,
# mint credentials, or manage IAM. With no active binding it is inert.
resource "google_project_iam_custom_role" "validator_hotkey_generator" {
  count       = local.validator_prod_count
  project     = var.project
  role_id     = "validatorProdHotkeyGenerator"
  title       = "Validator production hotkey generator"
  description = "Inspect one empty secret and add its first version without reading or lifecycle-managing payloads."
  permissions = [
    "secretmanager.secrets.get",
    "secretmanager.versions.add",
    "secretmanager.versions.get",
    "secretmanager.versions.list",
  ]
}

# Attach the add-only permission only after the in-place tag update removes the
# bootstrap internet allow. The explicit dependency prevents a transient window
# where an internet-connected VM could add the mnemonic.
resource "google_secret_manager_secret_iam_member" "validator_hotkey_generator" {
  count     = local.validator_hotkey_admin_armed ? 1 : 0
  project   = var.project
  secret_id = google_secret_manager_secret.validator_prod_hotkey_mnemonic[0].secret_id
  role      = google_project_iam_custom_role.validator_hotkey_generator[0].name
  member    = "serviceAccount:${google_service_account.validator_hotkey_admin[0].email}"

  depends_on = [
    google_compute_firewall.validator_hotkey_admin_bootstrap_egress,
    google_compute_firewall.validator_hotkey_admin_deny_other_egress,
    google_compute_firewall.validator_hotkey_admin_deny_private_egress,
    google_compute_firewall.validator_hotkey_admin_restricted_googleapis,
    google_compute_instance_from_template.validator_hotkey_admin,
  ]
}

# The admin can never reach Platform or any RFC1918 destination, including
# during dependency bootstrap. This rule outranks the temporary internet allow.
resource "google_compute_firewall" "validator_hotkey_admin_deny_private_egress" {
  count              = local.validator_prod_count
  project            = var.project
  name               = "validator-hotkey-admin-deny-private-egress"
  network            = module.network.network_id
  direction          = "EGRESS"
  priority           = 600
  destination_ranges = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
  target_tags        = [local.validator_hotkey_admin_base_tag]

  deny {
    protocol = "all"
  }
}

# Bootstrap has ordinary NAT egress only while no Secret Manager add grant is
# attached. Removing this tag is the first operation in the protected arm apply.
resource "google_compute_firewall" "validator_hotkey_admin_bootstrap_egress" {
  count              = local.validator_prod_count
  project            = var.project
  name               = "validator-hotkey-admin-bootstrap-egress"
  network            = module.network.network_id
  direction          = "EGRESS"
  priority           = 700
  destination_ranges = ["0.0.0.0/0"]
  target_tags        = [local.validator_hotkey_admin_bootstrap_tag]

  allow {
    protocol = "all"
  }
}

# Once armed, only HTTPS to restricted.googleapis.com (199.36.153.4/30) is
# allowed. Secret Manager is available on that VPC-SC-compatible restricted VIP.
resource "google_compute_firewall" "validator_hotkey_admin_restricted_googleapis" {
  count              = local.validator_prod_count
  project            = var.project
  name               = "validator-hotkey-admin-restricted-googleapis"
  network            = module.network.network_id
  direction          = "EGRESS"
  priority           = 800
  destination_ranges = ["199.36.153.4/30"]
  target_tags        = [local.validator_hotkey_admin_base_tag]

  allow {
    protocol = "tcp"
    ports    = ["443"]
  }
}

resource "google_compute_firewall" "validator_hotkey_admin_deny_other_egress" {
  count              = local.validator_prod_count
  project            = var.project
  name               = "validator-hotkey-admin-deny-other-egress"
  network            = module.network.network_id
  direction          = "EGRESS"
  priority           = 900
  destination_ranges = ["0.0.0.0/0"]
  target_tags        = [local.validator_hotkey_admin_base_tag]

  deny {
    protocol = "all"
  }
}

resource "google_compute_instance_template" "validator_hotkey_admin" {
  count        = local.validator_hotkey_admin_count
  project      = var.project
  name_prefix  = "validator-hotkey-admin-"
  description  = "Disposable, two-phase production validator hotkey generator"
  machine_type = "e2-standard-2"
  region       = var.region

  disk {
    source_image = var.validator_hotkey_admin_image
    auto_delete  = true
    boot         = true
    disk_size_gb = 25
    disk_type    = "pd-balanced"
  }

  network_interface {
    subnetwork = google_compute_subnetwork.validator_prod[0].id
    # No access_config: private IP, NAT egress during bootstrap, IAP SSH only.
  }

  tags = [
    module.network.ssh_target_tag,
    local.validator_hotkey_admin_base_tag,
    local.validator_hotkey_admin_bootstrap_tag,
  ]
  labels = {
    env     = "prod"
    role    = "validator-hotkey-admin"
    managed = "terraform"
  }

  metadata = {
    enable-oslogin         = "TRUE"
    block-project-ssh-keys = "TRUE"
    startup-script = templatefile("${path.module}/files/validator-hotkey-admin-startup.sh.tpl", {
      git_revision = var.validator_hotkey_admin_revision
      project      = var.project
      repository   = "https://github.com/ditto-assistant/ditto-subnet.git"
      secret       = google_secret_manager_secret.validator_prod_hotkey_mnemonic[0].secret_id
      uv_sha256    = "49fe42df9f42056037473f3876adec1615709b57d3470ed39178ff420f3afb9f"
      uv_version   = "0.11.28"
      armed_tag    = local.validator_hotkey_admin_armed_tag
    })
  }

  service_account {
    email  = google_service_account.validator_hotkey_admin[0].email
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "google_compute_instance_from_template" "validator_hotkey_admin" {
  count                    = local.validator_hotkey_admin_count
  project                  = var.project
  name                     = "ditto-validator-hotkey-admin"
  zone                     = var.zone
  source_instance_template = google_compute_instance_template.validator_hotkey_admin[0].self_link_unique
  deletion_protection      = false

  # This is the only bootstrap -> armed mutation. Keeping the original source
  # template and revision prevents the boot disk and frozen environment from
  # being replaced during the arm apply.
  tags = compact([
    module.network.ssh_target_tag,
    local.validator_hotkey_admin_base_tag,
    local.validator_hotkey_admin_active && !local.validator_hotkey_admin_armed ? local.validator_hotkey_admin_bootstrap_tag : "",
    local.validator_hotkey_admin_armed ? local.validator_hotkey_admin_armed_tag : "",
  ])

  # The VM must never boot before all deny/allow boundaries exist. The armed
  # secret grant separately waits for both these rules and the atomic tag swap.
  depends_on = [
    google_compute_firewall.validator_hotkey_admin_bootstrap_egress,
    google_compute_firewall.validator_hotkey_admin_deny_other_egress,
    google_compute_firewall.validator_hotkey_admin_deny_private_egress,
    google_compute_firewall.validator_hotkey_admin_restricted_googleapis,
  ]
}

# Human access exists only while the disposable instance exists. The exact-SA
# actAs grant required by OS Login is scoped below and removed at teardown.
resource "google_compute_instance_iam_member" "validator_hotkey_admin_operator_osadmin" {
  for_each      = local.validator_hotkey_admin_active ? var.validator_prod_operators : toset([])
  project       = var.project
  zone          = var.zone
  instance_name = google_compute_instance_from_template.validator_hotkey_admin[0].name
  role          = "roles/compute.osAdminLogin"
  member        = each.value
}

resource "google_iap_tunnel_instance_iam_member" "validator_hotkey_admin_operator_iap" {
  for_each = local.validator_hotkey_admin_active ? var.validator_prod_operators : toset([])
  project  = var.project
  zone     = var.zone
  instance = google_compute_instance_from_template.validator_hotkey_admin[0].name
  role     = "roles/iap.tunnelResourceAccessor"
  member   = each.value

  depends_on = [google_project_service.iap]
}

# OS Login requires actAs when a VM has an attached service account. Scope that
# authority to this disposable identity and remove it with the active phase.
resource "google_service_account_iam_member" "validator_hotkey_admin_operator_actas" {
  for_each           = local.validator_hotkey_admin_active ? var.validator_prod_operators : toset([])
  service_account_id = google_service_account.validator_hotkey_admin[0].name
  role               = "roles/iam.serviceAccountUser"
  member             = each.value
}

output "validator_hotkey_admin_instance" {
  description = "Disposable generator instance name, empty when absent."
  value       = local.validator_hotkey_admin_active ? google_compute_instance_from_template.validator_hotkey_admin[0].name : ""
}

output "validator_hotkey_admin_phase" {
  description = "Planned lifecycle phase of the disposable generator."
  value       = var.validator_hotkey_admin_phase
}
