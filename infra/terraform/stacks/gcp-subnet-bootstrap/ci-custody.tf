# Recovery CI, federation and state must not inherit backend impersonation or
# mutable plan artifacts from ditto-app-dev. These remain owner-bootstrapped.
locals {
  terraform_sa_emails = {
    plan  = "subnet-recovery-tf-plan@ditto-subnet.iam.gserviceaccount.com"
    apply = "subnet-recovery-tf-apply@ditto-subnet.iam.gserviceaccount.com"
  }
  terraform_subjects = {
    plan  = "repo:ditto-assistant/ditto-subnet:environment:infra-plan"
    apply = "repo:ditto-assistant/ditto-subnet:environment:infra-apply"
  }
  terraform_pool_path = "projects/286408627661/locations/global/workloadIdentityPools/subnet-recovery-infra"
  recovery_bucket     = "ditto-subnet-recovery-tfstate"
}

resource "google_service_account" "terraform" {
  for_each     = local.terraform_sa_emails
  project      = local.project
  account_id   = "subnet-recovery-tf-${each.key}"
  display_name = "Isolated subnet recovery Terraform ${each.key}"
  depends_on   = [google_project_service.recovery]
  lifecycle { prevent_destroy = true }
}

resource "google_iam_workload_identity_pool" "terraform" {
  project                   = local.project
  workload_identity_pool_id = "subnet-recovery-infra"
  depends_on                = [google_project_service.recovery]
}

resource "google_iam_workload_identity_pool_provider" "terraform" {
  project                            = local.project
  workload_identity_pool_id          = google_iam_workload_identity_pool.terraform.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  attribute_mapping                  = { "google.subject" = "assertion.sub" }
  attribute_condition = join(" && ", [
    "assertion.repository_id == '1224630318'",
    "assertion.repository_owner_id == '148669063'",
    "assertion.ref == 'refs/heads/main'",
    "assertion.event_name == 'workflow_dispatch'",
    "assertion.workflow_ref == 'ditto-assistant/ditto-subnet/.github/workflows/infra-plan-apply.yml@refs/heads/main'",
    "assertion.sub in ['${local.terraform_subjects.plan}', '${local.terraform_subjects.apply}']",
  ])
  oidc { issuer_uri = "https://token.actions.githubusercontent.com" }
}

resource "google_service_account_iam_member" "terraform_wif" {
  for_each           = local.terraform_sa_emails
  service_account_id = "projects/${local.project}/serviceAccounts/${each.value}"
  role               = "roles/iam.workloadIdentityUser"
  member             = "principal://iam.googleapis.com/${local.terraform_pool_path}/subject/${local.terraform_subjects[each.key]}"
  depends_on         = [google_service_account.terraform, google_iam_workload_identity_pool_provider.terraform]
}

resource "google_storage_bucket" "recovery_state" {
  project                     = local.project
  name                        = local.recovery_bucket
  location                    = "US"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  versioning { enabled = true }
  depends_on = [google_project_service.recovery]
  lifecycle { prevent_destroy = true }
}

# Only recovery state and reviewed recovery plan artifacts live in this bucket.
# Neither account receives bucket IAM administration or access to other roots.
resource "google_storage_bucket_iam_member" "terraform_state" {
  for_each   = local.terraform_sa_emails
  bucket     = google_storage_bucket.recovery_state.name
  role       = "roles/storage.objectAdmin"
  member     = "serviceAccount:${each.value}"
  depends_on = [google_service_account.terraform]
}

output "recovery_ci_provider" { value = google_iam_workload_identity_pool_provider.terraform.name }
output "recovery_ci_accounts" { value = local.terraform_sa_emails }
output "recovery_state_bucket" { value = google_storage_bucket.recovery_state.name }
