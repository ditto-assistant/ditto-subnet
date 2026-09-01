# These outputs feed the platform_app Ansible role (PG IP, bucket names, HMAC
# access id) and the ditto-platform deploy workflow (VM names/zone via labels).

output "pg_internal_ip" {
  description = "Private IP of the platform Postgres VM — what the app VMs set as POSTGRES_HOST."
  value       = module.pg_vm.internal_ip
}

output "pg_instance" {
  description = "Platform Postgres VM instance name."
  value       = module.pg_vm.hostname
}

output "app_vm_names" {
  description = "App VM instance names keyed by env (the deploy workflow IAP-SSHes into these)."
  value       = { for k, m in module.app : k => m.hostname }
}

output "app_vm_ips" {
  description = "App VM public IPv4 addresses keyed by env."
  value       = { for k, m in module.app : k => m.ipv4 }
}

output "agent_bucket_names" {
  description = "Agent-tarball GCS bucket names keyed by env (STORAGE_BUCKET)."
  value       = { for k, b in google_storage_bucket.agents : k => b.name }
}

output "storage_hmac_access_id" {
  description = "GCS HMAC access id (non-secret) — the app's STORAGE_ACCESS_KEY. The matching secret is in Secret Manager (platform-storage-hmac-secret)."
  value       = google_storage_hmac_key.platform.access_id
}

output "coding_private_input_bucket_names" {
  description = "Private coding input S3-compatible bucket names by environment; empty while the authority is disabled."
  value       = { for k, bucket in google_storage_bucket.coding_private_inputs : k => bucket.name }
}

output "coding_sealed_evidence_bucket_names" {
  description = "Sealed coding evidence S3-compatible bucket names by environment; empty while the authority is disabled."
  value       = { for k, bucket in google_storage_bucket.coding_sealed_evidence : k => bucket.name }
}

output "coding_private_input_curator_hmac_access_ids" {
  description = "Non-secret HMAC access IDs for create-only private-input curators by environment."
  value       = { for k, key in google_storage_hmac_key.coding_private_input_curator : k => key.access_id }
}

output "coding_private_input_reader_hmac_access_ids" {
  description = "Non-secret HMAC access IDs for get-only private-input readers by environment."
  value       = { for k, key in google_storage_hmac_key.coding_private_input_reader : k => key.access_id }
}

output "coding_evidence_finalizer_hmac_access_ids" {
  description = "Non-secret HMAC access IDs for create-and-verify evidence finalizers by environment."
  value       = { for k, key in google_storage_hmac_key.coding_evidence_finalizer : k => key.access_id }
}

output "api_domain_dev" {
  description = "Dev API hostname."
  value       = var.api_domain_dev
}

output "api_domain_prod" {
  description = "Prod API hostname."
  value       = var.api_domain_prod
}

output "network_name" {
  description = "Platform VPC name."
  value       = module.network.network_name
}

# --- Identity (feeds the ditto-subnet repo's deploy.yml GitHub secrets) ---

output "ditto_platform_sa_email" {
  description = "Runtime SA the app VMs run as."
  value       = google_service_account.ditto_platform.email
}

output "platform_api_sa_email" {
  description = "Dedicated runtime SA for the platform API and inference proxy."
  value       = google_service_account.platform_api.email
}

output "platform_deploy_sa_email" {
  description = "github-actions-platform-deploy SA email — set as the ditto-subnet repo secret GCP_PLATFORM_DEPLOY_SA."
  value       = google_service_account.platform_deploy.email
}

output "wif_provider" {
  description = "Full `github` WIF provider resource name — set as the ditto-subnet repo secret GCP_WIF_PROVIDER."
  value       = "projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.wif_pool_id}/providers/${var.wif_provider_id}"
}
output "datagen_release_sa_email" {
  description = "Set as ditto-subnet's GCP_DATAGEN_RELEASE_SA prod-environment secret."
  value       = google_service_account.datagen_release.email
}
