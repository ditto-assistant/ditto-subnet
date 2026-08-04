###############################################################################
# ditto-platform (Bittensor Subnet 118 API) — persistent GCP infrastructure.
#
# Unlike envs/gcp-prod (the unapplied backend migration), this stack stands on
# its own: its OWN VPC, its OWN dedicated Postgres VM, and one small app VM per
# deploy env. The app runs as a host process under pm2/uv with Pylon as a Docker
# sidecar (NOT Cloud Run) — see the ditto-subnet repo's scripts/ + the infra
# `platform_app` Ansible role.
#
#   Postgres                 -> dedicated GCE VM (private)   [modules/compute/gcp]
#   ditto-platform-{dev,prod}-> GCE app VMs (public IP)      [modules/compute/gcp]
#   agent tarballs           -> GCS buckets (S3 interop/HMAC)
#   platform-api[-dev]       -> Cloudflare A -> app VM IP    [modules/dns/cloudflare]
#
# Deploy flow (lives in the ditto-subnet repo): push dev|main -> WIF ->
# IAP SSH into the matching VM -> scripts/update.sh (git pull, uv sync, alembic
# upgrade, pm2 reload). Terraform owns only the persistent layer below.
###############################################################################

locals {
  env_label    = "platform"
  http_app_tag = "platform-http"

  # The app VMs run as (and the HMAC key + secret grants target) the
  # ditto-platform SA created in identity.tf. var.run_service_account_email is an
  # optional override (e.g. for tests); empty = use the SA this stack creates.
  run_sa_email          = var.run_service_account_email != "" ? var.run_service_account_email : google_service_account.ditto_platform.email
  platform_api_sa_email = google_service_account.platform_api.email

  # One app VM + bucket + DNS record + env-scoped secrets per deploy env. `dev`
  # tracks the ditto-platform `dev` branch; `prod` tracks `main`. Prod runs the
  # larger class: upload fingerprinting is GIL-bound in-process CPU and the
  # shared-core e2-medium saturated under the 2026-07-16 miner upload storm.
  app_envs = {
    dev  = { hostname = "ditto-platform-dev", domain = var.api_domain_dev, size = "app-small" }
    prod = { hostname = "ditto-platform-prod", domain = var.api_domain_prod, size = "app-standard" }
  }

  # Secrets the platform runtime SA reads at converge/deploy time (see the
  # platform_app Ansible role and ditto-platform scripts/update.sh). Collected
  # here to grant secretAccessor in one loop.
  # DITTO_UPLOAD_PAYMENT_ADDRESS is intentionally NOT here: its initial value
  # comes from the ditto-subnet repo's GitHub environment secret and is kept
  # in its separate mode-0600 .env.deploy file. See deploy.yml + update.sh.
  platform_secret_ids = concat(
    [
      "ADMIN_API_PASSWORD",
      google_secret_manager_secret.db_password.secret_id,
      google_secret_manager_secret.hmac_secret.secret_id,
      google_secret_manager_secret.github_deploy_key.secret_id,
      google_secret_manager_secret.taostats_api_key.secret_id,
    ],
    [for s in google_secret_manager_secret.pylon_open_access_token : s.secret_id],
  )
}

# --- Network: dedicated VPC + subnet + Cloud NAT (for the private PG VM) ---
module "network" {
  source       = "../../modules/network/gcp"
  project      = var.project
  region       = var.region
  network_name = "ditto-platform-net"
  subnet_name  = "ditto-platform-${var.region}"
  subnet_cidr  = var.subnet_cidr
  enable_nat   = true
}

# Public HTTP/HTTPS ingress to the app VMs. Caddy on each VM terminates TLS on
# :443 (ACME on :80). The network module ships only IAP-SSH + internal-DB rules,
# so this rule is required for the API to be reachable.
resource "google_compute_firewall" "app_http" {
  project       = var.project
  name          = "${module.network.network_name}-allow-http"
  network       = module.network.network_id
  direction     = "INGRESS"
  source_ranges = ["0.0.0.0/0"]
  target_tags   = [local.http_app_tag]

  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
  }
}

