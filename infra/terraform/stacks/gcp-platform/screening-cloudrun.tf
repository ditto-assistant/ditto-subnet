###############################################################################
# Cloud Run screening compute (Targon-first GCP fallback).
#
# Kaniko compile remains a Cloud Run Job. Runtime smoke is a short-lived
# internal Service, while L1/L2/L3 can use one private warm Service. The
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

variable "enable_screening_source_review_service" {
  description = "Create the private warm source-review Cloud Run service. Enable only with an immutable released screener image."
  type        = bool
  default     = false
}

variable "screening_source_review_image" {
  description = "Immutable screener image digest used to bootstrap the warm source-review service; semantic release owns later image updates."
  type        = string
  default     = ""
}

resource "google_cloud_run_v2_service" "screening_source_review" {
  count               = var.enable_screening_source_review_service ? 1 : 0
  project             = var.project
  name                = "ditto-source-review-warm"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  labels              = { role = "screening-source-review", managed = "terraform" }
  deletion_protection = false

  template {
    service_account                  = google_service_account.screening_untrusted.email
    timeout                          = "3600s"
    max_instance_request_concurrency = 1

    scaling {
      min_instance_count = 1
      max_instance_count = 2
    }

    containers {
      image   = var.screening_source_review_image
      command = ["/app/workers/screener/.venv/bin/python", "-m"]
      args    = ["ditto_screener.source_review_service"]

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "2"
          memory = "4Gi"
        }
        cpu_idle          = false
        startup_cpu_boost = true
      }

      startup_probe {
        initial_delay_seconds = 0
        timeout_seconds       = 3
        period_seconds        = 3
        failure_threshold     = 20

        http_get {
          path = "/health"
          port = 8080
        }
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  lifecycle {
    ignore_changes = [template[0].containers[0].image]

    precondition {
      condition = can(regex(
        "^us-central1-docker\\.pkg\\.dev/ditto-app-dev/ditto-public-runtime/screener@sha256:[0-9a-f]{64}$",
        var.screening_source_review_image,
      ))
      error_message = "screening_source_review_image must be the immutable production screener digest"
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "screening_source_review_invoker" {
  count    = var.enable_screening_source_review_service ? 1 : 0
  project  = var.project
  location = var.region
  name     = google_cloud_run_v2_service.screening_source_review[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.platform_api_sa_email}"
}

resource "google_cloud_run_v2_service_iam_member" "screening_source_review_release" {
  count    = var.enable_screening_source_review_service ? 1 : 0
  project  = var.project
  location = var.region
  name     = google_cloud_run_v2_service.screening_source_review[0].name
  role     = "roles/run.developer"
  member   = "serviceAccount:${google_service_account.subnet_build.email}"
}

resource "google_service_account_iam_member" "subnet_build_actas_screening_review" {
  count              = var.enable_screening_source_review_service ? 1 : 0
  service_account_id = google_service_account.screening_untrusted.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.subnet_build.email}"
}

output "screening_source_review_service_name" {
  description = "Private warm source-review service name (empty until explicitly enabled)."
  value       = var.enable_screening_source_review_service ? google_cloud_run_v2_service.screening_source_review[0].name : ""
}
