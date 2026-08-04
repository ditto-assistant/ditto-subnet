provider "google" {
  project = var.project
  region  = var.region
}

provider "cloudflare" {
  # A no-DNS apply (manage_dns = false) leaves cloudflare_api_token empty, but the
  # provider validates the token's charset even when unused — substitute a
  # syntactically-valid dummy that is never sent (no cloudflare resources exist
  # in that case). With manage_dns = true the real token must be provided.
  api_token = var.cloudflare_api_token != "" ? var.cloudflare_api_token : "0123456789012345678901234567890123456789"
}
