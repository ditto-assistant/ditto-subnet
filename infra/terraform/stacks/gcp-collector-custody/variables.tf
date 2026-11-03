# Explicit deployment boundary selected by Peyton; never infer another project
# or copy Platform/screener variables into this dedicated state root.
variable "project" {
  type = string
  validation {
    condition     = var.project == "ditto-app-dev"
    error_message = "Custody is approved only for ditto-app-dev."
  }
}

variable "region" {
  type = string
  validation {
    condition     = var.region == "us-central1"
    error_message = "Custody is approved only in us-central1."
  }
}

variable "zone" {
  type = string
  validation {
    condition     = var.zone == "us-central1-a"
    error_message = "Custody is approved only in us-central1-a."
  }
}

# Preserve the reviewed ceremony precondition; this root has no treasury host.
variable "enable_treasury_host" {
  type    = bool
  default = false
  validation {
    condition     = !var.enable_treasury_host
    error_message = "Legacy treasury resources cannot be enabled in custody."
  }
}
