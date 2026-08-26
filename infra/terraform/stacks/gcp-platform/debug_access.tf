###############################################################################
# Least-privilege GCP access for repo debug skills (gcloud-ditto-readonly,
# ditto-subnet-runtime-profiling) and leftover GCE hosts still being drained.
# Distinct from ssh_users, which is sudo SSH on postgres + both app VMs.
#
# Required by those skills:
#   * IAP + osAdminLogin on named subnet VMs — query_prod_db.sh runs
#     `sudo -n -u deploy` to source /opt/ditto-platform/.env; py-spy uses sudo
#     for ptrace; pprofctl wraps IAP SSH to loopback profilers. Dev and the
#     leftover screener/validator boxes are included so operators can debug
#     those hosts without project Editor.
#   * secretAccessor on TARGON_API_KEY only — query_targon.sh streams the key
#     into targon_cli. No other Secret Manager secrets.
#
# Not granted: project Editor/Owner, IAM admin, Cloud Run mutate, instance
# start/stop, postgres SSH, Platform DB/admin/OpenRouter secrets.
###############################################################################

locals {
  debug_operators = toset(var.debug_operators)

  # Named GCE hosts. ssh_users already covers platform-dev/prod (and postgres,
  # which is intentionally omitted here). Leftover screener/validator VMs are
  # still live in prod.auto.tfvars while Targon-first retires them.
  debug_named_vms = merge(
    {
      platform_prod = {
        vm                   = module.app["prod"].hostname
        zone                 = var.zone
        covered_by_ssh_users = true
      }
      platform_dev = {
        vm                   = module.app["dev"].hostname
        zone                 = var.zone
        covered_by_ssh_users = true
      }
    },
    var.enable_screener ? {
      screener_dev = {
        vm                   = module.screener_vm[0].hostname
        zone                 = var.zone
        covered_by_ssh_users = false
      }
    } : {},
    var.enable_screener_prod ? {
      screener_prod = {
        vm                   = module.screener_vm_prod[0].hostname
        zone                 = var.screener_prod_zone
        covered_by_ssh_users = false
      }
    } : {},
    var.enable_screener_capacity_controller ? {
      screener_capacity = {
        vm                   = module.screener_capacity_controller_vm[0].hostname
        zone                 = var.zone
        covered_by_ssh_users = false
      }
    } : {},
    var.enable_validator ? {
      validator_dev = {
        vm                   = module.validator_vm[0].hostname
        zone                 = var.zone
        covered_by_ssh_users = false
      }
    } : {},
  )

  debug_ssh_grants = {
    for pair in setproduct(local.debug_operators, keys(local.debug_named_vms)) :
    "${pair[0]}::${pair[1]}" => {
      member = pair[0]
      vm     = local.debug_named_vms[pair[1]].vm
      zone   = local.debug_named_vms[pair[1]].zone
    }
    if !local.debug_named_vms[pair[1]].covered_by_ssh_users || !contains(var.ssh_users, pair[0])
  }

  debug_fleet_operators = var.enable_screener_fleet ? local.debug_operators : toset([])
}

resource "google_compute_instance_iam_member" "debug_operator_osadmin" {
  for_each      = local.debug_ssh_grants
  project       = var.project
  zone          = each.value.zone
  instance_name = each.value.vm
  role          = "roles/compute.osAdminLogin"
  member        = each.value.member
}

# Instance-level IAP IAM 403s for the gcp-platform apply SA. Project-level
# IAP with a hostname prefix already applied for the fleet, so use that path
# for the named leftover VMs too. ditto-pg-platform is excluded.
resource "google_project_iam_member" "debug_operator_iap" {
  for_each = local.debug_operators
  project  = var.project
  role     = "roles/iap.tunnelResourceAccessor"
  member   = each.value

  condition {
    title       = "subnet_debug_and_leftover_vms"
    description = "IAP only to platform, screener, validator, and leftover fleet instances. Not postgres."
    expression  = <<-EOT
      resource.name.extract('/instances/{name}').startsWith('ditto-platform-')
      || resource.name.extract('/instances/{name}').startsWith('ditto-screener-')
      || resource.name.extract('/instances/{name}').startsWith('ditto-validator-')
    EOT
  }

  depends_on = [google_project_service.iap]
}

# Fleet instances are ephemeral (`ditto-screener-fleet-*`, often at zero). Bind
# login to that name prefix instead of a missing instance address.
resource "google_project_iam_member" "debug_operator_fleet_osadmin" {
  for_each = local.debug_fleet_operators
  project  = var.project
  role     = "roles/compute.osAdminLogin"
  member   = each.value

  condition {
    title       = "only_ditto_screener_fleet_instances"
    description = "Restrict leftover fleet SSH to ditto-screener-fleet-* instances."
    expression  = "resource.name.extract('/instances/{name}').startsWith('ditto-screener-fleet')"
  }
}

resource "google_project_iam_member" "debug_operator_fleet_iap" {
  for_each = local.debug_fleet_operators
  project  = var.project
  role     = "roles/iap.tunnelResourceAccessor"
  member   = each.value

  condition {
    title       = "only_ditto_screener_fleet_instances"
    description = "Restrict leftover fleet IAP to ditto-screener-fleet-* instances."
    expression  = "resource.name.extract('/instances/{name}').startsWith('ditto-screener-fleet')"
  }

  depends_on = [google_project_service.iap]
}

resource "google_secret_manager_secret_iam_member" "debug_operator_targon" {
  for_each  = local.debug_operators
  project   = var.project
  secret_id = data.google_secret_manager_secret.targon_api_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = each.value
}
