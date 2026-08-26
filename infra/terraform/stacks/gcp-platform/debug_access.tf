###############################################################################
# Least-privilege GCP access for repo debug skills (gcloud-ditto-readonly,
# ditto-subnet-runtime-profiling). Distinct from ssh_users, which is sudo SSH
# on postgres + both app VMs.
#
# Required by those skills:
#   * IAP + osAdminLogin on ditto-platform-prod only — query_prod_db.sh runs
#     `sudo -n -u deploy` to source /opt/ditto-platform/.env; py-spy uses sudo
#     for ptrace; pprofctl wraps IAP SSH to loopback profilers.
#   * secretAccessor on TARGON_API_KEY only — query_targon.sh streams the key
#     into targon_cli. No other Secret Manager secrets.
#
# Not granted: project Editor/Owner, IAM admin, Cloud Run mutate, instance
# start/stop, postgres or platform-dev SSH, Platform DB/admin/OpenRouter
# secrets.
###############################################################################

locals {
  debug_operators = toset(var.debug_operators)

  # ssh_users already has prod osAdminLogin + IAP; do not double-manage it.
  debug_prod_ssh = setsubtract(local.debug_operators, toset(var.ssh_users))
}

resource "google_compute_instance_iam_member" "debug_operator_osadmin" {
  for_each      = local.debug_prod_ssh
  project       = var.project
  zone          = var.zone
  instance_name = module.app["prod"].hostname
  role          = "roles/compute.osAdminLogin"
  member        = each.value
}

resource "google_iap_tunnel_instance_iam_member" "debug_operator_iap" {
  for_each   = local.debug_prod_ssh
  project    = var.project
  zone       = var.zone
  instance   = module.app["prod"].hostname
  role       = "roles/iap.tunnelResourceAccessor"
  member     = each.value
  depends_on = [google_project_service.iap]
}

resource "google_secret_manager_secret_iam_member" "debug_operator_targon" {
  for_each  = local.debug_operators
  project   = var.project
  secret_id = data.google_secret_manager_secret.targon_api_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = each.value
}
