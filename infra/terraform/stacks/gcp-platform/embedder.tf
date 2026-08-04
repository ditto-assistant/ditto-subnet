###############################################################################
# Code-embedding service (optional; gated by var.enable_embedder).
#
# The anti-copy code-embedding signal embeds each uploaded crate through a self-hosted
# text-embeddings-inference (TEI) service and compares vectors by cosine in the
# gate (ditto-platform api_server/embedding/). The workload is embed-once-per-
# upload, latency-tolerant, low-QPS, and the app VMs are small (e2-medium/4 GB)
# with no room to co-locate TEI, so it runs as an AUTHENTICATED, SCALE-TO-ZERO
# Cloud Run service:
#
#   - Private (allow_unauthenticated = false). The app VMs run as the
#     ditto-platform SA and invoke it with a Google-signed identity token minted
#     from the metadata server (the client's CODE_EMBEDDER_AUTH=gcp_id_token path).
#     No static secret, no ingress on the VM.
#   - min_instances = 0 → nothing runs (and nothing is billed) at idle. A cold
#     start loads the BAKED model from the image (docker/embedder in the
#     ditto-subnet repo) rather than pulling from the HF hub, so it is a few
#     seconds — absorbed by the client's raised CODE_EMBEDDER_TIMEOUT_SECONDS.
#
# OFF by default so a routine `terraform apply` doesn't create it. Provisioning
# is two-phase because Cloud Run needs the image to exist before it can pull it:
#
#   1. Create the Artifact Registry repo first:
#        terraform apply -var=enable_embedder=true \
#          -target=google_artifact_registry_repository.embedder
#   2. Build + push the image (ditto-subnet repo):
#        cd docker/embedder && gcloud builds submit --config cloudbuild.yaml \
#          --substitutions=_IMAGE=us-central1-docker.pkg.dev/ditto-app-dev/embedder/embedder:jina-v2,_MODEL_ID=jinaai/jina-embeddings-v2-base-code
#   3. Apply the rest (the Cloud Run service + IAM):
#        terraform apply -var=enable_embedder=true
#   4. Wire CODE_EMBEDDER_URL (the embedder_url output) into the platform_app role
#      and converge. See docs/embedder-deploy.md for the full runbook.
###############################################################################

variable "enable_embedder" {
  description = "Create the code-embedding Cloud Run service + its Artifact Registry repo. Off by default; two-phase apply (repo → push image → service). See docs/embedder-deploy.md."
  type        = bool
  default     = false
}

variable "embedder_image" {
  description = "Full Artifact Registry image ref (with tag) the Cloud Run service runs. Empty derives the default repo path + the `jina-v2` tag. Build/push it with the ditto-subnet repo's docker/embedder/cloudbuild.yaml before enabling the service."
  type        = string
  default     = ""
}

variable "embedder_cpu" {
  description = "CPU limit per embedder instance. jina-embeddings-v2-base-code (161M) is CPU-fast; 2 vCPU with startup boost keeps a single embed and the cold-start load responsive."
  type        = string
  default     = "2"
}

variable "embedder_memory" {
  description = "Memory limit per embedder instance. TEI/Candle CPU warmup materializes the full f32 attention over MAX_BATCH_TOKENS (8192² × heads ≈ 7Gi peak, dominated by sequence length not model size), so 8Gi. (Qwen3-0.6B needed 16Gi and truncated inputs — rejected for CPU serving.)"
  type        = string
  default     = "8Gi"
}

variable "embedder_max_instances" {
  description = "Scale ceiling for the embedder. Uploads are low-QPS, so a small ceiling is ample and bounds cost."
  type        = number
  default     = 3
}

