###############################################################################
# SN118 prod screener FLEET — horizontally scaled, queue-depth autoscaled.
# (optional; gated by var.enable_screener_fleet)
#
# The screener is a PULL worker: each instance leases one submission at a time
# from the platform's screening queue (`POST /api/v1/screener/claim`, backed by
# `SELECT … FOR UPDATE SKIP LOCKED` + a 30-min lease TTL), so "load balancing"
# is inherent in the queue — N workers drain it concurrently with no LB, no
# ingress, and no coordination beyond the lease rows. What this file adds is
# ELASTICITY: a regional MIG whose size follows the screening backlog.
#
#   backlog metric  custom.googleapis.com/ditto/screener/queue_depth
#                   (published every 60s by a systemd timer on the prod
#                   platform app VM — infra/ansible/roles/screening_queue_metric —
#                   from the platform's own /api/v1/public/activity counts:
#                   waiting_screening + screening)
#   autoscaler      emergency ONLY_SCALE_OUT watchdog. The normal fenced
#                   controller owns both providers and may return the MIG to 0.
#   bootstrap       instance-template startup script: fetches the read-only
#                   repo deploy key from Secret Manager, clones ditto-subnet,
#                   and hands off to scripts/bootstrap-screener.sh in that repo
#                   (which installs Docker/uv, renders screener.env from Secret
#                   Manager, and runs the repo's exact-commit updater)
#   deploys         the ditto-subnet screener deploy workflow targets instances by
#                   label (env=prod, role in screener|screener-fleet), so fleet
#                   instances receive the same exact-commit updates as the pet
#                   VM; a freshly booted instance starts from origin/main and
#                   is converged by the next scheduled deploy run
#
# All instances share the single prod screener identity (hotkey + bearer token
# + sr25519 key): the platform authenticates ONE screener principal today. The
# lease layer disambiguates work per attempt row, so this is safe; the known
# cosmetic loss is the fleet heartbeat (keyed by hotkey, so N workers collapse
# into one row on /screeners). Per-worker heartbeat identity is a platform
# follow-up, not a blocker. See docs/screener-scaling.md.
#
# Rollout order matters (metric before autoscaler does anything useful); the
# runbook lives in docs/screener-scaling.md.
###############################################################################

variable "enable_screener_fleet" {
  description = "Create the autoscaled prod screener fleet (regional MIG + queue-depth autoscaler). Off by default. The pet ditto-screener-prod VM can run alongside the fleet safely (the lease queue serializes work); decommission it once the fleet is verified."
  type        = bool
  default     = false
}

variable "enable_screener_fleet_secrets" {
  description = "Create the fleet's deploy-key secret container + its accessor grant + the metric-writer grant WITHOUT creating the MIG. This is the first of the two-phase stand-up: apply with this on, add the deploy-key secret VERSION out of band, converge the metric publisher, and only THEN flip enable_screener_fleet on. Enabling the fleet implies these (the MIG cannot boot without them), so a one-shot enable still works — but the two-phase path avoids booting a VM before a key version exists. See docs/screener-scaling.md."
  type        = bool
  default     = false
}

variable "screener_fleet_readiness_port" {
  description = "TCP port the worker's readiness server binds (SCREENER_READINESS_PORT). The MIG autohealing health check and the GCP health-check firewall target this port; a fresh/broken instance that never becomes ready is recreated instead of being counted as idle capacity."
  type        = number
  default     = 8099
}

variable "screener_fleet_release_sha" {
  description = "Exact semantic-release source commit booted by every fresh GCE screener. Required when the fleet is enabled; mutable branches are forbidden."
  type        = string
  default     = ""
  validation {
    condition     = var.screener_fleet_release_sha == "" || can(regex("^[0-9a-f]{40}$", var.screener_fleet_release_sha))
    error_message = "screener_fleet_release_sha must be empty or a full lowercase commit SHA."
  }
}

variable "screener_fleet_image" {
  description = "Boot image for fleet instances. Default is stock Debian 13. After the ditto-subnet screener bake publishes the golden family, select it for faster first claim. bootstrap-screener.sh is idempotent and stores no baked credential."
  type        = string
  default     = "projects/debian-cloud/global/images/family/debian-13"
}

