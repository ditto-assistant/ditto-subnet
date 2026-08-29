###############################################################################
# SN118 reference/dev validator VM (optional; gated by var.enable_validator).
#
# This resource is not the production validator. Production runs on
# DigitalOcean and is converged through its operator-owned host path. The
# generic Ansible roles below remain the runtime configuration authority, but
# this GCE VM, its attachment flags, and its lifecycle are a reference/dev
# rehearsal target only. The rest of gcp-platform Terraform remains production
# authority for the platform API, IAM, inference proxy, datagen, and app hosts.
#
# The optional private GCE VM runs, as host systemd services (see
# the infra `dittobench` + `validator_worker` Ansible roles):
#
#   dittobench-api   — the scoring engine, co-located. BUILDS + RUNS untrusted
#                      miner harnesses in Docker (the sandbox path Cloud Run
#                      can't host). Loopback-only.
#   ditto-validator  — the stateless worker: pulls the platform queue, scores
#                      via dittobench-api, sets weights on chain.
#
# No public IP: egress via the platform Cloud NAT, SSH via IAP — same posture as
# the Postgres VM. It is NOT part of the public HTTP ingress.
#
# This is OFF by default so a routine `terraform apply` of the platform stack
# doesn't create it. Flip it on deliberately:
#   terraform apply -var=enable_validator=true
# then converge with infra/ansible/playbooks/gcp-validator.yml. See
# docs/validator-deploy.md for the full runbook + secret population.
###############################################################################

variable "enable_validator" {
  description = "Create the reference/dev GCE SN118 validator VM and its Secret Manager containers. This does not manage the production DigitalOcean validator."
  type        = bool
  default     = false
}

variable "validator_boot_disk_gb" {
  description = "Boot disk for the validator VM. Holds the repo checkouts, the uv venv, Go build cache, Docker images, and cargo target dirs for miner builds."
  type        = number
  default     = 80
}

locals {
  validator_count              = var.enable_validator ? 1 : 0
  validator_wandb_secret_count = var.enable_validator || var.enable_validator_prod ? 1 : 0

  # Secret containers the validator VM's runtime SA reads at converge time
  # (rendered into env files by the Ansible roles). VALUES are added out of band
  # (`gcloud secrets versions add …`), never through Terraform state — same
  # pattern as the github deploy key. See docs/validator-deploy.md.
  #
  # Keys are STATIC literals, not `google_secret_manager_secret.*[0].secret_id`:
  # a secret created out of band (before it is imported into state) has an
  # unknown-until-apply `secret_id`, and `for_each` cannot take unknown keys —
  # which broke every non-targeted plan/apply/import. The literals must match
  # each resource's `secret_id` below.
  validator_non_provider_secret_ids = var.enable_validator ? [
    "validator-hotkey-mnemonic",
    "validator-gh-token",
    "validator-wandb-key",
    "validator-pylon-identity-token",
    "validator-pylon-open-access-token",
  ] : []
  validator_secret_ids = local.validator_non_provider_secret_ids

  # Preserve the pre-migration `validator_access[secret-id]` state addresses
  # for every grant held by the shared runtime identity. The provider grant
  # survives only for the bounded v6 transition; non-provider grants survive
  # until the drained validator identity flip. Keeping both sets under the
  # original resource prevents two Terraform addresses from managing the same
  # remote IAM membership during the transition.
  validator_transition_secret_ids = var.enable_validator ? setunion(
    var.validator_dedicated_identity_attached ? toset([]) : toset(local.validator_non_provider_secret_ids),
    !var.platform_dedicated_identity_attached || !var.validator_dedicated_identity_attached ? toset(["validator-openrouter-key"]) : toset([]),
  ) : toset([])
}

resource "google_service_account" "validator" {
  count        = local.validator_count
  project      = var.project
  account_id   = "ditto-validator"
  display_name = "Ditto validator runtime"
}

# --- The VM (private; label role=validator → Ansible group role_validator). ---
module "validator_vm" {
  source   = "../../modules/compute/gcp"
  count    = local.validator_count
  project  = var.project
  name     = "ditto-validator-dev"
  size     = "validator"
  image    = "debian-13"
  location = var.zone

  subnetwork       = module.network.subnetwork_id
  network_tags     = [module.network.ssh_target_tag] # IAP-SSH only; no public ingress
  assign_public_ip = false                           # egress via Cloud NAT
  boot_disk_gb     = var.validator_boot_disk_gb

  # Keep validator code outside the platform inference secret's IAM boundary.
  service_account_email = var.validator_dedicated_identity_attached ? google_service_account.validator[0].email : local.run_sa_email
  labels                = { env = "dev", role = "validator", managed = "terraform" }
}

# --- Secret containers (values added out of band; see the runbook). ---

# The validator's signing hotkey mnemonic. prevent_destroy: losing it means
# re-registering the hotkey (permit + stake) from scratch.
resource "google_secret_manager_secret" "validator_hotkey_mnemonic" {
  count     = local.validator_count
  project   = var.project
  secret_id = "validator-hotkey-mnemonic"
  replication {
    auto {}
  }
  lifecycle {
    prevent_destroy = true
  }
}

# Existing OpenRouter credential, retained only as the platform proxy upstream
# key. The dedicated validator identity is never granted access.
resource "google_secret_manager_secret" "validator_openrouter_key" {
  count     = local.validator_count
  project   = var.project
  secret_id = "validator-openrouter-key"
  replication {
    auto {}
  }
}

