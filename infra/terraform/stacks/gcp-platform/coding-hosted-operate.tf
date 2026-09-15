# Protected main-only coding-host workflow identity. Root-capable on the native
# qualification host only, through the coding-hosted-host module's
# destination-scoped workflow grant. Its only project-level permission is the
# module's compute.projects.get custom role, which gcloud compute ssh requires.
# It has no Secret Manager access, Terraform state access, or actAs beyond the
# host service account. A separate pool prevents principals from the broad CI
# pool impersonating it.
locals {
  coding_hosted_operate_subject = "repo:ditto-assistant/ditto-subnet:environment:coding-hosted-operate"
}

variable "enable_coding_hosted_operate_workflow" {
  description = "Grant the protected coding-host workflow identity root-capable IAP SSH to the native host only."
  type        = bool
  default     = false

  validation {
    condition     = !var.enable_coding_hosted_operate_workflow || var.enable_coding_hosted_host
    error_message = "The coding-host workflow grant requires the native Coding host to be explicitly enabled."
  }
}

resource "google_service_account" "coding_hosted_operate" {
  project      = var.project
  account_id   = "github-coding-hosted-operate"
  display_name = "Protected main-only native coding host operations"
}

resource "google_iam_workload_identity_pool" "coding_hosted_operate" {
  project                   = var.project
  workload_identity_pool_id = "coding-hosted-operate"
  display_name              = "Coding host operations"
}

resource "google_iam_workload_identity_pool_provider" "coding_hosted_operate" {
  project                            = var.project
  workload_identity_pool_id          = google_iam_workload_identity_pool.coding_hosted_operate.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  attribute_mapping = {
    "google.subject" = "assertion.sub"
  }
  # Immutable repository/owner/actor IDs survive renames without accepting a
  # replacement account. Only this manual main workflow can use the identity.
  attribute_condition = join(" && ", [
    "assertion.repository_id == '1224630318'",
    "assertion.repository_owner_id == '148669063'",
    "assertion.sub == '${local.coding_hosted_operate_subject}'",
    "assertion.ref == 'refs/heads/main'",
    "assertion.event_name == 'workflow_dispatch'",
    "assertion.workflow_ref == 'ditto-assistant/ditto-subnet/.github/workflows/coding-hosted-operate.yml@refs/heads/main'",
    "assertion.actor_id in ['6766068', '170978465']",
  ])
  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "coding_hosted_operate_wif" {
  service_account_id = google_service_account.coding_hosted_operate.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${google_iam_workload_identity_pool.coding_hosted_operate.workload_identity_pool_id}/subject/${local.coding_hosted_operate_subject}"
}

output "coding_hosted_operate_service_account" {
  value = google_service_account.coding_hosted_operate.email
}

output "coding_hosted_operate_workload_identity_provider" {
  value = google_iam_workload_identity_pool_provider.coding_hosted_operate.name
}
