variable "project" {
  description = "GCP project ID. Empty uses the provider's configured project."
  type        = string
  default     = ""
}

variable "name" {
  description = "Cloud Run service name (e.g. backend, share, staging-backend)."
  type        = string
}

variable "location" {
  description = "Region for the service."
  type        = string
  default     = "us-central1"
}

variable "image" {
  description = "Full Artifact Registry image reference (with tag or digest)."
  type        = string
}

variable "service_account_email" {
  description = "Runtime service account for the service. Empty uses the project default compute SA."
  type        = string
  default     = ""
}

variable "ingress" {
  description = "Ingress setting. INGRESS_TRAFFIC_ALL is required when Cloudflare proxies to the run.app/domain-mapping URL."
  type        = string
  default     = "INGRESS_TRAFFIC_ALL"
}

variable "container_port" {
  description = "Port the container listens on (backend = 3400, share = its own)."
  type        = number
  default     = 8080
}

variable "cpu" {
  description = "CPU limit per instance (e.g. \"1\", \"2\")."
  type        = string
  default     = "1"
}

variable "memory" {
  description = "Memory limit per instance (e.g. \"512Mi\", \"1Gi\")."
  type        = string
  default     = "512Mi"
}

variable "min_instances" {
  description = "Minimum instances. 1 keeps prod warm; 0 lets staging scale to zero."
  type        = number
  default     = 0
}

variable "max_instances" {
  description = "Maximum instances (scale ceiling)."
  type        = number
  default     = 4
}

variable "concurrency" {
  description = "Max concurrent requests per instance."
  type        = number
  default     = 80
}

variable "timeout_seconds" {
  description = "Request timeout in seconds."
  type        = number
  default     = 300
}

variable "cpu_idle" {
  description = "Allocate CPU only during request processing (true) vs always-allocated (false). true is cheaper for warm-but-idle min instances."
  type        = bool
  default     = true
}

variable "startup_cpu_boost" {
  description = "Boost CPU during cold start for faster startup."
  type        = bool
  default     = true
}

variable "env" {
  description = "Plain (non-secret) environment variables."
  type        = map(string)
  default     = {}
}

variable "secret_env" {
  description = "Secret Manager-backed env vars: { ENV_NAME = { secret = \"secret-id\", version = \"latest\" } }."
  type = map(object({
    secret  = string
    version = string
  }))
  default = {}
}

variable "vpc_network" {
  description = "VPC self-link for Direct VPC egress to Postgres. Empty disables VPC egress."
  type        = string
  default     = ""
}

variable "vpc_subnetwork" {
  description = "Subnet self-link for Direct VPC egress. Required when vpc_network is set."
  type        = string
  default     = ""
}

variable "vpc_egress" {
  description = "Which egress goes through the VPC: PRIVATE_RANGES_ONLY (only RFC1918 → Postgres) or ALL_TRAFFIC."
  type        = string
  default     = "PRIVATE_RANGES_ONLY"
}

variable "vpc_tags" {
  description = "Network tags applied to the Direct VPC egress interface (for firewall source matching)."
  type        = list(string)
  default     = []
}

variable "domain" {
  description = "Custom domain to map (e.g. api.heyditto.ai). Empty skips the domain mapping. Requires var.project set."
  type        = string
  default     = ""
}

variable "allow_unauthenticated" {
  description = "Grant allUsers the run.invoker role (public service). Cloudflare/Caddy front it but invocation must be public."
  type        = bool
  default     = true
}

variable "deletion_protection" {
  description = "Block terraform destroy of the service. Set true for prod."
  type        = bool
  default     = false
}

variable "labels" {
  description = "Service labels."
  type        = map(string)
  default     = {}
}
