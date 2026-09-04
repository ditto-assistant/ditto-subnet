# The read-only Terraform plan identity may create and release only the lock
# object for the preview root. This grant is owned by the already-live
# gcp-platform root so the new gcp-preview root can initialize without an
# out-of-band IAM bootstrap.
resource "google_storage_bucket_iam_member" "terraform_plan_preview_state_lock" {
  bucket = "ditto-app-dev-tfstate"
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:github-actions-terraform-plan@${var.project}.iam.gserviceaccount.com"

  condition {
    title       = "terraform_plan_gcp_preview_state_lock"
    description = "Create and release only the gcp-preview Terraform state lock"
    expression  = "resource.name == \"projects/_/buckets/ditto-app-dev-tfstate/objects/gcp-preview/default.tflock\""
  }
}

# A new GCS backend writes an empty state object during its first init. Object
# Creator permits that initial write but cannot overwrite or delete the object;
# subsequent plans use the plan identity's bucket-level Object Viewer grant.
resource "google_storage_bucket_iam_member" "terraform_plan_preview_state_initial_create" {
  bucket = "ditto-app-dev-tfstate"
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:github-actions-terraform-plan@${var.project}.iam.gserviceaccount.com"

  condition {
    title       = "terraform_plan_gcp_preview_state_initial_create"
    description = "Create only the initial gcp-preview Terraform state object"
    expression  = "resource.name == \"projects/_/buckets/ditto-app-dev-tfstate/objects/gcp-preview/default.tfstate\""
  }
}