# A GitHub token (fine-grained PAT / App token) with READ on ditto-assistant/
# {dittobench-api, ditto-subnet, ditto-harness}. Clones the two repos over HTTPS
# and is mounted as the BuildKit gh_token for miner harness builds (which pull
# the private ditto-harness crate). Rotatable.
resource "google_secret_manager_secret" "validator_gh_token" {
  count     = local.validator_count
  project   = var.project
  secret_id = "validator-gh-token"
  replication {
    auto {}
  }
}

# Shared W&B API key for validator telemetry. Both the reference validator and
# the private production validator consume this existing, rotatable secret;
# Terraform owns only metadata and IAM, never a secret version or value.
resource "google_secret_manager_secret" "validator_wandb_key" {
  count     = local.validator_wandb_secret_count
  project   = var.project
  secret_id = "validator-wandb-key"
  replication {
    auto {}
  }
}

# --- Pylon identity (write) path (E1). Both are self-generated bearer tokens
# (`openssl rand -base64 32`), NOT external credentials: the co-located Pylon
# sidecar holds the mounted validator hotkey and signs set_weights itself; these
# tokens only authorize the worker to ask it to. Off unless the validator_pylon
# role's validator_pylon_identity_enabled is set (finney/testnet). Rotatable.

# Shared between the Pylon sidecar (PYLON_ID_VALIDATOR_TOKEN) and the worker
# (PYLON_IDENTITY_TOKEN) — the write-path authorization for put_weights.
resource "google_secret_manager_secret" "validator_pylon_identity_token" {
  count     = local.validator_count
  project   = var.project
  secret_id = "validator-pylon-identity-token"
  replication {
    auto {}
  }
}

# The Pylon sidecar's open-access (read) token, shared with the worker's
# PYLON_OPEN_ACCESS_TOKEN (the ChainClient's permit/neuron reads).
resource "google_secret_manager_secret" "validator_pylon_open_access_token" {
  count     = local.validator_count
  project   = var.project
  secret_id = "validator-pylon-open-access-token"
  replication {
    auto {}
  }
}

# Both Pylon token containers predate the public monorepo state handoff. Adopt
# their metadata without reading or replacing either secret value. These imports
# are idempotent once the resources are recorded in the GCS state.
import {
  to = google_secret_manager_secret.validator_pylon_identity_token[0]
  id = "projects/${var.project}/secrets/validator-pylon-identity-token"
}

import {
  to = google_secret_manager_secret.validator_pylon_open_access_token[0]
  id = "projects/${var.project}/secrets/validator-pylon-open-access-token"
}

# This secret + its runtime-SA accessor binding were first created out-of-band
# (gcloud) when telemetry was turned on; adopt them into state on the next apply
# so config is the source of truth. Harmless once imported.
import {
  to = google_secret_manager_secret.validator_wandb_key[0]
  id = "projects/${var.project}/secrets/validator-wandb-key"
}

import {
  for_each = contains(local.validator_transition_secret_ids, "validator-wandb-key") ? toset(["validator-wandb-key"]) : toset([])
  to       = google_secret_manager_secret_iam_member.validator_access[each.value]
  id       = "projects/${var.project}/secrets/validator-wandb-key roles/secretmanager.secretAccessor serviceAccount:${local.run_sa_email}"
}

# Keep the VM's pre-migration identity functional until the drained identity
# flip. These are the existing state addresses from main; do not rename or
# repurpose them while any shared-identity grant remains.
resource "google_secret_manager_secret_iam_member" "validator_access" {
  for_each  = local.validator_transition_secret_ids
  project   = var.project
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${local.run_sa_email}"

  # for_each keys are static literals (see local.validator_secret_ids), so the
  # implicit secret->binding dependency is gone; make it explicit.
  depends_on = [
    google_secret_manager_secret.validator_hotkey_mnemonic,
    google_secret_manager_secret.validator_openrouter_key,
    google_secret_manager_secret.validator_gh_token,
    google_secret_manager_secret.validator_wandb_key,
    google_secret_manager_secret.validator_pylon_identity_token,
    google_secret_manager_secret.validator_pylon_open_access_token,
  ]
}

# Stage every non-provider grant before attaching the dedicated identity.
resource "google_secret_manager_secret_iam_member" "validator_dedicated_access" {
  for_each  = toset(local.validator_secret_ids)
  project   = var.project
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.validator[0].email}"

  depends_on = [
    google_secret_manager_secret.validator_hotkey_mnemonic,
    google_secret_manager_secret.validator_gh_token,
    google_secret_manager_secret.validator_wandb_key,
    google_secret_manager_secret.validator_pylon_identity_token,
    google_secret_manager_secret.validator_pylon_open_access_token,
  ]
}

# The existing OpenRouter credential is platform-owned. Validators exchange a
# ticket-scoped capability and never receive Secret Manager access or the value.
resource "google_secret_manager_secret_iam_member" "platform_inference_access" {
  count     = local.validator_count
  project   = var.project
  secret_id = google_secret_manager_secret.validator_openrouter_key[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${local.platform_api_sa_email}"
}

output "validator_vm_name" {
  description = "Name of the validator VM (empty when enable_validator = false)."
  value       = var.enable_validator ? module.validator_vm[0].hostname : ""
}

output "validator_vm_internal_ip" {
  description = "Private IP of the validator VM (reachability is via IAP)."
  value       = var.enable_validator ? module.validator_vm[0].internal_ip : ""
}