variable "screener_fleet_machine_type" {
  description = "Machine type for fleet instances. n2d-standard-4 = the steady-state 'validator' class (4 vCPU / 16 GB, enough for one Docker+cargo build at a time) on the N2D family the prod screener already uses. Scaling is horizontal — prefer more instances over bigger ones."
  type        = string
  default     = "n2d-standard-4"
}

variable "screener_fleet_boot_disk_gb" {
  description = "Boot disk per fleet instance: checkout + uv venv + Docker images + the 12 GB bounded BuildKit cache (deploy/daemon.json + updater GC keep it bounded)."
  type        = number
  default     = 100
}

variable "screener_fleet_min_replicas" {
  description = "Emergency autoscaler floor. Zero is intentional: the fenced capacity controller owns normal scale-in and can leave no idle GCE screeners."
  type        = number
  default     = 0
}

variable "screener_fleet_max_replicas" {
  description = "Autoscaler ceiling. 6× n2d-standard-4 = 24 vCPU, ~3× the burst capacity that drained the policy-v7 rescreen backlog in a day."
  type        = number
  default     = 6
}

variable "screener_fleet_backlog_per_instance" {
  description = "Queued or in-flight submissions each instance is expected to absorb (autoscaler single_instance_assignment). At ~5-20 min per screening, 6 per instance scales out once each worker's share exceeds the 5-7 submission catch-up target."
  type        = number
  default     = 6
}

variable "screener_fleet_zones" {
  description = "Zones the regional MIG spreads over. All four us-central1 zones offer N2D; spreading widens burst capacity headroom."
  type        = list(string)
  default     = ["us-central1-a", "us-central1-b", "us-central1-c", "us-central1-f"]
}

locals {
  screener_fleet_count = var.enable_screener_fleet ? 1 : 0
  # Secret container + IAM grants exist whenever EITHER flag is on, so they can
  # be created and populated a full apply before the MIG boots (two-phase
  # stand-up) while a one-shot `enable_screener_fleet=true` still creates them.
  screener_fleet_secrets_count = (var.enable_screener_fleet || var.enable_screener_fleet_secrets) ? 1 : 0
  screener_queue_metric        = "custom.googleapis.com/ditto/screener/queue_depth"
  screener_fleet_tag           = "ditto-screener-fleet"
}

check "screener_fleet_uses_released_source" {
  assert {
    condition     = !var.enable_screener_fleet || can(regex("^[0-9a-f]{40}$", var.screener_fleet_release_sha))
    error_message = "enable_screener_fleet requires screener_fleet_release_sha at an immutable semantic-release commit."
  }
}

# --- Read-only deploy key for the private ditto-subnet monorepo ---------------
# Fleet instances clone the repo at boot, so the key the pet VM holds only on
# its disk moves to Secret Manager. VALUE is added out of band:
#   gcloud secrets versions add screener-repo-deploy-key-prod --data-file=<key>
# Register only the PUBLIC half as a read-only deploy key on ditto-subnet.
resource "google_secret_manager_secret" "screener_repo_deploy_key" {
  count     = local.screener_fleet_secrets_count
  project   = var.project
  secret_id = "screener-repo-deploy-key-prod"
  replication {
    auto {}
  }
  lifecycle {
    prevent_destroy = true
  }
}

resource "google_secret_manager_secret_iam_member" "screener_repo_deploy_key_access" {
  count     = local.screener_fleet_secrets_count
  project   = var.project
  secret_id = google_secret_manager_secret.screener_repo_deploy_key[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.screener_worker.email}"
}

# --- Queue-depth metric writer -------------------------------------------------
# The publisher timer runs on the prod platform app VM as the same runtime SA.
# metricWriter only permits writing time series — no read/dashboard access.
resource "google_project_iam_member" "screener_queue_metric_writer" {
  # Bound to the secrets phase, not the MIG: the metric publisher on the
  # platform VM must be able to write points BEFORE the autoscaler exists, so
  # the autoscaler never starts against an absent metric.
  count   = local.screener_fleet_secrets_count
  project = var.project
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${local.run_sa_email}"
}

