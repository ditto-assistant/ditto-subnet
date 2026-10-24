# --- compute interface (shared across all providers) ----------------------
# See ../interface.md. These eight inputs are the portability contract; every
# provider implementation accepts them with the same names and types.

variable "name" {
  description = "Logical hostname; set as the EC2 Name tag and used by the Ansible inventory as inventory_hostname."
  type        = string
}

variable "size" {
  description = "Logical size (see ./locals.tf for the native instance-type mapping)."
  type        = string
}

variable "image" {
  description = "Logical OS image (see ./locals.tf), or an exact ami- ID to pin one build."
  type        = string
}

variable "location" {
  description = "Availability zone, e.g. us-east-1a. It must match the subnet's zone."
  type        = string
}

variable "ssh_key_ids" {
  description = "Accepted for interface parity. UNUSED on AWS: hosts are reached through SSM Session Manager, never an EC2 key pair."
  type        = list(string)
  default     = []
}

variable "firewall_id" {
  description = "Security group ID attached to the instance. Empty attaches the VPC default group, which no caller should rely on."
  type        = string
  default     = ""
}

variable "user_data" {
  description = "cloud-init user data. Applied at first boot only; later edits are ignored rather than stopping the instance."
  type        = string
  default     = null
}

variable "labels" {
  description = "Tags (key/value). Keep keys/values lowercase with no dots so one map is valid as GCE labels too."
  type        = map(string)
  default     = {}
}

# --- shared extensions (same names as modules/compute/gcp) ------------------

variable "subnetwork" {
  description = "Subnet ID to attach (AWS has no implicit default here; pass one explicitly)."
  type        = string
}

variable "assign_public_ip" {
  description = "Attach a public IPv4 for egress. Ingress is still governed only by firewall_id."
  type        = bool
  default     = false
}

variable "boot_disk_gb" {
  description = "Root volume size in GiB."
  type        = number
  default     = 30
}

variable "boot_disk_type" {
  description = "Root volume type."
  type        = string
  default     = "gp3"
}

variable "deletion_protection" {
  description = "EC2 API termination protection. Disable only in a separately reviewed retirement plan."
  type        = bool
  default     = true
}

# --- AWS-specific extensions (all defaulted) --------------------------------

variable "instance_profile_name" {
  description = "IAM instance profile (the host identity). Empty attaches none."
  type        = string
  default     = ""
}

variable "boot_disk_kms_key_id" {
  description = "KMS key ARN for the root volume. Null uses the account's aws/ebs key; the volume is encrypted either way."
  type        = string
  default     = null
}

variable "cpu_credits" {
  description = "Burstable (t*) credit mode: standard throttles at baseline when credits run out; unlimited bills surplus instead. Ignored for other families."
  type        = string
  default     = "standard"

  validation {
    condition     = contains(["standard", "unlimited"], var.cpu_credits)
    error_message = "cpu_credits must be standard or unlimited."
  }
}

variable "metadata_hop_limit" {
  description = "IMDSv2 PUT response hop limit. 1 keeps instance credentials out of reach of containers on bridge networks."
  type        = number
  default     = 1

  validation {
    condition     = var.metadata_hop_limit >= 1 && var.metadata_hop_limit <= 64
    error_message = "metadata_hop_limit must be between 1 and 64."
  }
}
