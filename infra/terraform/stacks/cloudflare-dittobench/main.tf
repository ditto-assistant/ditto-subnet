# Wrangler owns the Worker bundle and its encrypted bindings. Terraform owns
# the stable production hostname, so DNS/routing changes receive a plan and a
# protected apply independently from application deployment.
resource "cloudflare_workers_custom_domain" "backroom" {
  account_id = var.cloudflare_account_id
  zone_id    = var.cloudflare_zone_id
  hostname   = var.backroom_hostname
  service    = var.backroom_worker_name
}
