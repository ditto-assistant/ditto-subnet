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

variable "enable_cpm_scorer" {
  description = "Create the CPM scorer's VPC, SSM transfer bucket and GPU instance. Off by default so merging this root creates nothing. aws-bootstrap must already have created the instance identity, and the account's on-demand G and VT vCPU quota must cover the size (g6e.xlarge is 4 vCPU)."
  type        = bool
  default     = false
}

variable "instance_profile_name" {
  description = "Instance profile created by aws-bootstrap (cpm-scorer-identity.tf). This root only reads it; it must wrap the ditto-cpm-scorer role."
  type        = string
  default     = "ditto-cpm-scorer"
}

variable "availability_zone" {
  description = "Zone for the scorer subnet and instance. g6e.xlarge is offered in us-east-1a, b, c and d; confirm the offering before moving it."
  type        = string
  default     = "us-east-1a"
}

variable "vpc_cidr" {
  description = "Dedicated CPM scorer VPC, distinct from the generator scorer's 10.118.0.0/24 and the screener's 10.118.1.0/24. One /24 holds one public subnet with no ingress."
  type        = string
  default     = "10.118.2.0/24"
}

variable "size" {
  description = "Logical compute size (modules/compute/aws/locals.tf). gpu-l40s is g6e.xlarge: one L40S 48 GB, 4 vCPU, 32 GiB. AWS-only."
  type        = string
  default     = "gpu-l40s"
}

variable "image" {
  description = "Logical image. ubuntu-24 resolves Canonical's AMI, which the CI apply role's image publisher allowlist admits; NVIDIA drivers are installed later by the Ansible role, not baked into an Amazon-published Deep Learning AMI."
  type        = string
  default     = "ubuntu-24"
}

variable "boot_disk_gb" {
  description = "Encrypted gp3 root volume. The NVIDIA driver and CUDA runtime, the scorer's Python environment, container images and the Hugging Face model cache live here."
  type        = number
  default     = 200

  validation {
    condition     = var.boot_disk_gb >= 200
    error_message = "boot_disk_gb must be at least 200: the model cache and GPU runtime do not fit in less."
  }
}

variable "deletion_protection" {
  description = "EC2 termination protection. Set false only in the reviewed plan that precedes a retirement."
  type        = bool
  default     = true
}

variable "ssm_agent_version" {
  description = "Exact amazon-ssm-agent release installed at first boot. It replaces the snap-packaged agent Canonical's AMI ships, which tracks the snap store instead of a pinned release."
  type        = string
  default     = "3.3.5538.0"
}

variable "ssm_agent_sha256" {
  description = "SHA-256 of debian_amd64/amazon-ssm-agent.deb for ssm_agent_version (identical in every regional bucket; the same package serves Ubuntu)."
  type        = string
  default     = "d0330f570309ad3468c75f5b12da95d394385680de820f1f2628506244a4ab6d"

  validation {
    condition     = can(regex("^[0-9a-f]{64}$", var.ssm_agent_sha256))
    error_message = "ssm_agent_sha256 must be a lowercase hex SHA-256."
  }
}
