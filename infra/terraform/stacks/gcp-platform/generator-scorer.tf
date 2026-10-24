###############################################################################
# DittoBench generator competition scorer.
#
# challenge-scorer (services/dittobench-api/cmd/challenge-scorer) polls
# Platform for generator qualification and round work, builds each miner's Go
# generator in a networkless, read-only Docker sandbox, and posts the verdict.
# It talks to no model. It runs on its own private VM because it executes
# untrusted miner code: the Platform, screener, and validator hosts never build
# a generator, and this VM's identity can read nothing but its own bearer.
###############################################################################

variable "enable_generator_scorer" {
  description = "Create the private generator-competition scorer VM, its identity, and its Platform bearer secret."
  type        = bool
  default     = false
}

locals {
  generator_scorer_count = var.enable_generator_scorer ? 1 : 0
}

# The value is populated out of band (a random 64-hex bearer). Platform reads it
# as DITTO_GENERATOR_SCORER_TOKEN; the scorer presents it on every request.
resource "google_secret_manager_secret" "generator_scorer_token" {
  count     = local.generator_scorer_count
  project   = var.project
  secret_id = "generator-scorer-token-prod"
  replication {
    auto {}
  }
  lifecycle {
    prevent_destroy = true
  }
}

resource "google_service_account" "generator_scorer" {
  count        = local.generator_scorer_count
  project      = var.project
  account_id   = "ditto-generator-scorer"
  display_name = "Ditto Generator Competition Scorer"
}

resource "google_secret_manager_secret_iam_member" "generator_scorer_token_scorer_access" {
  count     = local.generator_scorer_count
  project   = var.project
  secret_id = google_secret_manager_secret.generator_scorer_token[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.generator_scorer[0].email}"
}

resource "google_secret_manager_secret_iam_member" "generator_scorer_token_platform_access" {
  count     = local.generator_scorer_count
  project   = var.project
  secret_id = google_secret_manager_secret.generator_scorer_token[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${local.platform_api_sa_email}"
}

# e2-standard-4: each sandboxed build is capped at 2 vCPU / 2 GiB, leaving room
# for the generator run, Docker, and the scorer itself.
module "generator_scorer_vm" {
  source   = "../../modules/compute/gcp"
  count    = local.generator_scorer_count
  project  = var.project
  name     = "ditto-generator-scorer-prod"
  size     = "app-standard"
  image    = "debian-13"
  location = var.zone

  subnetwork       = module.network.subnetwork_id
  network_tags     = [module.network.ssh_target_tag]
  assign_public_ip = false
  boot_disk_gb     = 50

  service_account_email = google_service_account.generator_scorer[0].email
  labels = {
    env     = "prod"
    role    = "generator-scorer"
    managed = "terraform"
  }
}

output "generator_scorer_vm_name" {
  description = "Private generator-scorer VM name (empty when disabled)."
  value       = var.enable_generator_scorer ? module.generator_scorer_vm[0].hostname : ""
}

output "generator_scorer_sa_email" {
  description = "Identity that can read only the generator-scorer bearer."
  value       = var.enable_generator_scorer ? google_service_account.generator_scorer[0].email : ""
}
