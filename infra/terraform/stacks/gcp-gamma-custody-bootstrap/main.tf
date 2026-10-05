# One-time organization-owner bootstrap, never an app deployment identity.
# Project creation/billing was explicitly authorized by Peyton on 2026-10-05.
# This state owns only the new project/deployment boundary, never signer keys.
terraform {
  required_version = ">= 1.10.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "6.50.0"
    }
  }
}

provider "google" {
  project = "sn118-gamma-custody"
  region  = "us-central1"
}

resource "google_project" "custody" {
  project_id          = "sn118-gamma-custody"
  name                = "SN118 Gamma Custody"
  org_id              = "406520318131"
  billing_account     = "01279D-184F4C-3102C7"
  auto_create_network = false
  deletion_policy     = "PREVENT"
  labels              = { system = "sn118-gamma-custody", managed-by = "terraform" }
  lifecycle { prevent_destroy = true }
}

import {
  to = google_project.custody
  id = "sn118-gamma-custody"
}

resource "google_project_service" "api" {
  for_each = toset([
    "cloudresourcemanager.googleapis.com", "serviceusage.googleapis.com",
    "iam.googleapis.com", "iamcredentials.googleapis.com", "sts.googleapis.com",
    "secretmanager.googleapis.com", "iap.googleapis.com",
    "storage.googleapis.com", "logging.googleapis.com", "orgpolicy.googleapis.com",
  ])
  project            = google_project.custody.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_org_policy_policy" "default_accounts" {
  parent = "projects/1037372464879"
  name   = "projects/1037372464879/policies/iam.automaticIamGrantsForDefaultServiceAccounts"
  spec {
    rules { enforce = "TRUE" }
  }
  depends_on = [google_project_service.api]
}

resource "google_project_service" "compute" {
  project            = google_project.custody.project_id
  service            = "compute.googleapis.com"
  disable_on_destroy = false
  depends_on         = [google_org_policy_policy.default_accounts]
}

resource "google_service_account" "terraform" {
  for_each     = toset(["plan", "apply"])
  project      = google_project.custody.project_id
  account_id   = "gamma-custody-tf-${each.key}"
  display_name = "Protected Gamma custody Terraform ${each.key}"
  depends_on   = [google_project_service.api]
}

# Metadata only. Plan also reads IAP policy for existing isolated hosts;
# roles/iap.viewer does not include this refresh permission. No tunnel access.
# Neither deployment identity receives versions.access or add.
# The protected apply identity can manage secret IAM, so it is a custody admin
# boundary and must never authenticate from an unreviewed branch/workflow.
resource "google_project_iam_custom_role" "secret_metadata" {
  for_each = toset(["plan", "apply"])
  project  = google_project.custody.project_id
  role_id  = "gammaSecretMetadata${title(each.key)}"
  title    = "Gamma custody metadata ${each.key}"
  permissions = concat([
    "secretmanager.secrets.get", "secretmanager.secrets.list",
    "secretmanager.secrets.getIamPolicy", "secretmanager.versions.get",
    "secretmanager.versions.list",
    ], each.key == "apply" ? [
    "secretmanager.secrets.create", "secretmanager.secrets.update",
    "secretmanager.secrets.delete", "secretmanager.secrets.setIamPolicy",
  ] : ["iap.tunnelInstances.getIamPolicy"])
  depends_on = [google_project_service.api]
}

locals {
  deployment_roles = {
    plan  = ["roles/compute.viewer", "roles/iam.serviceAccountViewer", "roles/iam.roleViewer", "roles/iap.viewer", "roles/browser", "projects/sn118-gamma-custody/roles/gammaSecretMetadataPlan"]
    apply = ["roles/compute.admin", "roles/iam.serviceAccountAdmin", "roles/iam.serviceAccountUser", "roles/iam.roleAdmin", "roles/iap.admin", "roles/browser", "projects/sn118-gamma-custody/roles/gammaSecretMetadataApply"]
  }
  grants = merge([for purpose, roles in local.deployment_roles : { for role in roles : "${purpose}:${role}" => { purpose = purpose, role = role } }]...)
}

