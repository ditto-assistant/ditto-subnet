# Separate root: never change the already-live ditto-app-dev custody state.
variable "project" {
  type = string
  validation {
    condition     = var.project == "sn118-gamma-custody"
    error_message = "Isolated Gamma custody is approved only in sn118-gamma-custody."
  }
}
variable "region" {
  type = string
  validation {
    condition     = var.region == "us-central1"
    error_message = "Isolated custody is approved only in us-central1."
  }
}
variable "zone" {
  type = string
  validation {
    condition     = var.zone == "us-central1-a"
    error_message = "Isolated custody is approved only in us-central1-a."
  }
}
variable "enable_treasury_host" {
  type    = bool
  default = false
  validation {
    condition     = !var.enable_treasury_host
    error_message = "Legacy treasury host cannot be enabled in isolated custody."
  }
}