# The queue publisher moves with the platform app identity. Add the dedicated
# identity first and retain the shared member until the prod cutover is proven.
resource "google_project_iam_member" "platform_api_screener_queue_metric_writer" {
  count   = local.screener_fleet_secrets_count
  project = var.project
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${local.platform_api_sa_email}"
}

# --- Instance template ---------------------------------------------------------
# Mirrors the pet screener VM's shape (private, IAP-SSH only, NAT egress, run
# SA) but bootstraps itself: MIG instances must come up with zero manual steps.
resource "google_compute_instance_template" "screener_fleet" {
  count        = local.screener_fleet_count
  project      = var.project
  name_prefix  = "ditto-screener-fleet-"
  machine_type = var.screener_fleet_machine_type
  region       = var.region

  disk {
    source_image = var.screener_fleet_image
    auto_delete  = true
    boot         = true
    disk_size_gb = var.screener_fleet_boot_disk_gb
    # pd-standard on purpose: fleet instances draw from the wide-open
    # DISKS_TOTAL_GB quota (0/4096 GB) instead of SSD_TOTAL_GB, which sits at
    # 460/500 GB from the long-lived VMs and blocked every fleet scale-up on
    # 2026-07-16 (QUOTA_EXCEEDED; the 500->1200 quota request settled ungranted).
    # Screening is CPU/network-bound; slower boot-disk IO is an acceptable
    # trade for actually getting burst instances.
    disk_type = "pd-standard"
  }

  network_interface {
    subnetwork = module.network.subnetwork_id
    # No access_config: egress via the platform Cloud NAT, SSH via IAP.
  }

  # ssh_target_tag: IAP-SSH. screener_fleet_tag: targeted by the health-check
  # firewall so the MIG can probe the worker's readiness port.
  tags   = [module.network.ssh_target_tag, local.screener_fleet_tag]
  labels = { env = "prod", role = "screener-fleet", managed = "terraform" }

  metadata = {
    enable-oslogin = "TRUE"
    startup-script = templatefile("${path.module}/files/screener-fleet-startup.sh.tpl", {
      project           = var.project
      deploy_key_secret = google_secret_manager_secret.screener_repo_deploy_key[0].secret_id
      repo_url          = "git@github.com:ditto-assistant/ditto-subnet.git"
      git_revision      = var.screener_fleet_release_sha
      platform_api_url  = "https://${var.api_domain_prod}"
      screener_hotkey   = "5G6fGXnXFYdLM3ZyAm9whUbCY4ziQzcbMiTEqZB5c9KekTtR"
      netuid            = 118
      mnemonic_secret   = google_secret_manager_secret.screener_hotkey_mnemonic_prod[0].secret_id
      api_token_secret  = google_secret_manager_secret.screener_api_token_prod[0].secret_id
      readiness_port    = var.screener_fleet_readiness_port
    })
  }

  service_account {
    email  = google_service_account.screener_worker.email
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  }

  scheduling {
    automatic_restart   = true
    on_host_maintenance = "MIGRATE"
  }

  lifecycle {
    create_before_destroy = true
    precondition {
      # The template reads the hotkey-mnemonic + api-token secrets that
      # enable_screener_prod owns; the fleet cannot exist without them.
      condition     = var.enable_screener_prod
      error_message = "enable_screener_fleet requires enable_screener_prod=true (the fleet reuses the prod screener identity secrets)."
    }
  }
}

# --- Readiness health check (autohealing signal only; nothing routes here) -----
# The worker runs a tiny readiness server (SCREENER_READINESS_PORT) that returns
# 200 only once the claim loop is live and its platform preflight passed. A
# fresh instance whose bootstrap died (apt / Secret Manager / clone / updater
# failure) never opens that port, so autohealing recreates it instead of the
# autoscaler counting a broken RUNNING VM as drained capacity.
resource "google_compute_health_check" "screener_fleet_readiness" {
  count               = local.screener_fleet_count
  project             = var.project
  name                = "ditto-screener-fleet-readiness"
  check_interval_sec  = 30
  timeout_sec         = 10
  healthy_threshold   = 1
  unhealthy_threshold = 4

  http_health_check {
    port         = var.screener_fleet_readiness_port
    request_path = "/healthz"
  }
}

