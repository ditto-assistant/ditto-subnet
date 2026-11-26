# Stand-ins for the four gcp-platform symbols cpm-scorer.tf references, so the
# real file can be planned alone against a mocked provider (run.sh). The whole
# root cannot be: its import blocks need a real provider.

variable "project" {
  type    = string
  default = "ditto-app-dev"
}

data "google_project" "this" {
  project_id = var.project
}

locals {
  # main.tf: google_service_account.platform_api.email
  platform_api_sa_email = "ditto-platform-api@ditto-app-dev.iam.gserviceaccount.com"
}

# screener-fleet-x509.tf owns the shared pool-admin grant; its count includes
# cpm_scorer_aws_account_id (ditto/tests/test_cpm_scorer_deploy.py).
resource "google_project_iam_member" "screener_fleet_x509_pool_admin" {
  count   = var.cpm_scorer_aws_account_id != "" ? 1 : 0
  project = var.project
  role    = "roles/iam.workloadIdentityPoolAdmin"
  member  = "serviceAccount:github-actions-terraform-apply@${var.project}.iam.gserviceaccount.com"
}
