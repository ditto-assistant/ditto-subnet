###############################################################################
# Trusted monorepo image-build and hosted DittoBench release identities.
#
# The Targon rental gets only one 30-minute Artifact Registry access token for
# ditto-image-builder. It never receives a GCP key, the controller identity, or
# Secret Manager authority. GitHub's release fallback is a separate prod-env
# WIF principal. The two public repositories are readable without credentials
# so Targon can pull the reviewed Kaniko executor and released screener image.
###############################################################################

resource "google_artifact_registry_repository" "public_builders" {
  project       = var.project
  location      = var.region
  repository_id = "ditto-public-builders"
  format        = "DOCKER"
  description   = "Public, reviewed builder images used by isolated Targon rentals."

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_artifact_registry_repository" "public_runtime" {
  project       = var.project
  location      = var.region
  repository_id = "ditto-public-runtime"
  format        = "DOCKER"
  description   = "Public, immutable Ditto subnet worker runtime images."

  docker_config {
    immutable_tags = true
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_artifact_registry_repository_iam_member" "public_builders_reader" {
  project    = var.project
  location   = var.region
  repository = google_artifact_registry_repository.public_builders.repository_id
  role       = "roles/artifactregistry.reader"
  member     = "allUsers"
}

resource "google_artifact_registry_repository_iam_member" "public_runtime_reader" {
  project    = var.project
  location   = var.region
  repository = google_artifact_registry_repository.public_runtime.repository_id
  role       = "roles/artifactregistry.reader"
  member     = "allUsers"
}

resource "google_service_account" "image_builder" {
  project      = var.project
  account_id   = "ditto-image-builder"
  display_name = "Ditto Trusted Image Builder"
}

resource "google_artifact_registry_repository_iam_member" "image_builder_runtime_writer" {
  project    = var.project
  location   = var.region
  repository = google_artifact_registry_repository.public_runtime.repository_id
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.image_builder.email}"
}

resource "google_service_account_iam_member" "screener_controller_mint_builder_tokens" {
  count              = local.screener_capacity_controller_count
  service_account_id = google_service_account.image_builder.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.screener_capacity_controller[0].email}"
}

resource "google_service_account" "subnet_build" {
  project      = var.project
  account_id   = "github-actions-subnet-build"
  display_name = "GitHub Actions Subnet Trusted Build"
}

resource "google_service_account_iam_member" "subnet_build_wif" {
  service_account_id = google_service_account.subnet_build.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.wif_pool_id}/subject/repo:ditto-assistant/ditto-subnet:environment:${var.platform_deploy_environment}"
}

resource "google_artifact_registry_repository_iam_member" "subnet_build_public_builders_writer" {
  project    = var.project
  location   = var.region
  repository = google_artifact_registry_repository.public_builders.repository_id
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.subnet_build.email}"
}

resource "google_artifact_registry_repository_iam_member" "subnet_build_public_runtime_writer" {
  project    = var.project
  location   = var.region
  repository = google_artifact_registry_repository.public_runtime.repository_id
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.subnet_build.email}"
}

resource "google_secret_manager_secret_iam_member" "subnet_build_controller_token_access" {
  count     = local.screener_capacity_controller_count
  project   = var.project
  secret_id = google_secret_manager_secret.screener_controller_api_token[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.subnet_build.email}"
}

# Hosted DittoBench deploys are no longer a side effect of its component CI.
# This prod-environment principal can write only its existing repository and
# update Cloud Run; actAs is scoped to the service's current runtime identity.
data "google_artifact_registry_repository" "dittobench_hosted" {
  project       = var.project
  location      = var.region
  repository_id = "cloud-run-source-deploy"
}

resource "google_service_account" "dittobench_deploy" {
  project      = var.project
  account_id   = "github-actions-dittobench"
  display_name = "GitHub Actions Hosted DittoBench Deploy"
}

resource "google_service_account_iam_member" "dittobench_deploy_wif" {
  service_account_id = google_service_account.dittobench_deploy.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.wif_pool_id}/subject/repo:ditto-assistant/ditto-subnet:environment:${var.platform_deploy_environment}"
}

resource "google_artifact_registry_repository_iam_member" "dittobench_deploy_writer" {
  project    = var.project
  location   = var.region
  repository = data.google_artifact_registry_repository.dittobench_hosted.repository_id
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.dittobench_deploy.email}"
}

resource "google_cloud_run_v2_service_iam_member" "dittobench_deploy_run_admin" {
  project  = var.project
  location = var.region
  name     = var.dittobench_cloud_run_service_name
  role     = "roles/run.admin"
  member   = "serviceAccount:${google_service_account.dittobench_deploy.email}"
}

resource "google_service_account_iam_member" "dittobench_deploy_actas_runtime" {
  service_account_id = "projects/${var.project}/serviceAccounts/${data.google_project.this.number}-compute@developer.gserviceaccount.com"
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.dittobench_deploy.email}"
}

output "subnet_build_sa_email" {
  description = "Set as ditto-subnet's GCP_SUBNET_BUILD_SA prod-environment secret."
  value       = google_service_account.subnet_build.email
}

output "image_builder_sa_email" {
  description = "Identity impersonated for 30-minute Targon registry tokens."
  value       = google_service_account.image_builder.email
}

output "dittobench_deploy_sa_email" {
  description = "Set as ditto-subnet's GCP_DITTOBENCH_DEPLOY_SA prod-environment secret."
  value       = google_service_account.dittobench_deploy.email
}
