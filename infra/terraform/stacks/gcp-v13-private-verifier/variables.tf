variable "enable_v13_private_project" {
  description = "Create the isolated project and stage-only verifier infrastructure. No runtime, bank object, or secret version is installed."
  type        = bool
  default     = false
}

variable "project_id" {
  description = "Proposed globally unique project ID; verify availability immediately before an approved apply."
  type        = string
  default     = "ditto-v13-private-verifier"
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "zone" {
  type    = string
  default = "us-central1-a"
}

variable "subnet_cidr" {
  type    = string
  default = "10.32.0.0/24"
}

variable "boot_disk_gb" {
  type    = number
  default = 100
  validation {
    condition     = var.boot_disk_gb >= 100
    error_message = "The staged verifier host needs at least 100 GiB."
  }
}

variable "operators" {
  description = "Explicit host custodians. Empty until separately reviewed."
  type        = set(string)
  default     = []
}

variable "platform_api_service_account" {
  description = "Exact existing Platform API runtime SA allowed to read only the ticket and provider key containers."
  type        = string
  default     = "ditto-platform-api@ditto-app-dev.iam.gserviceaccount.com"
}