locals {
  embedder_count   = var.enable_embedder ? 1 : 0
  embedder_repo_id = "embedder"

  # Default image path when var.embedder_image is empty. Region-scoped Artifact
  # Registry, repo `embedder`, image `embedder`, tag tied to the baked model.
  embedder_image = var.embedder_image != "" ? var.embedder_image : "${var.region}-docker.pkg.dev/${var.project}/${local.embedder_repo_id}/embedder:jina-v2"

  # Cloud Run pulls images as the serverless service agent (not the runtime SA);
  # grant it read on the repo so a cross-repo pull can't be denied.
  run_service_agent = "serviceAccount:service-${data.google_project.this.number}@serverless-robot-prod.iam.gserviceaccount.com"
}

# --- Artifact Registry repo holding the baked embedder image(s). ---
resource "google_artifact_registry_repository" "embedder" {
  count         = local.embedder_count
  project       = var.project
  location      = var.region
  repository_id = local.embedder_repo_id
  format        = "DOCKER"
  description   = "Code-embedding (TEI) images for the Cloud Run embedder."
}

resource "google_artifact_registry_repository_iam_member" "embedder_reader" {
  count      = local.embedder_count
  project    = var.project
  location   = var.region
  repository = google_artifact_registry_repository.embedder[0].repository_id
  role       = "roles/artifactregistry.reader"
  member     = local.run_service_agent
}

# --- The Cloud Run service (private, scale-to-zero). ---
module "embedder" {
  source   = "../../modules/cloudrun"
  count    = local.embedder_count
  project  = var.project
  name     = "ditto-embedder-dev"
  location = var.region
  image    = local.embedder_image

  # Runs as the platform SA (needs no GCP permissions itself — it makes no GCP
  # calls; this just avoids the broad default compute SA).
  service_account_email = local.run_sa_email

  container_port    = 8080 # matches docker/embedder/Dockerfile
  cpu               = var.embedder_cpu
  memory            = var.embedder_memory
  min_instances     = 0 # scale to zero
  max_instances     = var.embedder_max_instances
  concurrency       = 8  # CPU embedding; keep per-instance queueing modest
  timeout_seconds   = 60 # request timeout; also covers a cold model load
  cpu_idle          = true
  startup_cpu_boost = true

  # TEI reads its CLI flags from env too, so we tune here without rebuilding the
  # image. Warmup materializes the full f32 attention over MAX_BATCH_TOKENS, and
  # that cost is quadratic in the token count — the default 16384 (and even 8192)
  # OOMs an 8Gi/2-vCPU instance before it listens. Cap it to 4096 (~1/4 the
  # attention, comfortably under 8Gi); inputs longer than 4096 tokens
  # (~14k chars) truncate deterministically, which is fine for a best-effort
  # near-duplicate signal (both sides of a comparison truncate identically).
  env = {
    MAX_BATCH_TOKENS = "4096"
    AUTO_TRUNCATE    = "true"
  }

  # PRIVATE: only the app SA may invoke (binding below). Ingress stays open
  # because the app VMs call the public run.app URL over the internet; IAM — not
  # network reachability — is the gate.
  allow_unauthenticated = false
  ingress               = "INGRESS_TRAFFIC_ALL"

  labels = { env = "dev", role = "embedder", managed = "terraform" }

  depends_on = [google_artifact_registry_repository.embedder]
}

# The app VMs (running as ditto-platform) invoke the embedder with a metadata
# ID token; grant that SA run.invoker on the service.
resource "google_cloud_run_v2_service_iam_member" "embedder_invoker" {
  count    = local.embedder_count
  project  = var.project
  location = var.region
  name     = module.embedder[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.run_sa_email}"
}

# Grant the dedicated platform API identity before moving either VM off the
# shared runtime service account. Retain the legacy member for bounded rollback.
resource "google_cloud_run_v2_service_iam_member" "platform_api_embedder_invoker" {
  count    = local.embedder_count
  project  = var.project
  location = var.region
  name     = module.embedder[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.platform_api_sa_email}"
}

output "embedder_url" {
  description = "Cloud Run URL of the code-embedding embedder (empty when enable_embedder = false). Feeds CODE_EMBEDDER_URL in the platform_app role (as PLATFORM_EMBEDDER_URL)."
  value       = var.enable_embedder ? module.embedder[0].uri : ""
}
