###############################################################################
# Day-2 screener deployment identity.
#
# One narrowly-scoped prod-environment principal discovers screener instances
# and IAP-SSHes to workers plus the capacity-controller VM. It cannot resize
# the fleet, access Secret Manager directly, write Artifact Registry, or mutate
# other GCP resources. OS Admin Login intentionally grants root on those VMs,
# including access to their local 0600 runtime files; do not treat this identity
# as isolated from credentials already materialized on a target. Runtime code
# deploys remain automatic; creating/changing this identity still goes through
# the protected Terraform plan/apply workflow.
###############################################################################

resource "google_service_account" "screener_deploy" {
  project      = var.project
  account_id   = "github-actions-screener-deploy"
  display_name = "GitHub Actions Screener Runtime Deploy"
}

resource "google_project_iam_member" "screener_deploy_roles" {
  for_each = toset([
    "roles/compute.osAdminLogin",
    "roles/compute.viewer",
    "roles/iap.tunnelResourceAccessor",
  ])
  project = var.project
  role    = each.value
  member  = "serviceAccount:${google_service_account.screener_deploy.email}"
}

resource "google_service_account_iam_member" "screener_deploy_wif" {
  service_account_id = google_service_account.screener_deploy.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.wif_pool_id}/subject/repo:ditto-assistant/ditto-subnet:environment:${var.platform_deploy_environment}"
}

resource "google_service_account_iam_member" "screener_deploy_depot_wif" {
  service_account_id = google_service_account.screener_deploy.name
  role               = "roles/iam.workloadIdentityUser"
  member             = local.depot_deploy_principal
}

# `gcloud compute ssh` checks actAs for the identity attached to the target.
# Pet and MIG workers share the provider-secret-free screener_worker identity.
resource "google_service_account_iam_member" "screener_deploy_actas_worker" {
  service_account_id = google_service_account.screener_worker.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.screener_deploy.email}"
}

resource "google_service_account_iam_member" "screener_deploy_actas_controller" {
  count              = local.screener_capacity_controller_count
  service_account_id = google_service_account.screener_capacity_controller[0].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.screener_deploy.email}"
}

output "screener_deploy_sa_email" {
  description = "Set as ditto-subnet's GCP_SCREENER_DEPLOY_SA prod-environment secret."
  value       = google_service_account.screener_deploy.email
}
