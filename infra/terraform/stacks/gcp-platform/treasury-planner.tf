# The daily credit observer has its own private host and identity. Its GM API
# credential cannot sign chain transactions, and its service account has no
# access to the treasury signing secret.
variable "enable_treasury_planner_host" {
  description = "Provision the isolated SN118 treasury credit planner after review."
  type        = bool
  default     = false
}

resource "google_service_account" "treasury_planner" {
  count        = var.enable_treasury_planner_host ? 1 : 0
  project      = var.project
  account_id   = "sn118-treasury-planner"
  display_name = "Isolated SN118 treasury credit planner"
}

resource "google_secret_manager_secret" "treasury_gm_read_key" {
  count     = var.enable_treasury_planner_host ? 1 : 0
  project   = var.project
  secret_id = "sn118-treasury-gm-read-key"

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_iam_member" "treasury_planner_gm_read_key" {
  count     = var.enable_treasury_planner_host ? 1 : 0
  project   = var.project
  secret_id = google_secret_manager_secret.treasury_gm_read_key[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.treasury_planner[0].email}"
}

module "treasury_planner_host" {
  count                 = var.enable_treasury_planner_host ? 1 : 0
  source                = "../../modules/compute/gcp"
  project               = var.project
  name                  = "sn118-treasury-planner"
  size                  = "app-small"
  image                 = "debian-13"
  location              = var.zone
  subnetwork            = module.network.subnetwork_id
  network_tags          = [module.network.ssh_target_tag]
  assign_public_ip      = false
  enable_secure_boot    = true
  service_account_email = google_service_account.treasury_planner[0].email
  labels                = { env = "platform", role = "treasury-planner", managed = "terraform" }
}

resource "google_compute_instance_iam_member" "treasury_planner_operator_osadmin" {
  count         = var.enable_treasury_planner_host ? 1 : 0
  project       = var.project
  zone          = var.zone
  instance_name = module.treasury_planner_host[0].hostname
  role          = "roles/compute.osAdminLogin"
  member        = "user:${var.treasury_operator_email}"
}

resource "google_iap_tunnel_instance_iam_member" "treasury_planner_operator_iap" {
  count    = var.enable_treasury_planner_host ? 1 : 0
  project  = var.project
  zone     = var.zone
  instance = module.treasury_planner_host[0].hostname
  role     = "roles/iap.tunnelResourceAccessor"
  member   = "user:${var.treasury_operator_email}"
}

output "treasury_planner_host" {
  description = "Private planner hostname, empty until a protected Terraform apply."
  value       = var.enable_treasury_planner_host ? module.treasury_planner_host[0].hostname : ""
}

output "treasury_gm_read_secret_id" {
  description = "GM read-key container only; key versions are created outside Terraform."
  value       = var.enable_treasury_planner_host ? google_secret_manager_secret.treasury_gm_read_key[0].secret_id : ""
}
