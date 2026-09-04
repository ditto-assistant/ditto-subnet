# Wrangler owns the Worker bundle and its encrypted bindings. Terraform owns
# the stable production hostname, so DNS/routing changes receive a plan and a
# protected apply independently from application deployment.
resource "cloudflare_workers_custom_domain" "backroom" {
  account_id = var.cloudflare_account_id
  zone_id    = var.cloudflare_zone_id
  hostname   = var.backroom_hostname
  service    = var.backroom_worker_name
}

# Durable store for the Backroom MCP OAuth provider: registered MCP clients,
# authorization grants, and issued access/refresh tokens. Terraform owns it for
# the same reason it owns the hostname — losing this namespace revokes every
# operator's MCP connection at once, so it must not be a side effect of an
# application deploy. `apps/backroom/wrangler.jsonc` binds it as OAUTH_KV and
# the deploy fails closed without it.
resource "cloudflare_workers_kv_namespace" "backroom_oauth" {
  account_id = var.cloudflare_account_id
  title      = "${var.backroom_worker_name}-oauth"
}

# Direct-upload target for exact-SHA public dashboard previews. PR code is
# built without credentials; a trusted workflow uploads only the static bundle
# and installs a repository-owned read-only public-API proxy at publication.
resource "cloudflare_pages_project" "dashboard_preview" {
  account_id        = var.cloudflare_account_id
  name              = "ditto-subnet-dashboard-preview"
  production_branch = "main"

  deployment_configs = {
    preview = {
      always_use_latest_compatibility_date = false
      compatibility_date                   = "2026-08-22"
      fail_open                            = false
    }
    production = {
      always_use_latest_compatibility_date = false
      compatibility_date                   = "2026-08-22"
      fail_open                            = false
    }
  }
}