resource "google_project_iam_member" "deployment" {
  for_each   = local.grants
  project    = google_project.custody.project_id
  role       = each.value.role
  member     = "serviceAccount:${google_service_account.terraform[each.value.purpose].email}"
  depends_on = [google_project_iam_custom_role.secret_metadata, google_project_service.compute]
}

resource "google_iam_workload_identity_pool" "github" {
  project                   = google_project.custody.project_id
  workload_identity_pool_id = "gamma-custody-github"
  depends_on                = [google_project_service.api]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  project                            = google_project.custody.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "protected-infra"
  attribute_mapping                  = { "google.subject" = "assertion.sub" }
  attribute_condition                = "assertion.repository_id == '1224630318' && assertion.repository_owner_id == '148669063' && assertion.ref == 'refs/heads/main' && assertion.event_name == 'workflow_dispatch' && assertion.workflow_ref == 'ditto-assistant/ditto-subnet/.github/workflows/infra-plan-apply.yml@refs/heads/main' && assertion.actor_id == '6766068' && assertion.sub in ['repo:ditto-assistant/ditto-subnet:environment:infra-plan', 'repo:ditto-assistant/ditto-subnet:environment:infra-apply']"
  oidc { issuer_uri = "https://token.actions.githubusercontent.com" }
}

resource "google_service_account_iam_member" "github" {
  for_each           = google_service_account.terraform
  service_account_id = each.value.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principal://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/subject/repo:ditto-assistant/ditto-subnet:environment:infra-${each.key}"
}

resource "google_storage_bucket" "state" {
  project                     = google_project.custody.project_id
  name                        = "sn118-gamma-custody-tfstate"
  location                    = "US-CENTRAL1"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  versioning { enabled = true }
  lifecycle { prevent_destroy = true }
}

resource "google_storage_bucket_iam_member" "plan_read" {
  bucket = google_storage_bucket.state.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.terraform["plan"].email}"
}

resource "google_storage_bucket_iam_member" "plan_create" {
  bucket = google_storage_bucket.state.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.terraform["plan"].email}"
  condition {
    title      = "own-plan-and-initial-state-only"
    expression = "resource.name.startsWith('projects/_/buckets/sn118-gamma-custody-tfstate/objects/ci-plans/gcp-gamma-custody/') || resource.name == 'projects/_/buckets/sn118-gamma-custody-tfstate/objects/gcp-gamma-custody/default.tfstate'"
  }
}

resource "google_storage_bucket_iam_member" "plan_lock" {
  bucket = google_storage_bucket.state.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.terraform["plan"].email}"
  condition {
    title      = "own-state-lock-only"
    expression = "resource.name == 'projects/_/buckets/sn118-gamma-custody-tfstate/objects/gcp-gamma-custody/default.tflock'"
  }
}

resource "google_storage_bucket_iam_member" "apply_state" {
  bucket = google_storage_bucket.state.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.terraform["apply"].email}"
  condition {
    title      = "own-state-and-plan-only"
    expression = "resource.name.startsWith('projects/_/buckets/sn118-gamma-custody-tfstate/objects/gcp-gamma-custody/') || resource.name.startsWith('projects/_/buckets/sn118-gamma-custody-tfstate/objects/ci-plans/gcp-gamma-custody/')"
  }
}

# Terraform enumerates workspaces at bucket scope before reading an object.
# Object-prefix IAM cannot authorize that request. Permit list metadata only;
# get/create/update/delete stay on the existing exact custody prefixes.
resource "google_project_iam_custom_role" "state_list" {
  project     = google_project.custody.project_id
  role_id     = "gammaStateListMetadata"
  title       = "Gamma custody workspace metadata"
  permissions = ["storage.objects.list"]
}

resource "google_storage_bucket_iam_member" "apply_list" {
  bucket = google_storage_bucket.state.name
  role   = google_project_iam_custom_role.state_list.name
  member = "serviceAccount:${google_service_account.terraform["apply"].email}"
}

resource "google_project_iam_audit_config" "secret_reads" {
  project = google_project.custody.project_id
  service = "secretmanager.googleapis.com"
  audit_log_config { log_type = "DATA_READ" }
  audit_log_config { log_type = "DATA_WRITE" }
}

output "wif_provider" { value = google_iam_workload_identity_pool_provider.github.name }
output "deployment_accounts" { value = { for role, account in google_service_account.terraform : role => account.email } }
