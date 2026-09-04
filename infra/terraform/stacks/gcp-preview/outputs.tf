output "controller_service_account" {
  value = google_service_account.controller.email
}

output "runtime_service_account" {
  value = google_service_account.runtime.email
}

output "lease_bucket" {
  value = google_storage_bucket.leases.name
}

output "snapshot_bucket" {
  value = google_storage_bucket.snapshots.name
}

output "network" {
  value = google_compute_network.preview.name
}

output "subnetwork" {
  value = google_compute_subnetwork.preview.name
}

output "lease_ttl_hours" {
  value = var.lease_ttl_hours
}
