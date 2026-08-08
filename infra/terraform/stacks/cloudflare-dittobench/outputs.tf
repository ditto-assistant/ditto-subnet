output "backroom_hostname" {
  value = cloudflare_workers_custom_domain.backroom.hostname
}

output "backroom_custom_domain_id" {
  value = cloudflare_workers_custom_domain.backroom.id
}

# Paste into the OAUTH_KV binding in apps/backroom/wrangler.jsonc.
output "backroom_oauth_kv_namespace_id" {
  value = cloudflare_workers_kv_namespace.backroom_oauth.id
}
