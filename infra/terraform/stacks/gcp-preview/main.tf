data "google_project" "this" {
  project_id = var.project
}

locals {
  preview_subject = "repo:${var.repository}:environment:${var.preview_environment}"
  labels = {
    managed_by = "terraform"
    system     = "sn118-preview"
  }
}

resource "google_storage_bucket" "leases" {
  name                        = "${var.project}-sn118-preview-leases"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false
  labels                      = local.labels

  versioning { enabled = true }

  lifecycle_rule {
    condition { num_newer_versions = 2 }
    action { type = "Delete" }
  }
}

resource "google_storage_bucket" "snapshots" {
  name                        = "${var.project}-sn118-preview-snapshots"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false
  labels                      = local.labels

  versioning { enabled = true }

  lifecycle_rule {
    condition { age = 8 }
    action { type = "Delete" }
  }
}

resource "google_service_account" "controller" {
  account_id   = "github-sn118-preview"
  display_name = "GitHub SN118 preview controller"
}

resource "google_service_account" "runtime" {
  account_id   = "sn118-preview-runtime"
  display_name = "Credential-empty SN118 preview VM runtime"
}

# The trusted default-branch controller creates and deletes bounded preview VMs.
# It never checks out or executes PR code. The runtime identity deliberately has
# no project IAM roles and receives cloud-platform scope only so its lack of
# authorization is independently enforceable by IAM.
resource "google_project_iam_member" "controller_compute" {
  project = var.project
  role    = "roles/compute.instanceAdmin.v1"
  member  = "serviceAccount:${google_service_account.controller.email}"
}

resource "google_service_account_iam_member" "controller_act_as_runtime" {
  service_account_id = google_service_account.runtime.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.controller.email}"
}

resource "google_storage_bucket_iam_member" "controller_leases" {
  bucket = google_storage_bucket.leases.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.controller.email}"
}

resource "google_storage_bucket_iam_member" "controller_snapshots" {
  bucket = google_storage_bucket.snapshots.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.controller.email}"
}

resource "google_storage_bucket_iam_member" "snapshot_writer" {
  bucket = google_storage_bucket.snapshots.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${var.snapshot_writer_service_account}"
}

resource "google_service_account_iam_member" "controller_wif" {
  service_account_id = google_service_account.controller.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.wif_pool_id}/subject/${local.preview_subject}"
}

resource "google_service_account_iam_member" "controller_signs_urls" {
  service_account_id = google_service_account.controller.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.controller.email}"
}

resource "google_compute_network" "preview" {
  name                    = "sn118-preview"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "preview" {
  name          = "sn118-preview-${var.region}"
  region        = var.region
  network       = google_compute_network.preview.id
  ip_cidr_range = "10.42.0.0/24"
}

resource "google_compute_firewall" "web" {
  name    = "sn118-preview-web"
  network = google_compute_network.preview.name

  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
  }
  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["sn118-preview"]
}

resource "google_compute_firewall" "deny_internal" {
  name      = "sn118-preview-deny-internal"
  network   = google_compute_network.preview.name
  direction = "EGRESS"
  priority  = 100

  deny {
    protocol = "all"
  }
  destination_ranges = [
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
  ]
  target_tags = ["sn118-preview"]
}

resource "google_compute_firewall" "allow_internet" {
  name      = "sn118-preview-allow-internet"
  network   = google_compute_network.preview.name
  direction = "EGRESS"
  priority  = 1000

  allow {
    protocol = "all"
  }
  destination_ranges = ["0.0.0.0/0"]
  target_tags        = ["sn118-preview"]
}
