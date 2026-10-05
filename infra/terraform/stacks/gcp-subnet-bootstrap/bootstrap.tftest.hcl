mock_provider "google" {}
run "metadata_bootstrap_only" {
  command = plan
  assert {
    condition     = google_billing_project_info.subnet.project == "ditto-subnet" && google_billing_project_info.subnet.billing_account == "01279D-184F4C-3102C7" && length(google_project_service.recovery) == 6
    error_message = "Bootstrap is only for the nominated project, billing account and six recovery APIs."
  }
  assert {
    condition     = !contains(google_project_iam_custom_role.apply.permissions, "secretmanager.versions.access") && !contains(google_project_iam_custom_role.apply.permissions, "iam.serviceAccounts.getAccessToken") && !contains(google_project_iam_custom_role.apply.permissions, "resourcemanager.projects.setIamPolicy")
    error_message = "CI must not receive secret payload, token-minting or project IAM authority."
  }
  assert {
    condition     = google_project_iam_member.apply.member == "serviceAccount:subnet-recovery-tf-apply@ditto-subnet.iam.gserviceaccount.com" && google_storage_bucket.recovery_state.project == "ditto-subnet" && google_storage_bucket.recovery_state.public_access_prevention == "enforced" && google_storage_bucket.recovery_state.uniform_bucket_level_access && google_storage_bucket.recovery_state.versioning[0].enabled
    error_message = "Recovery CI and its private versioned state must remain in the isolated project."
  }
}
