output "name" {
  description = "Cloud Run service name."
  value       = google_cloud_run_v2_service.this.name
}

output "uri" {
  description = "Default run.app URL of the service."
  value       = google_cloud_run_v2_service.this.uri
}

output "domain" {
  description = "Mapped custom domain, or empty when no mapping was created."
  value       = var.domain
}

output "domain_dns_records" {
  description = "DNS records the domain mapping wants (feed into Cloudflare). Empty when no domain mapping."
  value       = try(google_cloud_run_domain_mapping.this[0].status[0].resource_records, [])
}
