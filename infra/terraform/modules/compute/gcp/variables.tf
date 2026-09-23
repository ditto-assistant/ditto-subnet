# --- compute interface (shared across all providers) ----------------------
# See ../interface.md. These eight inputs are the portability contract; any
# provider implementation must accept them with the same names and types.

variable "name" {
  description = "Logical hostname; also used as the GCE instance name."
  type        = string
}

variable "size" {
  description = "Logical size (see ./locals.tf for native machine-type mapping)."
  type        = string
}

variable "image" {
  description = "Logical OS image (see ./locals.tf for native image mapping)."
  type        = string
}

variable "location" {
  description = "GCP zone for this zonal instance, e.g. us-central1-a. (The interface calls this 'location'; on GCP it is a zone, not a region.)"
  type        = string
}

variable "ssh_key_ids" {
  description = "Accepted for interface parity. UNUSED on GCP — SSH access is via OS Login (see var.enable_os_login), not injected keys."
  type        = list(string)
  default     = []
}

variable "firewall_id" {
  description = "Accepted for interface parity. UNUSED on GCP — firewalls are network-level rules targeted by network tags (see var.network_tags), not per-instance attachments."
  type        = string
  default     = ""
}

variable "user_data" {
  description = "cloud-init user-data, written to the `user-data` metadata key. Null if the host is configured by Ansible only."
  type        = string
  default     = null
}

variable "labels" {
  description = "GCE labels (key/value). Keys/values must be lowercase; no dots."
  type        = map(string)
  default     = {}
}

# --- GCP-specific extensions (all defaulted; do not break the contract) -----

variable "project" {
  description = "GCP project ID. Defaults to the provider's configured project when empty."
  type        = string
  default     = ""
}

variable "subnetwork" {
  description = "Self-link or name of the subnetwork to attach. Empty uses the project's auto-mode `default` network."
  type        = string
  default     = ""
}

variable "assign_public_ip" {
  description = "Attach an ephemeral external IP. Keep false for the Postgres VM — egress is via Cloud NAT, SSH via IAP."
  type        = bool
  default     = false
}

variable "network_tags" {
  description = "Network tags applied to the instance, used as firewall-rule targets."
  type        = list(string)
  default     = []
}

variable "boot_disk_gb" {
  description = "Boot disk size in GB."
  type        = number
  default     = 30
}

variable "boot_disk_type" {
  description = "Boot disk type (pd-standard | pd-balanced | pd-ssd)."
  type        = string
  default     = "pd-balanced"
}

variable "data_disk_gb" {
  description = "Persistent data disk size in GB for Postgres. 0 disables the data disk."
  type        = number
  default     = 0
}

variable "data_disk_type" {
  description = "Data disk type. pd-balanced gives good IOPS/$ for Postgres."
  type        = string
  default     = "pd-balanced"
}

variable "service_account_email" {
  description = "Email of the runtime service account for the VM. Empty omits the block (uses the default compute SA)."
  type        = string
  default     = ""
}

variable "service_account_scopes" {
  description = "OAuth scopes for the VM SA. cloud-platform + least-privilege IAM roles is the recommended pattern."
  type        = list(string)
  default     = ["https://www.googleapis.com/auth/cloud-platform"]
}

variable "enable_os_login" {
  description = "Enable OS Login (IAM-managed SSH). Strongly preferred over metadata SSH keys."
  type        = bool
  default     = true
}

variable "enable_secure_boot" {
  description = "Enable Shielded VM Secure Boot. Keep false for existing generic callers; security-sensitive new hosts should opt in explicitly."
  type        = bool
  default     = false
}

variable "enable_vtpm" {
  description = "Enable the Shielded VM virtual TPM."
  type        = bool
  default     = true
}

variable "enable_integrity_monitoring" {
  description = "Enable Shielded VM boot integrity monitoring."
  type        = bool
  default     = true
}
