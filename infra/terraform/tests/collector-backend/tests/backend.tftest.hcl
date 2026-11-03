mock_provider "google" {
  mock_data "google_project" {
    defaults = { number = "22790208601" }
  }
}

# Computed SA fields are unknown in a plan until explicitly overridden. This
# fixture is a public fake identity; it does not create or impersonate a role.
override_resource {
  target          = google_service_account.bake[0]
  override_during = plan
  values = {
    name  = "projects/ditto-app-dev/serviceAccounts/sn118-preview-bake@ditto-app-dev.iam.gserviceaccount.com"
    email = "sn118-preview-bake@ditto-app-dev.iam.gserviceaccount.com"
  }
}

run "default_preserves_absent_bake_and_no_new_backend_grants" {
  command = plan
  assert {
    condition     = length(google_service_account.bake) == 0 && length(google_project_iam_member.bake_compute) == 0 && length(google_service_account_iam_member.bake_wif) == 0 && output.bake_service_account == null
    error_message = "All three staged bake prerequisites must remain absent by default."
  }
  assert {
    condition     = length(google_storage_bucket_iam_member.collector_custody_state_lock) == 0 && length(google_storage_bucket_iam_member.collector_custody_state_initial_create) == 0
    error_message = "No additive backend grants without reviewed public intent."
  }
}

run "bootstrap_only_two_exact_object_grants_and_existing_principal" {
  command = plan
  variables { enable_collector_custody_backend = true }
  assert {
    condition     = length(google_service_account.bake) == 0 && length(google_project_iam_member.bake_compute) == 0 && length(google_service_account_iam_member.bake_wif) == 0
    error_message = "Custody backend bootstrap cannot activate preview bake."
  }
  assert {
    condition     = google_storage_bucket_iam_member.collector_custody_state_lock[0].condition[0].expression == "resource.name == \"projects/_/buckets/ditto-app-dev-tfstate/objects/gcp-collector-custody/default.tflock\"" && google_storage_bucket_iam_member.collector_custody_state_lock[0].role == "roles/storage.objectAdmin"
    error_message = "Lock authority must cover only the exact custody lock object."
  }
  assert {
    condition     = google_storage_bucket_iam_member.collector_custody_state_initial_create[0].condition[0].expression == "resource.name == \"projects/_/buckets/ditto-app-dev-tfstate/objects/gcp-collector-custody/default.tfstate\"" && google_storage_bucket_iam_member.collector_custody_state_initial_create[0].role == "roles/storage.objectCreator"
    error_message = "Initial state create cannot overwrite or delete existing state."
  }
  assert {
    condition     = google_storage_bucket_iam_member.collector_custody_state_lock[0].member == "serviceAccount:github-actions-terraform-plan@ditto-app-dev.iam.gserviceaccount.com" && google_storage_bucket_iam_member.collector_custody_state_initial_create[0].member == google_storage_bucket_iam_member.collector_custody_state_lock[0].member
    error_message = "No new principal or broad bucket grant is authorized."
  }
}

run "wrong_project_backend_bootstrap_refused" {
  command = plan
  variables {
    project                          = "other-project"
    enable_collector_custody_backend = true
  }
  expect_failures = [google_storage_bucket_iam_member.collector_custody_state_lock, google_storage_bucket_iam_member.collector_custody_state_initial_create]
}

run "separate_bake_opt_in_covers_all_three_staged_prerequisites" {
  command = plan
  variables { enable_preview_bake = true }
  assert {
    condition     = length(google_service_account.bake) == 1 && length(google_project_iam_member.bake_compute) == 1 && length(google_service_account_iam_member.bake_wif) == 1 && output.bake_service_account != null
    error_message = "Explicit separately reviewed bake intent must be internally consistent."
  }
}
