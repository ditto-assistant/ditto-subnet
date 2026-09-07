# Independent of coding_executor_host_count and its legacy validator/scorer
# trust model. Enabling this foundation does not authorize private execution.
variable "enable_coding_hosted_host" {
  description = "Create one Platform-owned native-v2 qualification host through protected plan/apply."
  type        = bool
  default     = false
}

variable "coding_hosted_operators" {
  description = "Explicit reviewed Platform host custodians; required before enabling the native-v2 host."
  type        = set(string)
  default     = []
}

variable "enable_coding_hosted_postgres" {
  description = "Add the separately reviewed private native-host TCP 5432 path; no worker, credentials or guest database admission is enabled."
  type        = bool
  default     = false

  validation {
    condition     = !var.enable_coding_hosted_postgres || var.enable_coding_hosted_host
    error_message = "Private PostgreSQL access requires the native Coding host to be explicitly enabled."
  }
}

module "coding_hosted_host" {
  source    = "../../modules/coding-hosted-host"
  enabled   = var.enable_coding_hosted_host
  project   = var.project
  region    = var.region
  zone      = var.zone
  operators = var.coding_hosted_operators
  postgres_peer = var.enable_coding_hosted_postgres ? {
    network_self_link = module.network.network_self_link
    private_ip        = module.pg_vm.internal_ip
    target_tag        = module.network.postgres_target_tag
  } : null

  depends_on = [google_project_service.iap]
}

output "coding_hosted_host" {
  description = "Native-v2 Platform qualification host, null until explicitly provisioned."
  value       = module.coding_hosted_host.host
}

output "coding_hosted_postgres_access" {
  description = "Private native PostgreSQL path, null until explicitly approved; not database-login readiness."
  value       = module.coding_hosted_host.postgres_access
}
