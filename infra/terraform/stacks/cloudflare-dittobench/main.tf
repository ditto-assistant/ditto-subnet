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
