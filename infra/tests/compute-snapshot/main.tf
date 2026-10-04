terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "= 6.50.0"
    }
  }
}
provider "google" { project = "snapshot-test" }

resource "google_compute_resource_policy" "daily" {
  name   = "daily"
  region = "us-central1"
  snapshot_schedule_policy {
    schedule {
      daily_schedule {
        days_in_cycle = 1
        start_time    = "06:00"
      }
    }
  }
}
variable "enable_snapshots" {
  type    = bool
  default = false
}
module "vm" {
  source                    = "../../terraform/modules/compute/gcp"
  name                      = "test-postgres"
  project                   = "snapshot-test"
  size                      = "db-staging"
  image                     = "debian-13"
  location                  = "us-central1-a"
  data_disk_gb              = 50
  boot_disk_snapshot_policy = var.enable_snapshots ? "projects/snapshot-test/regions/us-central1/resourcePolicies/${google_compute_resource_policy.daily.name}" : ""
  data_disk_snapshot_policy = var.enable_snapshots ? "projects/snapshot-test/regions/us-central1/resourcePolicies/${google_compute_resource_policy.daily.name}" : ""
}
