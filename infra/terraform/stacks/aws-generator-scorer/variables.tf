variable "aws_account_id" {
  description = "The only AWS account this root may touch (provider allowed_account_ids)."
  type        = string
  default     = "339713011188"
}

variable "region" {
  description = "AWS region. Must match the backend and the Ansible aws_ec2 inventory."
  type        = string
  default     = "us-east-1"
}

variable "enable_generator_scorer" {
  description = "Create the scorer's VPC, identity, SSM transfer bucket and instance. Off by default so merging this root creates nothing."
  type        = bool
  default     = false
}

variable "availability_zone" {
  description = "Zone for the scorer subnet and instance. us-east-1a (use1-az1 in this account) offers t3.medium; us-east-1e does not."
  type        = string
  default     = "us-east-1a"
}

variable "vpc_cidr" {
  description = "Dedicated scorer VPC. One /24 holds one public subnet with no ingress."
  type        = string
  default     = "10.118.0.0/24"
}

variable "size" {
  description = "Logical compute size (modules/compute/aws/locals.tf). app-small is t3.medium."
  type        = string
  default     = "app-small"
}

variable "image" {
  description = "Logical image, matching the GCE scorer's debian-13."
  type        = string
  default     = "debian-13"
}

variable "boot_disk_gb" {
  description = "Root volume size. Docker images, the Go build cache and the monorepo checkout live here."
  type        = number
  default     = 50
}

variable "cpu_credits" {
  description = "unlimited keeps a long sandboxed build from being throttled to baseline and timing out as if the generator were at fault. Surplus is billed per vCPU-hour."
  type        = string
  default     = "unlimited"
}

variable "deletion_protection" {
  description = "EC2 termination protection. Set false only in the reviewed plan that precedes a retirement."
  type        = bool
  default     = true
}

variable "ssm_agent_version" {
  description = "Exact amazon-ssm-agent release installed at first boot. Debian AMIs do not ship the agent."
  type        = string
  default     = "3.3.5538.0"
}

variable "ssm_agent_sha256" {
  description = "SHA-256 of debian_amd64/amazon-ssm-agent.deb for ssm_agent_version (identical in every regional bucket)."
  type        = string
  default     = "d0330f570309ad3468c75f5b12da95d394385680de820f1f2628506244a4ab6d"

  validation {
    condition     = can(regex("^[0-9a-f]{64}$", var.ssm_agent_sha256))
    error_message = "ssm_agent_sha256 must be a lowercase hex SHA-256."
  }
}
