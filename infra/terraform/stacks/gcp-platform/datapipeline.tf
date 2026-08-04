###############################################################################
# Dataset-generation service (optional; gated by var.enable_datapipeline).
#
# The per-submission dataset generator (public dittobench-datagen repo,
# cmd/generate-service). The platform calls POST /generate?seed=&run_size= once per
# submission at screening pass and pins (seed, dataset_sha256, run_size) plus
# the on-chain seed block, which is what turns the dashboard's provenance rows
# from "random fallback / no reproduction command" into on-chain-derived,
# reproducible pins. Generation is deterministic and non-LLM (a static Go
# binary), so the workload is generate-once-per-submission, low-QPS, and fast;
# it runs as an AUTHENTICATED, SCALE-TO-ZERO Cloud Run service exactly like the
# embedder:
#
#   - Private (allow_unauthenticated = false). The app VMs run as the
#     ditto-platform SA and invoke it with a Google-signed identity token minted
#     from the metadata server (the client's DATA_PIPELINE_AUTH=gcp_id_token
#     path). No static secret, no ingress on the VM. Keeping it private means
#     nobody can use platform infrastructure as a generation oracle; the
#     unpredictability guarantee itself comes from the post-commit seed, and the
#     generator code is the public dittobench-datagen module.
#   - min_instances = 0 → nothing runs (and nothing is billed) at idle. The
#     image is a static Go binary, so a cold start is well under a second.
#
# OFF by default so a routine `terraform apply` doesn't create it. Provisioning
# is two-phase because Cloud Run needs the image to exist before it can pull it:
#
#   1. Create the Artifact Registry repo first:
#        terraform apply -var=enable_datapipeline=true \
#          -target=google_artifact_registry_repository.datapipeline
#   2. Merge a conventional datagen PR. Semantic-release tags the exact release,
#      verifies it, publishes the image, and prints its immutable digest.
#   3. Apply the rest (the Cloud Run service + IAM):
#        terraform apply -var=enable_datapipeline=true
#   4. Wire DATA_PIPELINE_URL (the datapipeline_url output) into the
#      platform_app role (as PLATFORM_DATAPIPELINE_URL) and converge.
###############################################################################

variable "enable_datapipeline" {
  description = "Create the dataset-generation Cloud Run service + its Artifact Registry repo. Off by default; two-phase apply (repo → push image → service)."
  type        = bool
  default     = false
}

variable "datapipeline_image" {
  description = "Full digest-pinned Artifact Registry image ref the Cloud Run service runs. Empty retains the audited v0.12.0/v7 default published and verified by semantic-release CI."
  type        = string
  default     = ""
}

variable "datapipeline_max_instances" {
  description = "Scale ceiling for the generator. One short call per submission at screening pass, so a small ceiling is ample and bounds cost."
  type        = number
  default     = 2
}

locals {
  datapipeline_count   = var.enable_datapipeline ? 1 : 0
  datapipeline_repo_id = "datapipeline"

  # Default image path when var.datapipeline_image is empty. Region-scoped
  # Artifact Registry, repo `datapipeline`, image `generate`, digest tied to the
  # pinned generator release. The v0.12.0 tag was built from immutable datagen
  # commit c168a2593abe8e2da1b72dd5f38493f1e86dc39e.
  #
  # Keep this digest current with the deployed generator, in the same change that
  # cuts the datagen release. This has drifted twice: it sat at v0.7.1, then
  # v0.8.0, while the live service ran a newer image deployed out of band. An
  # apply with the variable unset ROLLS THE GENERATOR BACK, and that does not
  # fail loudly -- the scorer fails runs whose regenerated dataset hash
  # mismatches the pin, so a skew surfaces as failing benchmark runs, not as an
  # infra error, and not near the apply that caused it.
  #
  # v0.12.0 preserves bench versions 2-6 and ships the v7 product-grounded
  # difficulty generator that dittobench-api's v7 strict scoring is calibrated
  # against (the api build at 001d3aa pins datagen v0.12.0 in go.mod). Keep the
  # digest, release tag, and source commit together so an apply cannot silently
  # select a mutable tag or an unverified generator build.
  #
  # This bump is a deliberate FORWARD upgrade, not a drift repair: at the time of
  # the change the live ditto-datapipeline service was verified to be serving the
  # previous v0.11.2 digest (7c798902), exactly matching the old pin. Note the
  # forward direction of the same hazard — v0.11.2 still ANSWERS bench_version=7,
  # so a lagging generator does not 502; it renders the pre-difficulty v7 bytes
  # while dittobench-api regenerates v0.12.0 bytes. Apply this BEFORE the bench
  # rollout is flipped to 7.
  datapipeline_image = var.datapipeline_image != "" ? var.datapipeline_image : "${var.region}-docker.pkg.dev/${var.project}/${local.datapipeline_repo_id}/generate@sha256:78f2a4da66b8ef8465ffe650cf8bfda0e60ec8dcd80cc6f36d824f604d99e132"
}

