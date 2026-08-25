###############################################################################
# Cloud Run screening compute (Targon-first GCP fallback).
#
# Kaniko compile and L1 source review are Cloud Run Jobs. Runtime smoke is a
# short-lived internal Cloud Run Service so Platform can GET /health. The
# untrusted runtime identity has no project roles beyond log write. Jobs and
# services are created per attempt by Platform; Terraform owns identity and IAM.
###############################################################################

resource "google_service_account" "screening_untrusted" {
  project      = var.project
  account_id   = "ditto-screening-untrusted"
  display_name = "Ditto untrusted screening compute"
}

resource "google_project_iam_custom_role" "screening_cloudrun_operator" {
  project     = var.project
  role_id     = "dittoScreeningCloudRunOperator"
  title       = "Ditto screening Cloud Run operator"
  description = "Platform API creates ephemeral screening Jobs and internal smoke Services."
  permissions = [
    "run.jobs.create",
    "run.jobs.get",
    "run.jobs.delete",
    "run.jobs.run",
    "run.jobs.update",
    "run.executions.get",
    "run.executions.delete",
    "run.services.create",
    "run.services.get",
    "run.services.delete",
    "run.services.update",
    "run.services.setIamPolicy",
    "run.routes.invoke",
  ]
}

resource "google_project_iam_member" "platform_api_screening_cloudrun_operator" {
  project = var.project
  role    = google_project_iam_custom_role.screening_cloudrun_operator.id
  member  = "serviceAccount:${local.platform_api_sa_email}"
}

resource "google_service_account_iam_member" "platform_api_actas_screening_untrusted" {
  service_account_id = google_service_account.screening_untrusted.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${local.platform_api_sa_email}"
}

resource "google_project_iam_member" "screening_untrusted_log_writer" {
  project = var.project
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.screening_untrusted.email}"
}

resource "google_artifact_registry_repository_iam_member" "cloudrun_agent_candidates_reader" {
  project    = var.project
  location   = var.region
  repository = google_artifact_registry_repository.screening_candidates.repository_id
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:service-${data.google_project.this.number}@serverless-robot-prod.iam.gserviceaccount.com"
}

# Cloud Run create_service checks that the caller can download the image, not
# only that the Cloud Run robot can pull it later.
resource "google_artifact_registry_repository_iam_member" "platform_api_candidates_reader" {
  project    = var.project
  location   = var.region
  repository = google_artifact_registry_repository.screening_candidates.repository_id
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${local.platform_api_sa_email}"
}

output "screening_untrusted_sa_email" {
  description = "Runtime identity for untrusted Cloud Run screening Jobs and smoke Services."
  value       = google_service_account.screening_untrusted.email
}
