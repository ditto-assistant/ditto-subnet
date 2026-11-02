# Only GCE snapshot metadata belongs to the live compute project.
variable "enable_platform_postgres_snapshot_reader" {
  description = "Permit Platform to list snapshots of its database disk."
  type        = bool
  default     = true
}
resource "google_project_iam_custom_role" "pg_backup_snapshot_reader" {
  count       = var.enable_platform_postgres_snapshot_reader ? 1 : 0
  project     = var.project
  role_id     = "platformPostgresSnapshotReader"
  title       = "Platform database snapshot metadata"
  permissions = ["compute.snapshots.list"]
}

resource "google_project_iam_member" "pg_backup_snapshot_reader" {
  count   = var.enable_platform_postgres_snapshot_reader ? 1 : 0
  project = var.project
  role    = google_project_iam_custom_role.pg_backup_snapshot_reader[0].id
  member  = "serviceAccount:${local.platform_api_sa_email}"
}