# --- Artifact Registry repo holding the generate-service image(s). ---
resource "google_artifact_registry_repository" "datapipeline" {
  count         = local.datapipeline_count
  project       = var.project
  location      = var.region
  repository_id = local.datapipeline_repo_id
  format        = "DOCKER"
  description   = "Per-submission dataset generator (dittobench-datagen cmd/generate-service) images for Cloud Run."

  docker_config {
    immutable_tags = true
  }
}

resource "google_artifact_registry_repository_iam_member" "datapipeline_reader" {
  count      = local.datapipeline_count
  project    = var.project
  location   = var.region
  repository = google_artifact_registry_repository.datapipeline[0].repository_id
  role       = "roles/artifactregistry.reader"
  member     = local.run_service_agent
}

# Semantic-release publishes immutable datagen images through a dedicated
# environment-scoped WIF identity. It cannot deploy Cloud Run or mutate hosts.
resource "google_artifact_registry_repository_iam_member" "datapipeline_release_writer" {
  count      = local.datapipeline_count
  project    = var.project
  location   = var.region
  repository = google_artifact_registry_repository.datapipeline[0].repository_id
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.datagen_release.email}"
}

# --- The Cloud Run service (private, scale-to-zero). ---
module "datapipeline" {
  source   = "../../modules/cloudrun"
  count    = local.datapipeline_count
  project  = var.project
  name     = "ditto-datapipeline"
  location = var.region
  image    = local.datapipeline_image

  # Runs as the platform SA (needs no GCP permissions itself — it makes no GCP
  # calls; this just avoids the broad default compute SA).
  service_account_email = local.run_sa_email

  container_port    = 8090 # cmd/generate-service reads PORT (default 8090)
  cpu               = "1"
  memory            = "512Mi"
  min_instances     = 0 # scale to zero
  max_instances     = var.datapipeline_max_instances
  concurrency       = 4  # deterministic CPU generation; keep per-instance queueing modest
  timeout_seconds   = 60 # covers a full-profile generation with margin (client default is 30s)
  cpu_idle          = true
  startup_cpu_boost = true

  # PRIVATE: only the app SA may invoke (binding below). Ingress stays open
  # because the app VMs call the public run.app URL over the internet; IAM — not
  # network reachability — is the gate.
  allow_unauthenticated = false
  ingress               = "INGRESS_TRAFFIC_ALL"

  labels = { role = "datapipeline", managed = "terraform" }

  depends_on = [google_artifact_registry_repository.datapipeline]
}

# The app VMs (running as ditto-platform) invoke the generator with a metadata
# ID token; grant that SA run.invoker on the service.
resource "google_cloud_run_v2_service_iam_member" "datapipeline_invoker" {
  count    = local.datapipeline_count
  project  = var.project
  location = var.region
  name     = module.datapipeline[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.run_sa_email}"
}

# Keep the legacy runtime binding during the additive identity transition, and
# authorize the dedicated API identity before either app VM is attached to it.
# This prevents the service-account cutover from breaking deterministic dataset
# generation while prod can still roll back to the shared runtime identity.
resource "google_cloud_run_v2_service_iam_member" "platform_api_datapipeline_invoker" {
  count    = local.datapipeline_count
  project  = var.project
  location = var.region
  name     = module.datapipeline[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.platform_api_sa_email}"
}

output "datapipeline_url" {
  description = "Cloud Run URL of the dataset generator (empty when enable_datapipeline = false). Feeds DATA_PIPELINE_URL in the platform_app role (as PLATFORM_DATAPIPELINE_URL)."
  value       = var.enable_datapipeline ? module.datapipeline[0].uri : ""
}
