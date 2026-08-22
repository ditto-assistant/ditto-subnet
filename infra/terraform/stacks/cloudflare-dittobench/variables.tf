variable "cloudflare_api_token" {
  description = "Scoped token with Workers Scripts Write, Workers KV Storage Write, and Pages Write on the Ditto account."
  type        = string
  sensitive   = true
}

variable "cloudflare_account_id" {
  description = "Cloudflare account containing the Backroom Worker."
  type        = string
}

variable "cloudflare_zone_id" {
  description = "Cloudflare zone ID for dittobench.ai."
  type        = string
}

variable "backroom_worker_name" {
  description = "Wrangler service name deployed from apps/backroom."
  type        = string
  default     = "ditto-subnet-backroom"
}

variable "backroom_hostname" {
  description = "Production hostname routed to the subnet Backroom Worker."
  type        = string
  default     = "backroom.dittobench.ai"
}
