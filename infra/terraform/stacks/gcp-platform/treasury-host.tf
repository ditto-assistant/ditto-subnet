# Private, disabled-by-default signing boundary. No wallet bytes or secret
# versions enter Terraform state. Neither Platform nor Backroom can read this
# secret or impersonate this service account.
variable "enable_treasury_host" {
  description = "Provision the isolated SN118 treasury signer host after custody review."
  type        = bool
  default     = false
}

variable "treasury_operator_email" {
  description = "Peyton's reviewed Google identity for IAP OS Login on the signer."
  type        = string
  default     = ""

  validation {
    condition     = !var.enable_treasury_host || can(regex("^[^@]+@[^@]+$", var.treasury_operator_email))
    error_message = "An explicit operator email is required to enable the treasury host."
  }
}

resource "google_service_account" "treasury_signer" {
  count        = var.enable_treasury_host ? 1 : 0
  project      = var.project
  account_id   = "sn118-treasury-signer"
  display_name = "Isolated SN118 treasury signing host"
}

resource "google_secret_manager_secret" "treasury_signing_key" {
  count     = var.enable_treasury_host ? 1 : 0
  project   = var.project
  secret_id = "sn118-treasury-signing-key"

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_iam_member" "treasury_signer_key" {
  count     = var.enable_treasury_host ? 1 : 0
  project   = var.project
  secret_id = google_secret_manager_secret.treasury_signing_key[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.treasury_signer[0].email}"
}

# Create the role but do not bind it. For the one-time key ceremony, a protected
# operator temporarily binds it on this secret to the signer host SA, then
# removes that binding immediately after the public address is verified.
resource "google_project_iam_custom_role" "treasury_key_provisioner" {
  count       = var.enable_treasury_host ? 1 : 0
  project     = var.project
  role_id     = "sn118TreasuryKeyProvisioner"
  title       = "SN118 treasury key provisioner"
  description = "One-time list/add of the dedicated treasury secret version"
  permissions = ["secretmanager.versions.list", "secretmanager.versions.add"]
}

module "treasury_signer_host" {
  count                 = var.enable_treasury_host ? 1 : 0
  source                = "../../modules/compute/gcp"
  project               = var.project
  name                  = "sn118-treasury-signer"
  size                  = "app-small"
  image                 = "debian-13"
  location              = var.zone
  subnetwork            = module.network.subnetwork_id
  network_tags          = [module.network.ssh_target_tag]
  assign_public_ip      = false
  enable_secure_boot    = true
  service_account_email = google_service_account.treasury_signer[0].email
  labels                = { env = "platform", role = "treasury-signer", managed = "terraform" }
}

resource "google_compute_instance_iam_member" "treasury_operator_osadmin" {
  count         = var.enable_treasury_host ? 1 : 0
  project       = var.project
  zone          = var.zone
  instance_name = module.treasury_signer_host[0].hostname
  role          = "roles/compute.osAdminLogin"
  member        = "user:${var.treasury_operator_email}"
}

resource "google_iap_tunnel_instance_iam_member" "treasury_operator_iap" {
  count    = var.enable_treasury_host ? 1 : 0
  project  = var.project
  zone     = var.zone
  instance = module.treasury_signer_host[0].hostname
  role     = "roles/iap.tunnelResourceAccessor"
  member   = "user:${var.treasury_operator_email}"
}

output "treasury_signer_host" {
  description = "Private signer hostname, empty until a protected Terraform apply."
  value       = var.enable_treasury_host ? module.treasury_signer_host[0].hostname : ""
}

output "treasury_signing_secret_id" {
  description = "Secret container only; key versions must be created outside Terraform."
  value       = var.enable_treasury_host ? google_secret_manager_secret.treasury_signing_key[0].secret_id : ""
}

output "treasury_key_provisioner_role" {
  description = "Unbound one-time key-creation role for protected temporary grant."
  value       = var.enable_treasury_host ? google_project_iam_custom_role.treasury_key_provisioner[0].id : ""
}
