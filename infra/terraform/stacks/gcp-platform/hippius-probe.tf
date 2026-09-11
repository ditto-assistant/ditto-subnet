# The delegated probe has no project roles, Terraform state access, or actAs.
# A separate pool prevents principals from the broad CI pool impersonating it.
locals {
  hippius_probe_subject = "repo:ditto-assistant/ditto-subnet:environment:coding-hippius-probe"
  hippius_probe_secrets = {
    reader_access   = google_secret_manager_secret.coding_catalog_access_key.secret_id
    reader_secret   = google_secret_manager_secret.coding_catalog_secret_key.secret_id
    curator_access  = google_secret_manager_secret.coding_catalog_curator_access_key.secret_id
    curator_secret  = google_secret_manager_secret.coding_catalog_curator_secret_key.secret_id
    mediator_access = google_secret_manager_secret.coding_evidence_access_key.secret_id
    mediator_secret = google_secret_manager_secret.coding_evidence_secret_key.secret_id
  }
}

resource "google_service_account" "hippius_probe" {
  project      = var.project
  account_id   = "github-hippius-coding-probe"
  display_name = "Delegated synthetic Hippius Coding probe"
}

resource "google_iam_workload_identity_pool" "hippius_probe" {
  project                   = var.project
  workload_identity_pool_id = "hippius-coding-probe"
  display_name              = "Hippius Coding probe"
}

resource "google_iam_workload_identity_pool_provider" "hippius_probe" {
  project                            = var.project
  workload_identity_pool_id          = google_iam_workload_identity_pool.hippius_probe.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  attribute_mapping = {
    "google.subject" = "assertion.sub"
  }
  # Immutable repository/owner/actor IDs survive renames without accepting a
  # replacement account. Only this manual main workflow can use the identity.
  attribute_condition = join(" && ", [
    "assertion.repository_id == '1224630318'",
    "assertion.repository_owner_id == '148669063'",
    "assertion.sub == '${local.hippius_probe_subject}'",
    "assertion.ref == 'refs/heads/main'",
    "assertion.event_name == 'workflow_dispatch'",
    "assertion.workflow_ref == 'ditto-assistant/ditto-subnet/.github/workflows/coding-hippius-probe.yml@refs/heads/main'",
    "assertion.actor_id in ['6766068', '170978465']",
  ])
  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "hippius_probe_wif" {
  service_account_id = google_service_account.hippius_probe.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${google_iam_workload_identity_pool.hippius_probe.workload_identity_pool_id}/subject/${local.hippius_probe_subject}"
}

resource "google_project_iam_custom_role" "hippius_probe_secret_reader" {
  project     = var.project
  role_id     = "hippiusProbeSecretReader"
  title       = "Hippius probe secret version reader"
  description = "List enabled versions and consume payloads on six individually bound probe secrets."
  permissions = ["secretmanager.versions.list", "secretmanager.versions.access"]
}

resource "google_secret_manager_secret_iam_member" "hippius_probe" {
  for_each  = local.hippius_probe_secrets
  project   = var.project
  secret_id = each.value
  role      = google_project_iam_custom_role.hippius_probe_secret_reader.id
  member    = "serviceAccount:${google_service_account.hippius_probe.email}"
}

output "hippius_probe_service_account" {
  value = google_service_account.hippius_probe.email
}

output "hippius_probe_workload_identity_provider" {
  value = google_iam_workload_identity_pool_provider.hippius_probe.name
}
