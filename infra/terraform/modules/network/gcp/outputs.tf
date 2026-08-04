output "network_id" {
  description = "Self-link of the VPC. Pass to the cloudrun module for Direct VPC egress."
  value       = google_compute_network.this.id
}

output "network_name" {
  description = "Name of the VPC."
  value       = google_compute_network.this.name
}

output "subnetwork_id" {
  description = "Self-link of the subnet. Pass to compute/gcp (var.subnetwork) and cloudrun (egress subnet)."
  value       = google_compute_subnetwork.this.id
}

output "subnetwork_name" {
  description = "Name of the subnet."
  value       = google_compute_subnetwork.this.name
}

output "region" {
  description = "Region of the subnet/NAT."
  value       = var.region
}

output "postgres_target_tag" {
  description = "Network tag to attach to the Postgres VM (firewall target)."
  value       = var.postgres_target_tag
}

output "ssh_target_tag" {
  description = "Network tag to attach to instances that need IAP SSH."
  value       = var.ssh_target_tag
}
