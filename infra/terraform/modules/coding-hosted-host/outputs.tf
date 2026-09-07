output "host" {
  description = "Non-secret qualification host identity, or null while disabled. Not runtime readiness."
  value = var.enabled ? {
    name            = module.host[0].hostname
    instance_id     = module.host[0].id
    private_ip      = module.host[0].internal_ip
    zone            = var.zone
    shadow_only     = true
    weight_eligible = false
  } : null
}
