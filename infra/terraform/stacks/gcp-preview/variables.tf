variable "project" {
  description = "GCP project that owns SN118 preview infrastructure."
  type        = string
  default     = "ditto-app-dev"
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "zone" {
  type    = string
  default = "us-central1-a"
}

variable "repository" {
  type    = string
  default = "ditto-assistant/ditto-subnet"
}

variable "wif_pool_id" {
  type    = string
  default = "github"
}

variable "preview_environment" {
  description = "Protected GitHub environment used only by the trusted preview controller."
  type        = string
  default     = "preview-stack"
}

variable "snapshot_writer_service_account" {
  description = "Existing main-only deploy identity used by the scheduled sanitizer."
  type        = string
  default     = "github-actions-platform-deploy@ditto-app-dev.iam.gserviceaccount.com"
}

variable "lease_ttl_hours" {
  type    = number
  default = 24

  validation {
    condition     = var.lease_ttl_hours >= 1 && var.lease_ttl_hours <= 48
    error_message = "lease_ttl_hours must be between 1 and 48."
  }
}
