provider "google" {
  region = "us-central1"
}

variable "enable_v13_private_bootstrap" {
  description = "Create only the dedicated project and isolated Terraform state bucket."
  type        = bool
  default     = false
}

variable "project_id" {
  type    = string
  default = "ditto-v13-private-verifier"
}

variable "organization_id" {
  type = string
}

variable "billing_account_id" {
  type      = string
  sensitive = true
}

variable "state_custodians" {
  description = "Exact operators allowed to read/write only the new project's Terraform state bucket. Supplied through a private plan input."
  type        = set(string)
  default     = []
}

resource "google_project" "verifier" {
  count               = var.enable_v13_private_bootstrap ? 1 : 0
  project_id          = var.project_id
  name                = "Ditto V13 private verifier"
  org_id              = var.organization_id
  billing_account     = var.billing_account_id
  auto_create_network = false
  lifecycle {
    prevent_destroy = true
  }
}

resource "google_project_service" "storage" {
  count              = var.enable_v13_private_bootstrap ? 1 : 0
  project            = google_project.verifier[0].project_id
  service            = "storage.googleapis.com"
  disable_on_destroy = false
}

resource "google_storage_bucket" "state" {
  count                       = var.enable_v13_private_bootstrap ? 1 : 0
  project                     = google_project.verifier[0].project_id
  name                        = "${var.project_id}-tfstate"
  location                    = "us-central1"
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  versioning {
    enabled = true
  }
  lifecycle {
    prevent_destroy = true
  }
  depends_on = [google_project_service.storage]
}

resource "google_storage_bucket_iam_member" "state_custodian" {
  for_each = var.enable_v13_private_bootstrap ? var.state_custodians : toset([])
  bucket   = google_storage_bucket.state[0].name
  role     = "roles/storage.objectAdmin"
  member   = each.value
}

output "state_bucket" {
  value = var.enable_v13_private_bootstrap ? google_storage_bucket.state[0].name : ""
}
