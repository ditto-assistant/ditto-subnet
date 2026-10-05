mock_provider "google" {}
run "metadata_bootstrap_only" {
  command = plan
  assert {
    condition     = google_billing_project_info.subnet.project == "ditto-subnet" && google_billing_project_info.subnet.billing_account == "01279D-184F4C-3102C7" && length(google_project_service.recovery) == 5
    error_message = "Bootstrap is only for the nominated project, billing account and five recovery APIs."
  }
  assert {
    condition     = !contains(google_project_iam_custom_role.apply.permissions, "secretmanager.versions.access") && !contains(google_project_iam_custom_role.apply.permissions, "iam.serviceAccounts.getAccessToken") && !contains(google_project_iam_custom_role.apply.permissions, "resourcemanager.projects.setIamPolicy")
    error_message = "CI must not receive secret payload, token-minting or project IAM authority."
  }
}