# --- Dedicated Postgres VM (private). Holds ditto_platform_dev + _prod DBs. ---
# db-staging (e2-standard-2, 2 vCPU / 8 GB) is ample for the platform's small
# working set. The app VMs reach it in-subnet via the module's allow-internal-db
# rule (source = subnet_cidr) on :5432 — no extra firewall needed.
module "pg_vm" {
  source                = "../../modules/compute/gcp"
  project               = var.project
  name                  = "ditto-pg-platform"
  size                  = "db-staging"
  image                 = "debian-13"
  location              = var.zone
  subnetwork            = module.network.subnetwork_id
  network_tags          = [module.network.postgres_target_tag, module.network.ssh_target_tag]
  data_disk_gb          = var.pg_data_disk_gb
  assign_public_ip      = false
  service_account_email = var.vm_service_account_email
  labels                = { env = local.env_label, role = "platform-postgres", managed = "terraform" }
}

# --- App VMs: one per deploy env (public IP for HTTP ingress + direct egress). ---
module "app" {
  source   = "../../modules/compute/gcp"
  for_each = local.app_envs

  project               = var.project
  name                  = each.value.hostname
  size                  = each.value.size
  image                 = "debian-13"
  location              = var.zone
  subnetwork            = module.network.subnetwork_id
  network_tags          = [module.network.ssh_target_tag, local.http_app_tag]
  assign_public_ip      = true
  boot_disk_gb          = var.app_boot_disk_gb
  service_account_email = var.platform_dedicated_identity_attached ? local.platform_api_sa_email : local.run_sa_email
  labels                = { env = each.key, role = "platform-app", managed = "terraform" }
}

