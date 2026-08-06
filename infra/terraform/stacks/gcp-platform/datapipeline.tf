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
# is two-phase because Cloud Run needs a bootstrap image before semantic release
# can take ownership of subsequent image deploys:
#
#   1. Create the Artifact Registry repo first:
#        terraform apply -var=enable_datapipeline=true \
#          -target=google_artifact_registry_repository.datapipeline
#   2. Apply the rest (the Cloud Run service + release IAM) with the audited
#      bootstrap digest below:
#        terraform apply -var=enable_datapipeline=true
#   3. Merge a conventional datagen PR. Semantic release tags the exact release,
#      verifies it, publishes the image, deploys its immutable digest, and
#      smoke-tests the selected benchmark contract before shifting traffic.
#   4. Wire DATA_PIPELINE_URL (the datapipeline_url output) into the
#      platform_app role (as PLATFORM_DATAPIPELINE_URL) and converge.
###############################################################################

variable "enable_datapipeline" {
  description = "Create the dataset-generation Cloud Run service + its Artifact Registry repo. Off by default; two-phase apply (repo → push image → service)."
  type        = bool
  default     = false
}

variable "datapipeline_image" {
  description = "Bootstrap-only digest-pinned image used when Cloud Run is first created. Semantic release owns every subsequent image deployment; Terraform ignores image drift."
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

  # This is a creation bootstrap, not the desired ongoing version. The default
  # is the immutable v0.13.2 multi-platform index that was live-verified to serve
  # bench_version=8 on 2026-08-06. The service lifecycle below deliberately
  # ignores image changes after creation, so a later Terraform apply can never
  # roll back a semantic-release deployment.
  datapipeline_image = var.datapipeline_image != "" ? var.datapipeline_image : "${var.region}-docker.pkg.dev/${var.project}/${local.datapipeline_repo_id}/generate@sha256:321c264198641de74583d566a16d863d95837233a116acb5500db8ab4d412796"
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

# Semantic release publishes and deploys immutable datagen images through a
# dedicated environment-scoped WIF identity. It cannot mutate any host.
resource "google_artifact_registry_repository_iam_member" "datapipeline_release_writer" {
  count      = local.datapipeline_count
  project    = var.project
  location   = var.region
  repository = google_artifact_registry_repository.datapipeline[0].repository_id
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.datagen_release.email}"
}

# Permit the release identity to update only this Cloud Run service. Developer
# can create revisions but cannot alter the service IAM policy; actAs remains
# separately scoped to the one runtime identity below.
resource "google_cloud_run_v2_service_iam_member" "datapipeline_release_developer" {
  count    = local.datapipeline_count
  project  = var.project
  location = var.region
  name     = google_cloud_run_v2_service.datapipeline[0].name
  role     = "roles/run.developer"
  member   = "serviceAccount:${google_service_account.datagen_release.email}"
}

resource "google_service_account_iam_member" "datapipeline_release_actas_runtime" {
  count              = local.datapipeline_count
  service_account_id = google_service_account.ditto_platform.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.datagen_release.email}"
}

# --- The Cloud Run service (private, scale-to-zero). ---
#
# This resource is intentionally local rather than the generic Cloud Run module:
# Terraform owns the service shape, while semantic release owns the container
# image. A lifecycle rule on the generic module would also hide image drift for
# services whose images Terraform still owns.
resource "google_cloud_run_v2_service" "datapipeline" {
  count               = local.datapipeline_count
  project             = var.project
  name                = "ditto-datapipeline"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  labels              = { role = "datapipeline", managed = "terraform" }
  deletion_protection = false

  template {
    # Runs as the platform SA (needs no GCP permissions itself — it makes no GCP
    # calls; this just avoids the broad default compute SA).
    service_account                  = local.run_sa_email
    timeout                          = "60s"
    max_instance_request_concurrency = 4

    scaling {
      min_instance_count = 0
      max_instance_count = var.datapipeline_max_instances
    }

    containers {
      image = local.datapipeline_image

      ports {
        container_port = 8090
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  lifecycle {
    ignore_changes = [template[0].containers[0].image]
  }

  depends_on = [google_artifact_registry_repository.datapipeline]
}

# Preserve the existing Cloud Run service while changing only which layer owns
# its image version. This is a state address move, not a destroy/create.
moved {
  from = module.datapipeline[0].google_cloud_run_v2_service.this
  to   = google_cloud_run_v2_service.datapipeline[0]
}

# The app VMs (running as ditto-platform) invoke the generator with a metadata
# ID token; grant that SA run.invoker on the service.
resource "google_cloud_run_v2_service_iam_member" "datapipeline_invoker" {
  count    = local.datapipeline_count
  project  = var.project
  location = var.region
  name     = google_cloud_run_v2_service.datapipeline[0].name
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
  name     = google_cloud_run_v2_service.datapipeline[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.platform_api_sa_email}"
}

output "datapipeline_url" {
  description = "Cloud Run URL of the dataset generator (empty when enable_datapipeline = false). Feeds DATA_PIPELINE_URL in the platform_app role (as PLATFORM_DATAPIPELINE_URL)."
  value       = var.enable_datapipeline ? google_cloud_run_v2_service.datapipeline[0].uri : ""
}
