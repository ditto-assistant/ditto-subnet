###############################################################################
# Capacity-controller deployment identity.
#
# GCE screener workers now pull authenticated releases and grant this principal
# no actAs permission. The existing state addresses and secret name remain for
# the capacity-controller's exact-release IAP deployment. It cannot resize the
# fleet, access Secret Manager directly, or write Artifact Registry.
###############################################################################

resource "google_service_account" "screener_deploy" {
  project      = var.project
  account_id   = "github-actions-screener-deploy"
  display_name = "GitHub Actions Screener Controller Deploy"
}

resource "google_project_iam_member" "screener_deploy_roles" {
  for_each = toset(["roles/compute.viewer"])
  project  = var.project
  role     = each.value
  member   = "serviceAccount:${google_service_account.screener_deploy.email}"
}

resource "google_compute_instance_iam_member" "screener_deploy_controller_osadmin" {
  count         = local.screener_capacity_controller_count
  project       = var.project
  zone          = var.zone
  instance_name = module.screener_capacity_controller_vm[0].hostname
  role          = "roles/compute.osAdminLogin"
  member        = "serviceAccount:${google_service_account.screener_deploy.email}"
}

resource "google_project_iam_custom_role" "terraform_screener_iap_policy_admin" {
  project     = var.project
  role_id     = "terraformScreenerIapPolicyAdmin"
  title       = "Terraform Screener IAP Policy Admin"
  description = "Manage the IAP policy on the screener capacity controller instance."
  permissions = [
    "iap.tunnelInstances.getIamPolicy",
    "iap.tunnelInstances.setIamPolicy",
  ]
}

resource "google_project_iam_member" "terraform_screener_iap_policy_admin" {
  project = var.project
  role    = google_project_iam_custom_role.terraform_screener_iap_policy_admin.id
  member  = "serviceAccount:github-actions-terraform-apply@${var.project}.iam.gserviceaccount.com"
}

resource "google_iap_tunnel_instance_iam_member" "screener_deploy_controller_iap" {
  count    = local.screener_capacity_controller_count
  project  = var.project
  zone     = var.zone
  instance = module.screener_capacity_controller_vm[0].hostname
  role     = "roles/iap.tunnelResourceAccessor"
  member   = "serviceAccount:${google_service_account.screener_deploy.email}"
  depends_on = [
    google_project_service.iap,
    google_project_iam_member.terraform_screener_iap_policy_admin,
  ]
}

resource "google_service_account_iam_member" "screener_deploy_wif" {
  service_account_id = google_service_account.screener_deploy.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.wif_pool_id}/subject/repo:ditto-assistant/ditto-subnet:environment:${var.platform_deploy_environment}"
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