# --- Agent-tarball object storage: one GCS bucket per env (S3 interop). ---
resource "google_storage_bucket" "agents" {
  for_each = local.app_envs

  project                     = var.project
  name                        = "ditto-platform-agents-${each.key}"
  location                    = var.bucket_location
  storage_class               = "STANDARD"
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  # Multipart uploads can leave billable parts behind indefinitely when a
  # screener exits before completing the upload. Abort only incomplete uploads;
  # this rule never targets a completed/current object.
  lifecycle_rule {
    condition {
      age = 1
    }
    action {
      type = "AbortIncompleteMultipartUpload"
    }
  }

  # Keep a bounded rollback window after the platform explicitly supersedes or
  # deletes an artifact. `ARCHIVED` is required: current source tarballs and
  # screened images remain available for active/top-agent rescoring regardless
  # of age. GCS's soft-delete window provides an additional recovery period
  # after this rule removes an archived generation.
  lifecycle_rule {
    condition {
      with_state                 = "ARCHIVED"
      days_since_noncurrent_time = 30
      send_age_if_zero           = false
    }
    action {
      type = "Delete"
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

# The app reaches the buckets through the GCS S3-compatible XML API (its existing
# aioboto3 code) using this HMAC key. Native-IAM access to the same buckets is
# granted too so the SA can manage them outside the S3 path.
resource "google_storage_hmac_key" "platform" {
  project               = var.project
  service_account_email = local.run_sa_email
}

resource "google_storage_bucket_iam_member" "agents_rw" {
  for_each = google_storage_bucket.agents
  bucket   = each.value.name
  role     = "roles/storage.objectAdmin"
  member   = "serviceAccount:${local.run_sa_email}"
}

resource "google_storage_bucket_iam_member" "platform_api_agents_rw" {
  for_each = google_storage_bucket.agents
  bucket   = each.value.name
  role     = "roles/storage.objectAdmin"
  member   = "serviceAccount:${local.platform_api_sa_email}"
}

# --- Sensitive app config in Secret Manager ---
# The platform runtime SA reads these at deploy time; the platform_app Ansible
# role renders them into /opt/ditto-platform/.env. Non-secret values (PG IP,
# bucket name, HMAC access id) come from this stack's outputs instead. Mirrors
# gcp-prod's "secret + version in Terraform, values via TF_VAR_*" approach.

# Shared across envs: one `ditto` Postgres role, one HMAC key for both buckets.
resource "google_secret_manager_secret" "db_password" {
  project   = var.project
  secret_id = "platform-db-password"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "db_password" {
  secret      = google_secret_manager_secret.db_password.id
  secret_data = var.db_password
}

resource "google_secret_manager_secret" "hmac_secret" {
  project   = var.project
  secret_id = "platform-storage-hmac-secret"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "hmac_secret" {
  secret      = google_secret_manager_secret.hmac_secret.id
  secret_data = google_storage_hmac_key.platform.secret
}

# Per-env: the Pylon open-access token the API and Pylon sidecar share.
resource "google_secret_manager_secret" "pylon_open_access_token" {
  for_each  = local.app_envs
  project   = var.project
  secret_id = "platform-${each.key}-pylon-open-access-token"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "pylon_open_access_token" {
  for_each    = local.app_envs
  secret      = google_secret_manager_secret.pylon_open_access_token[each.key].id
  secret_data = var.pylon_open_access_token[each.key]
}

# Read-only GitHub deploy key — the app VMs use it to clone/pull the (private)
# ditto-subnet repo over SSH. CONTAINER ONLY: the private-key value is added
# out of band (`gcloud secrets versions add platform-github-deploy-key --data-file=…`)
# and the matching public key registered as a read-only deploy key on the repo,
# so the key material never enters Terraform state. Mirrors envs/gcp-secrets.
resource "google_secret_manager_secret" "github_deploy_key" {
  project   = var.project
  secret_id = "platform-github-deploy-key"
  replication {
    auto {}
  }
  lifecycle {
    prevent_destroy = true
  }
}

# Optional public-dashboard validator-name enrichment. The value is populated
# out of band by the key owner so it never enters Terraform state. The platform
# VM reads it directly during scripts/update.sh; failures leave enrichment
# disabled without affecting health classification, scoring, or weights.
resource "google_secret_manager_secret" "taostats_api_key" {
  project   = var.project
  secret_id = "platform-taostats-api-key"
  replication {
    auto {}
  }
  lifecycle {
    prevent_destroy = true
  }
}

# The secret container was created and populated out of band before this
# resource was added. Adopt it on the next deliberate gcp-platform apply.
import {
  to = google_secret_manager_secret.taostats_api_key
  id = "projects/${var.project}/secrets/platform-taostats-api-key"
}

# Let the runtime SA read every platform secret above.
resource "google_secret_manager_secret_iam_member" "platform_access" {
  for_each  = toset(local.platform_secret_ids)
  project   = var.project
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${local.run_sa_email}"
}

resource "google_secret_manager_secret_iam_member" "platform_api_access" {
  for_each  = toset(local.platform_secret_ids)
  project   = var.project
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${local.platform_api_sa_email}"
}

# --- DNS: A record per env -> that app VM's public IP. ---
# DNS-only (proxied = false) so Caddy on the VM can complete ACME for its cert.
# Created directly (not via modules/dns/cloudflare): that module gates its
# A-record for_each on `ipv4 == ""`, which is UNKNOWN at plan when the IP is a
# freshly-created VM attribute. Here for_each keys off var.manage_dns (a known
# bool), so the record SET is known at plan and only the `content` (the IP) is
# deferred to apply.
resource "cloudflare_record" "api" {
  for_each = var.manage_dns ? local.app_envs : {}

  zone_id = var.cloudflare_zone_id
  name    = each.value.domain
  type    = "A"
  content = module.app[each.key].ipv4
  proxied = false # DNS-only so Caddy can issue the managed cert
  ttl     = 1     # automatic
  comment = "managed by terraform (infra/terraform/stacks/gcp-platform)"
}
