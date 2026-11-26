variable "aws_account_id" {
  description = "The only AWS account this root may touch (provider allowed_account_ids)."
  type        = string
  default     = "339713011188"
}

variable "region" {
  description = "AWS region. Must match the backend and the wake role's region condition."
  type        = string
  default     = "us-east-1"
}

variable "enable_aws_screener" {
  description = "Create the on-demand screener's VPC and instance. Off by default so merging this root creates nothing."
  type        = bool
  default     = false
}

variable "availability_zone" {
  description = "Zone for the subnet and instance. Confirm the size's instance type is offered there before enabling."
  type        = string
  default     = "us-east-1a"
}

variable "vpc_cidr" {
  description = "Dedicated screener VPC, distinct from the generator scorer's 10.118.0.0/24. One /24 holds one public subnet with no ingress."
  type        = string
  default     = "10.118.1.0/24"
}

variable "size" {
  description = "Logical compute size (modules/compute/aws/locals.tf). screener-on-demand is c7a.2xlarge."
  type        = string
  default     = "screener-on-demand"
}

variable "image" {
  description = "Logical image. The hetzner_screener_fleet role supports Debian 12 and 13; Debian 12's Python 3.11 bounds ansible_core_version to 2.19.x."
  type        = string
  default     = "debian-12"
}

variable "boot_disk_gb" {
  description = "Persistent gp3 root volume. Docker images, cargo/build caches, the release checkout and the node credential survive each stop."
  type        = number
  default     = 200
}

variable "deletion_protection" {
  description = "EC2 termination protection. Set false only in the reviewed plan that precedes a retirement."
  type        = bool
  default     = true
}

variable "instance_profile_name" {
  description = "Instance profile created by aws-bootstrap (output screener_node_instance_profile_name). Referenced by name; this root creates no IAM."
  type        = string
  default     = "ditto-screener-node"
}

variable "ssm_agent_version" {
  description = "Exact amazon-ssm-agent release installed at first boot (operator break-glass). Debian AMIs do not ship the agent."
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

variable "ansible_core_version" {
  description = "Exact ansible-core installed into the first-boot venv. Must support Python 3.11 (Debian 12) and every collection in infra/ansible/requirements.yml (amazon.aws 11.5.0 needs >= 2.17)."
  type        = string
  default     = "2.19.14"

  validation {
    condition     = can(regex("^[0-9]+\\.[0-9]+\\.[0-9]+$", var.ansible_core_version))
    error_message = "ansible_core_version must be an exact X.Y.Z release."
  }
}

# --- bootstrap vars (rendered into /etc/ditto-screener-fleet/bootstrap-vars.json)

variable "screener_fleet_node_id" {
  description = "Platform node id this host enrolls as."
  type        = string
  default     = "subnet-screener-3"

  validation {
    condition     = can(regex("^subnet-screener-[1-9][0-9]*$", var.screener_fleet_node_id))
    error_message = "screener_fleet_node_id must look like subnet-screener-<n>."
  }
}

variable "screener_fleet_release_sha" {
  description = "Exact 40-hex semantic-release commit the first boot checks out and converges. Later releases arrive through the on-host signed updater."
  type        = string
  default     = ""

  validation {
    condition     = var.screener_fleet_release_sha == "" || can(regex("^[0-9a-f]{40}$", var.screener_fleet_release_sha))
    error_message = "screener_fleet_release_sha must be empty or a full lowercase commit SHA."
  }
}

variable "screener_fleet_builder_image" {
  description = "Submission builder image, pinned by digest."
  type        = string
  default     = ""

  validation {
    # Same pattern the aws-screener-node playbook asserts.
    condition     = var.screener_fleet_builder_image == "" || can(regex("^[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$", var.screener_fleet_builder_image))
    error_message = "screener_fleet_builder_image must be empty or registry/repository@sha256:<64 hex>."
  }
}

variable "screener_fleet_aws_wif_provider" {
  description = "Full GCP WIF provider resource (gcp-platform output screener_aws_wif_provider)."
  type        = string
  default     = ""

  validation {
    condition = (
      var.screener_fleet_aws_wif_provider == "" ||
      can(regex("^projects/[0-9]+/locations/global/workloadIdentityPools/screener-aws/providers/aws-ec2$", var.screener_fleet_aws_wif_provider))
    )
    error_message = "screener_fleet_aws_wif_provider must be empty or projects/<number>/locations/global/workloadIdentityPools/screener-aws/providers/aws-ec2."
  }
}

variable "screener_fleet_gcp_project" {
  description = "GCP project holding the screener secrets."
  type        = string
  default     = "ditto-app-dev"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.screener_fleet_gcp_project))
    error_message = "screener_fleet_gcp_project must be a GCP project id."
  }
}

variable "screener_fleet_registration_token_secret" {
  description = "Secret Manager secret holding the single-use Platform registration grant (gcp-platform screener-aws.tf)."
  type        = string
  default     = "screener-aws-registration-token"

  validation {
    condition     = can(regex("^[A-Za-z0-9_-]{1,255}$", var.screener_fleet_registration_token_secret))
    error_message = "screener_fleet_registration_token_secret must be a bare Secret Manager secret id."
  }
}

variable "screener_fleet_idle_exit_seconds" {
  description = "Worker idle period after which it exits and the host powers itself off (EC2 stop); 900 is the intended value. No default: the worker gains idle exit in the DITTO-2594 runtime release (#3459), so enabling the root requires setting it explicitly, and the first-boot playbook refuses a release_sha whose worker lacks it."
  type        = number
  default     = null

  validation {
    condition     = var.screener_fleet_idle_exit_seconds == null || (var.screener_fleet_idle_exit_seconds >= 60 && var.screener_fleet_idle_exit_seconds <= 86400 && floor(var.screener_fleet_idle_exit_seconds) == var.screener_fleet_idle_exit_seconds)
    error_message = "screener_fleet_idle_exit_seconds must be a whole number from 60 to 86400."
  }
}

variable "screener_fleet_worker_processes" {
  description = "Worker processes on this host."
  type        = number
  default     = 1

  validation {
    condition     = var.screener_fleet_worker_processes >= 1 && var.screener_fleet_worker_processes <= 16 && floor(var.screener_fleet_worker_processes) == var.screener_fleet_worker_processes
    error_message = "screener_fleet_worker_processes must be a whole number from 1 to 16."
  }
}

variable "screener_fleet_platform_url" {
  description = "Platform API base URL."
  type        = string
  default     = "https://platform-api.heyditto.ai"

  validation {
    condition     = can(regex("^https://[^/]+$", var.screener_fleet_platform_url))
    error_message = "screener_fleet_platform_url must be an https origin with no path."
  }
}
