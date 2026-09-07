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

module "coding_hosted_host" {
  source    = "../../modules/coding-hosted-host"
  enabled   = var.enable_coding_hosted_host
  project   = var.project
  region    = var.region
  zone      = var.zone
  operators = var.coding_hosted_operators

  depends_on = [google_project_service.iap]
}

output "coding_hosted_host" {
  description = "Native-v2 Platform qualification host, null until explicitly provisioned."
  value       = module.coding_hosted_host.host
}
