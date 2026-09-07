terraform {
  required_version = ">= 1.10.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

variable "enabled" {
  description = "Provision one Platform-owned native-v2 qualification host. No worker or private execution is enabled."
  type        = bool
  default     = false
}

variable "project" {
  description = "Owning GCP project."
  type        = string
}

variable "region" {
  description = "Region for the isolated host network."
  type        = string
}

variable "zone" {
  description = "Zone for the qualification host."
  type        = string
}

variable "operators" {
  description = "Explicit reviewed Platform custodians. Host administrators can see future plaintext; do not inherit validator operators."
  type        = set(string)
  default     = []

  validation {
    condition     = !var.enabled || length(var.operators) > 0
    error_message = "An enabled native-v2 host requires an explicit Platform custodian set."
  }

  validation {
    condition     = alltrue([for member in var.operators : can(regex("^user:[A-Za-z0-9._%+\\-]+@[A-Za-z0-9.\\-]+\\.[A-Za-z]{2,}$", member))])
    error_message = "Host custodians must be explicit user:email identities, not public, group, domain, or service-account principals."
  }
}

variable "boot_disk_gb" {
  description = "Persistent boot disk for images and later bounded private state. Resizing/replacement requires a reviewed plan."
  type        = number
  default     = 200

  validation {
    condition     = var.boot_disk_gb >= 200 && var.boot_disk_gb <= 1000 && floor(var.boot_disk_gb) == var.boot_disk_gb
    error_message = "The host boot disk must be an integer between 200 and 1000 GB."
  }
}
