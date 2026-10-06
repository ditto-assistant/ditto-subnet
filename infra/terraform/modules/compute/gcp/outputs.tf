output "id" {
  description = "GCE instance ID (numeric, as a string) for import + outputs."
  value       = tostring(google_compute_instance.this.instance_id)
}

output "ipv4" {
  description = "Public IPv4 address. Empty when assign_public_ip = false (the default for the PG VM)."
  value       = try(google_compute_instance.this.network_interface[0].access_config[0].nat_ip, "")
}

output "ipv6" {
  description = "Public IPv6 address. Always empty for now (not provisioned)."
  value       = ""
}

output "internal_ip" {
  description = "Private (VPC) IP — the address Cloud Run reaches Postgres on via Direct VPC egress."
  value       = google_compute_instance.this.network_interface[0].network_ip
}

output "hostname" {
  description = "Instance name as configured."
  value       = google_compute_instance.this.name
}
output "snapshot_policy_disks" {
  description = "Disks with an optional snapshot attachment; empty for unchanged callers."
  value = concat(
    [for attachment in google_compute_disk_resource_policy_attachment.boot_snapshot : attachment.disk],
    [for attachment in google_compute_disk_resource_policy_attachment.data_snapshot : attachment.disk],
  )
}
