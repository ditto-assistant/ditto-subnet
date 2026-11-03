mock_provider "google" {
  mock_data "google_project" {
    defaults = { number = "286408627661" }
  }
}
run "isolated_recovery_scope" {
  command = plan
  assert {
    condition     = length(google_secret_manager_secret.pg_backup) == 6 && length(google_secret_manager_secret_iam_member.pg_backup_platform_reader) == 2 && length(google_secret_manager_secret_iam_member.pg_restore) == 3
    error_message = "Recovery must contain six empty secrets, two Platform reader grants and three restore grants."
  }
  assert {
    condition     = google_secret_manager_secret.pg_backup["platform-pg-backup-age-identity"].project == "ditto-subnet" && google_secret_manager_secret_iam_member.pg_restore["platform-pg-backup-age-identity"].member == "serviceAccount:github-platform-pg-restore@ditto-subnet.iam.gserviceaccount.com"
    error_message = "The private key may only be read by the isolated restore identity."
  }
  assert {
    condition     = google_service_account.pg_restore.project == "ditto-subnet" && google_service_account_iam_member.pg_restore_wif.member == "principal://iam.googleapis.com/projects/286408627661/locations/global/workloadIdentityPools/platform-pg-restore/subject/repo:ditto-assistant/ditto-subnet:environment:prod"
    error_message = "Restore federation must originate in the dedicated project and prod environment."
  }
}
