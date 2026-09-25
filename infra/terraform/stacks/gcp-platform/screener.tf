###############################################################################
# Retained screener fleet identities. The stopped ditto-screener-dev pet and
# its one-off secret grant were retired after a separate protected apply
# disabled that VM's GCE deletion protection. GCE autoscaling and the always-on
# Hetzner node use the retained identities below.
###############################################################################

variable "screener_prod_boot_disk_gb" {
  description = "Legacy static prod screener boot disk, if re-enabled."
  type        = number
  default     = 160
}

# Submission workers never inherit the Platform API identity. Their runtime
# authority is limited to the exact signing, bearer, source-review, and deploy
# key secrets required by the worker bootstrap.
resource "google_service_account" "screener_worker" {
  project      = var.project
  account_id   = "ditto-screener-worker"
  display_name = "Ditto Screener Worker"
}

resource "google_secret_manager_secret_iam_member" "screener_source_review_access" {
  count     = (var.enable_screener_prod || var.enable_screener_fleet) ? 1 : 0
  project   = var.project
  secret_id = "validator-openrouter-key"
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.screener_worker.email}"
  depends_on = [
    google_secret_manager_secret.validator_openrouter_key,
  ]
}

# Ditto Inference key for the private review layers (L1 Luna, L2 Terra, L3 Sol,
# L4 GLM) once a node sets screener_fleet_review_inference_provider = ditto.
# Deliberately a separate secret from validator-openrouter-key: that one is
# shared by validators, the platform relay, the DittoBench role, and the Targon
# CLI, so moving the screener to another gateway must never rotate their key.
# Terraform owns the container only; an operator adds the ditto_inf_ version.
resource "google_secret_manager_secret" "screener_review_ditto_inference_key" {
  count     = (var.enable_screener_prod || var.enable_screener_fleet) ? 1 : 0
  project   = var.project
  secret_id = "screener-review-ditto-inference-key"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_iam_member" "screener_review_ditto_inference_access" {
  count     = (var.enable_screener_prod || var.enable_screener_fleet) ? 1 : 0
  project   = var.project
  secret_id = google_secret_manager_secret.screener_review_ditto_inference_key[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.screener_worker.email}"
}

###############################################################################
# Prod static screener VM (env=prod). Disabled while the autoscale fleet and
# Hetzner node carry screening capacity.
###############################################################################

variable "enable_screener_prod" {
  description = "Create the legacy static prod SN118 screener VM. Its shared signing/API identity persists independently while the GCE fleet is enabled."
  type        = bool
  default     = false
}

variable "screener_prod_zone" {
  description = "Zone retained for the legacy static prod screener VM."
  type        = string
  default     = "us-central1-c"
}

locals {
  screener_prod_count = var.enable_screener_prod ? 1 : 0
  # The pet VM is disposable, but its signing and API identities are shared by
  # the retained GCE overflow fleet. Keep those secrets independently of the
  # obsolete static VM's lifecycle.
  screener_prod_identity_count = (
    var.enable_screener_prod || var.enable_screener_fleet || var.enable_screener_fleet_secrets
  ) ? 1 : 0
}

# The screener signing hotkey mnemonic (for SS58 5G6fG...KekTtR). VALUE is added
# out of band (`gcloud secrets versions add screener-hotkey-mnemonic-prod`),
# never through Terraform state. prevent_destroy: losing it means re-registering
# the hotkey (permit + stake) from scratch.
resource "google_secret_manager_secret" "screener_hotkey_mnemonic_prod" {
  count     = local.screener_prod_identity_count
  project   = var.project
  secret_id = "screener-hotkey-mnemonic-prod"
  replication {
    auto {}
  }
  lifecycle {
    prevent_destroy = true
  }
}

