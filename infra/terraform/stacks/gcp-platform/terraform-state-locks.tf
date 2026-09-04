# The read-only Terraform plan identity may create and release only the lock
# object for the preview root. It cannot write the preview state object. This
# grant is owned by the already-live gcp-platform root so the new gcp-preview
# root can initialize without an out-of-band IAM bootstrap.
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
