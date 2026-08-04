variable "project" {
  description = "GCP project ID. Empty uses the provider's configured project."
  type        = string
  default     = ""
}

variable "region" {
  description = "Region for the subnet, router, and Cloud NAT."
  type        = string
  default     = "us-central1"
}

variable "network_name" {
  description = "Name of the custom VPC."
  type        = string
  default     = "ditto-net"
}

variable "subnet_name" {
  description = "Name of the regional subnet that hosts the Postgres VM and backs Cloud Run Direct VPC egress."
  type        = string
  default     = "ditto-us-central1"
}

variable "subnet_cidr" {
  description = "Primary CIDR for the subnet. /24 is ample for one PG VM plus Cloud Run Direct VPC egress IPs."
  type        = string
  default     = "10.10.0.0/24"
}

variable "postgres_target_tag" {
  description = "Network tag applied to the Postgres VM; firewall rules target it."
  type        = string
  default     = "postgres"
}

variable "ssh_target_tag" {
  description = "Network tag for instances reachable over SSH via IAP."
  type        = string
  default     = "ssh"
}

variable "internal_db_ports" {
  description = "TCP ports the subnet may reach on the Postgres VM (Postgres + PgBouncer)."
  type        = list(string)
  default     = ["5432", "6432"]
}

variable "iap_source_range" {
  description = "Google IAP TCP-forwarding source range. Only IAP may open SSH."
  type        = string
  default     = "35.235.240.0/20"
}

variable "enable_nat" {
  description = "Provision Cloud Router + Cloud NAT so the private (no-public-IP) VM has outbound internet."
  type        = bool
  default     = true
}

variable "nat_logging" {
  description = "Enable Cloud NAT logging (ERRORS_ONLY). Off by default to avoid log cost."
  type        = bool
  default     = false
}
