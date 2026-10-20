# No secret payloads or production compute resources in this state.
data "google_project" "this" { project_id = "ditto-subnet" }
locals {
  project         = "ditto-subnet"
  platform_api_sa = "ditto-platform-api@ditto-app-dev.iam.gserviceaccount.com"
  pg_restore_sa   = "github-platform-pg-restore@ditto-subnet.iam.gserviceaccount.com"
  pg_backup_secrets = toset([
    "platform-pg-backup-hippius-access-key-id",
    "platform-pg-backup-hippius-secret-access-key",
    "platform-pg-backup-age-recipient",
    "platform-pg-backup-age-identity",
    "platform-pg-backup-reader-access-key-id",
    "platform-pg-backup-reader-secret-access-key",
  ])
  pg_restore_subject = "repo:ditto-assistant/ditto-subnet:environment:prod"
}

resource "google_secret_manager_secret" "pg_backup" {
  for_each  = local.pg_backup_secrets
  project   = local.project
  secret_id = each.value
  replication {
    auto {}
  }
  lifecycle { prevent_destroy = true }
}

# DB secrets are fetched by the authorized Ansible controller and copied as
# protected files. The DB VM has no attached service account; granting the
# shared default compute identity would not authorize it and would broaden IAM.

resource "google_secret_manager_secret_iam_member" "pg_backup_platform_reader" {
  for_each = toset([
    "platform-pg-backup-reader-access-key-id",
    "platform-pg-backup-reader-secret-access-key",
  ])
  project   = local.project
  secret_id = google_secret_manager_secret.pg_backup[each.value].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${local.platform_api_sa}"
}

resource "google_service_account" "pg_restore" {
  project      = local.project
  account_id   = "github-platform-pg-restore"
  display_name = "Isolated Platform PostgreSQL restore drill"
}

resource "google_iam_workload_identity_pool" "pg_restore" {
  project                   = local.project
  workload_identity_pool_id = "platform-pg-restore"
}

resource "google_iam_workload_identity_pool_provider" "pg_restore" {
  project                            = local.project
  workload_identity_pool_id          = google_iam_workload_identity_pool.pg_restore.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  attribute_mapping                  = { "google.subject" = "assertion.sub" }
  attribute_condition = join(" && ", [
    "assertion.repository_id == '1224630318'",
    "assertion.repository_owner_id == '148669063'",
    "assertion.sub == '${local.pg_restore_subject}'",
    "assertion.ref == 'refs/heads/main'",
    "assertion.event_name in ['schedule', 'workflow_dispatch']",
    "assertion.workflow_ref == 'ditto-assistant/ditto-subnet/.github/workflows/platform-pg-restore-drill.yml@refs/heads/main'",
  ])
  oidc { issuer_uri = "https://token.actions.githubusercontent.com" }
}

resource "google_service_account_iam_member" "pg_restore_wif" {
  service_account_id = "projects/ditto-subnet/serviceAccounts/${local.pg_restore_sa}"
  role               = "roles/iam.workloadIdentityUser"
  member             = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${google_iam_workload_identity_pool.pg_restore.workload_identity_pool_id}/subject/${local.pg_restore_subject}"
  depends_on         = [google_service_account.pg_restore]
}

resource "google_secret_manager_secret_iam_member" "pg_restore" {
  for_each = toset([
    "platform-pg-backup-reader-access-key-id",
    "platform-pg-backup-reader-secret-access-key",
    "platform-pg-backup-age-identity",
  ])
  project    = local.project
  secret_id  = google_secret_manager_secret.pg_backup[each.value].secret_id
  role       = "roles/secretmanager.secretAccessor"
  member     = "serviceAccount:${local.pg_restore_sa}"
  depends_on = [google_service_account.pg_restore]
}

output "pg_restore_service_account" { value = google_service_account.pg_restore.email }
output "pg_restore_workload_identity_provider" { value = google_iam_workload_identity_pool_provider.pg_restore.name }
