# Secret payloads never enter Terraform. Bootstrap identity separately from the
# two-resource targeted snapshot plan, through protected plan/apply.
variable "enable_platform_postgres_backup_identity" {
  description = "Create empty backup secrets and narrowly scoped runtime/restore identity. No backup activation or secret payload."
  type        = bool
  default     = true
}

locals {
  pg_backup_secrets = var.enable_platform_postgres_backup_identity ? toset([
    "platform-pg-backup-hippius-access-key-id",
    "platform-pg-backup-hippius-secret-access-key",
    "platform-pg-backup-age-recipient",
    "platform-pg-backup-age-identity",
    "platform-pg-backup-reader-access-key-id",
    "platform-pg-backup-reader-secret-access-key",
  ]) : toset([])
  pg_backup_vm_sa    = var.vm_service_account_email != "" ? var.vm_service_account_email : "${data.google_project.this.number}-compute@developer.gserviceaccount.com"
  pg_restore_subject = "repo:ditto-assistant/ditto-subnet:environment:prod"
}

resource "google_secret_manager_secret" "pg_backup" {
  for_each  = local.pg_backup_secrets
  project   = var.project
  secret_id = each.value
  replication {
    auto {}
  }
  lifecycle { prevent_destroy = true }
}

resource "google_secret_manager_secret_iam_member" "pg_backup_vm" {
  for_each = var.enable_platform_postgres_backup_identity ? toset([
    "platform-pg-backup-hippius-access-key-id",
    "platform-pg-backup-hippius-secret-access-key",
    "platform-pg-backup-age-recipient",
  ]) : toset([])
  project   = var.project
  secret_id = google_secret_manager_secret.pg_backup[each.value].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${local.pg_backup_vm_sa}"
}

resource "google_secret_manager_secret_iam_member" "pg_backup_platform_reader" {
  for_each = var.enable_platform_postgres_backup_identity ? toset([
    "platform-pg-backup-reader-access-key-id",
    "platform-pg-backup-reader-secret-access-key",
  ]) : toset([])
  project   = var.project
  secret_id = google_secret_manager_secret.pg_backup[each.value].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${local.platform_api_sa_email}"
}

resource "google_service_account" "pg_restore" {
  count        = var.enable_platform_postgres_backup_identity ? 1 : 0
  project      = var.project
  account_id   = "github-platform-pg-restore"
  display_name = "Isolated Platform PostgreSQL restore drill"
}

resource "google_iam_workload_identity_pool" "pg_restore" {
  count                     = var.enable_platform_postgres_backup_identity ? 1 : 0
  project                   = var.project
  workload_identity_pool_id = "platform-pg-restore"
}

resource "google_iam_workload_identity_pool_provider" "pg_restore" {
  count                              = var.enable_platform_postgres_backup_identity ? 1 : 0
  project                            = var.project
  workload_identity_pool_id          = google_iam_workload_identity_pool.pg_restore[0].workload_identity_pool_id
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
  count              = var.enable_platform_postgres_backup_identity ? 1 : 0
  service_account_id = google_service_account.pg_restore[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${google_iam_workload_identity_pool.pg_restore[0].workload_identity_pool_id}/subject/${local.pg_restore_subject}"
}

resource "google_secret_manager_secret_iam_member" "pg_restore" {
  for_each = var.enable_platform_postgres_backup_identity ? toset([
    "platform-pg-backup-reader-access-key-id",
    "platform-pg-backup-reader-secret-access-key",
    "platform-pg-backup-age-identity",
  ]) : toset([])
  project   = var.project
  secret_id = google_secret_manager_secret.pg_backup[each.value].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.pg_restore[0].email}"
}

resource "google_project_iam_custom_role" "pg_backup_snapshot_reader" {
  count       = var.enable_platform_postgres_backup_identity ? 1 : 0
  project     = var.project
  role_id     = "platformPostgresSnapshotReader"
  title       = "Platform database snapshot metadata"
  permissions = ["compute.snapshots.list"]
}

resource "google_project_iam_member" "pg_backup_snapshot_reader" {
  count   = var.enable_platform_postgres_backup_identity ? 1 : 0
  project = var.project
  role    = google_project_iam_custom_role.pg_backup_snapshot_reader[0].id
  member  = "serviceAccount:${local.platform_api_sa_email}"
}

output "pg_restore_service_account" {
  value = try(google_service_account.pg_restore[0].email, "")
}

output "pg_restore_workload_identity_provider" {
  value = try(google_iam_workload_identity_pool_provider.pg_restore[0].name, "")
}
