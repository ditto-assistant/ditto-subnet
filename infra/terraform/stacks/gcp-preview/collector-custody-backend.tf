# Existing preview backend is already authorized for protected plans. It owns
# only the two additive custody-backend bootstrap grants, not custody resources.
# No state migration, out-of-band IAM bootstrap, bucket-wide scope or state
# overwrite/delete grant is used. Plan's existing Object Viewer is unchanged.
resource "google_storage_bucket_iam_member" "collector_custody_state_lock" {
  count  = var.enable_collector_custody_backend ? 1 : 0
  bucket = "ditto-app-dev-tfstate"
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:github-actions-terraform-plan@ditto-app-dev.iam.gserviceaccount.com"
  condition {
    title       = "terraform_plan_collector_custody_state_lock"
    description = "Create and release only the isolated custody Terraform lock"
    expression  = "resource.name == \"projects/_/buckets/ditto-app-dev-tfstate/objects/gcp-collector-custody/default.tflock\""
  }
  lifecycle {
    precondition {
      condition     = var.project == "ditto-app-dev"
      error_message = "Backend bootstrap is bound to the reviewed ditto-app-dev project."
    }
  }
}

resource "google_storage_bucket_iam_member" "collector_custody_state_initial_create" {
  count  = var.enable_collector_custody_backend ? 1 : 0
  bucket = "ditto-app-dev-tfstate"
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:github-actions-terraform-plan@ditto-app-dev.iam.gserviceaccount.com"
  condition {
    title       = "terraform_plan_collector_custody_state_initial_create"
    description = "Create only the initial isolated custody Terraform state"
    expression  = "resource.name == \"projects/_/buckets/ditto-app-dev-tfstate/objects/gcp-collector-custody/default.tfstate\""
  }
  lifecycle {
    precondition {
      condition     = var.project == "ditto-app-dev"
      error_message = "Backend bootstrap is bound to the reviewed ditto-app-dev project."
    }
  }
}
