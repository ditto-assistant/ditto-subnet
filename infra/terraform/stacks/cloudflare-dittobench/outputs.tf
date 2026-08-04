output "backroom_hostname" {
  value = cloudflare_workers_custom_domain.backroom.hostname
}

output "backroom_custom_domain_id" {
  value = cloudflare_workers_custom_domain.backroom.id
}