# Let the platform runtime SA (which the prod screener VM runs as) read the
# finney mnemonic. The gh-token (validator-gh-token) grant to the same SA is
# already covered by validator.tf's validator_access binding.
resource "google_secret_manager_secret_iam_member" "screener_prod_mnemonic_access" {
  count     = local.screener_prod_identity_count
  project   = var.project
  secret_id = google_secret_manager_secret.screener_hotkey_mnemonic_prod[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.screener_worker.email}"
}

# The screener API bearer token, shared with the platform's /screener/* endpoints
# (the dedicated-credential auth that replaced the validator-permit gate). VALUE
# is added out of band (`gcloud secrets versions add screener-api-token-prod`),
# never through Terraform state. prevent_destroy: it must stay in sync with the
# platform's SCREENER_API_TOKEN or screening auth breaks.
resource "google_secret_manager_secret" "screener_api_token_prod" {
  count     = local.screener_prod_identity_count
  project   = var.project
  secret_id = "screener-api-token-prod"
  replication {
    auto {}
  }
  lifecycle {
    prevent_destroy = true
  }
}

# Both the prod screener VM (sends the token) and the prod platform app VM
# (verifies it) run as the platform runtime SA and read this secret.
resource "google_secret_manager_secret_iam_member" "screener_prod_api_token_access" {
  count     = local.screener_prod_identity_count
  project   = var.project
  secret_id = google_secret_manager_secret.screener_api_token_prod[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.screener_worker.email}"
}


# The prod API verifies the same bearer after its VM moves to the dedicated
# identity. Keep the shared binding during the bounded rollback window.
resource "google_secret_manager_secret_iam_member" "platform_api_screener_prod_api_token_access" {
  count     = local.screener_prod_identity_count
  project   = var.project
  secret_id = google_secret_manager_secret.screener_api_token_prod[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${local.platform_api_sa_email}"
}

# Temporary policy-v7 rescreen capacity. Keep this explicit so returning to the
# steady-state validator class is a reviewed IaC change after the queue drains;
# horizontal scaling is provided by the autoscaled fleet in screener-fleet.tf.
#
# This VM is superseded by the dedicated Hetzner worker plus the autoscaled GCE
# overflow fleet. It is expressed directly (rather than through the persistent
# compute module) so its deletion protection can be removed in one reviewed
# apply before a later apply sets enable_screener_prod=false and destroys it.
resource "google_compute_instance" "screener_vm_prod" {
  count        = local.screener_prod_count
  project      = var.project
  name         = "ditto-screener-prod"
  machine_type = "n2d-standard-8"
  zone         = var.screener_prod_zone
  labels       = { env = "prod", role = "screener", managed = "terraform" }
  tags         = [module.network.ssh_target_tag]

  allow_stopping_for_update = true

  boot_disk {
    initialize_params {
      image = "projects/debian-cloud/global/images/family/debian-13"
      size  = var.screener_prod_boot_disk_gb
      type  = "pd-balanced"
    }
  }

  network_interface {
    subnetwork = module.network.subnetwork_id
  }

  metadata = { enable-oslogin = "TRUE" }

  service_account {
    email  = google_service_account.screener_worker.email
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  }

  shielded_instance_config {
    enable_secure_boot          = false
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  deletion_protection = false

  lifecycle {
    ignore_changes = [
      boot_disk[0].initialize_params[0].image,
      metadata["ssh-keys"],
    ]
  }
}

moved {
  from = module.screener_vm_prod[0].google_compute_instance.this
  to   = google_compute_instance.screener_vm_prod[0]
}

output "screener_prod_vm_name" {
  description = "Name of the prod screener VM (empty when enable_screener_prod = false)."
  value       = var.enable_screener_prod ? google_compute_instance.screener_vm_prod[0].name : ""
}

output "screener_prod_vm_internal_ip" {
  description = "Private IP of the prod screener VM (reachability is via IAP)."
  value       = var.enable_screener_prod ? google_compute_instance.screener_vm_prod[0].network_interface[0].network_ip : ""
}