# GCP health checkers live in these fixed ranges; the fleet VMs are otherwise
# IAP-SSH only, so this is the single ingress that reaches the readiness port,
# and only on fleet-tagged instances.
resource "google_compute_firewall" "screener_fleet_health_check" {
  count         = local.screener_fleet_count
  project       = var.project
  name          = "${module.network.network_name}-allow-screener-fleet-hc"
  network       = module.network.network_id
  direction     = "INGRESS"
  source_ranges = ["35.191.0.0/16", "130.211.0.0/22"]
  target_tags   = [local.screener_fleet_tag]

  allow {
    protocol = "tcp"
    ports    = [tostring(var.screener_fleet_readiness_port)]
  }
}

# --- Regional MIG ----------------------------------------------------------------
# Autohealing reconciles capacity against real readiness (see the health check
# above); initial_delay covers the full first-boot bootstrap (apt + docker + uv
# sync + clone + updater) so a slow-but-healthy boot is never killed mid-flight.
# systemd Restart=always still covers transient process death within a ready
# instance. No named ports: nothing routes TO these instances.
resource "google_compute_region_instance_group_manager" "screener_fleet" {
  count                     = local.screener_fleet_count
  project                   = var.project
  name                      = "ditto-screener-fleet"
  region                    = var.region
  distribution_policy_zones = var.screener_fleet_zones
  base_instance_name        = "ditto-screener-fleet"

  # ANY (not the default EVEN): place instances in whichever allowed zone has
  # capacity. A small fleet cares far more about coming up at all than about an
  # even zonal spread, and EVEN made the MIG pin its single instance to one zone
  # and retry there through a ZONE_RESOURCE_POOL_EXHAUSTED stockout instead of
  # routing to a zone with n2d-standard-4 capacity.
  distribution_policy_target_shape = "ANY"

  version {
    instance_template = google_compute_instance_template.screener_fleet[0].id
  }

  auto_healing_policies {
    health_check      = google_compute_health_check.screener_fleet_readiness[0].id
    initial_delay_sec = 900
  }

  # Template changes roll out lazily (new instances only): running workers are
  # mid-build and get code updates through the deploy workflow anyway. Replace
  # deliberately with `gcloud compute instance-groups managed rolling-action
  # start-update` when a template change must reach existing instances.
  update_policy {
    type                  = "OPPORTUNISTIC"
    minimal_action        = "REPLACE"
    max_surge_fixed       = length(var.screener_fleet_zones)
    max_unavailable_fixed = 0
  }
}

# --- Autoscaler: size = ceil(queue_depth / backlog_per_instance) ---------------
resource "google_compute_region_autoscaler" "screener_fleet" {
  count   = local.screener_fleet_count
  project = var.project
  name    = "ditto-screener-fleet"
  region  = var.region
  target  = google_compute_region_instance_group_manager.screener_fleet[0].id

  autoscaling_policy {
    # Platform's fenced controller is the sole normal writer. This independent
    # GCP mechanism is only a stale-controller safety net: it may add GCE
    # capacity for a published backlog but can never race the controller by
    # deleting a worker. The controller drains and resizes back to zero.
    mode         = "ONLY_SCALE_OUT"
    min_replicas = var.screener_fleet_min_replicas
    max_replicas = var.screener_fleet_max_replicas
    # Bootstrap (apt + docker + uv sync + clone) takes ~5 min; don't judge a
    # new instance's contribution before it can have claimed anything.
    cooldown_period = 420

    metric {
      name = local.screener_queue_metric
      # Per-group signal: one global time series (published by the platform
      # VM), divided by the per-instance assignment to get the target size.
      filter                     = "resource.type = \"global\" AND metric.labels.env = \"prod\""
      single_instance_assignment = var.screener_fleet_backlog_per_instance
    }

  }
}

output "screener_fleet_mig" {
  description = "Regional MIG name for the prod screener fleet (empty when disabled)."
  value       = var.enable_screener_fleet ? google_compute_region_instance_group_manager.screener_fleet[0].name : ""
}
