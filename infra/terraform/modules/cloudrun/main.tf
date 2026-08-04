data "google_project" "current" {
  project_id = var.project == "" ? null : var.project
}

resource "google_cloud_run_v2_service" "this" {
  project             = var.project == "" ? null : var.project
  name                = var.name
  location            = var.location
  ingress             = var.ingress
  labels              = var.labels
  deletion_protection = var.deletion_protection

  template {
    service_account                  = var.service_account_email == "" ? null : var.service_account_email
    timeout                          = "${var.timeout_seconds}s"
    max_instance_request_concurrency = var.concurrency

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    # Direct VPC egress (no connector) — the path to the private Postgres VM.
    dynamic "vpc_access" {
      for_each = var.vpc_network == "" ? [] : [1]
      content {
        egress = var.vpc_egress
        network_interfaces {
          network    = var.vpc_network
          subnetwork = var.vpc_subnetwork
          tags       = var.vpc_tags
        }
      }
    }

    containers {
      image = var.image

      ports {
        container_port = var.container_port
      }

      resources {
        limits = {
          cpu    = var.cpu
          memory = var.memory
        }
        cpu_idle          = var.cpu_idle
        startup_cpu_boost = var.startup_cpu_boost
      }

      # Plain config env.
      dynamic "env" {
        for_each = var.env
        content {
          name  = env.key
          value = env.value
        }
      }

      # Secret Manager-backed env (e.g. DB password, API keys).
      dynamic "env" {
        for_each = var.secret_env
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value.secret
              version = env.value.version
            }
          }
        }
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }
}

# Public invocation (Cloudflare fronts it, but the service itself must be
# invokable). Skip to keep the service private/IAM-gated.
resource "google_cloud_run_v2_service_iam_member" "invoker" {
  count    = var.allow_unauthenticated ? 1 : 0
  project  = var.project == "" ? null : var.project
  location = var.location
  name     = google_cloud_run_v2_service.this.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# Cloud Run domain mapping — Google-managed cert, no load balancer cost. The
# Cloudflare CNAME (→ ghs.googlehosted.com) is created by the DNS module using
# this resource's status records (see output domain_dns_records).
resource "google_cloud_run_domain_mapping" "this" {
  count    = var.domain == "" ? 0 : 1
  project  = var.project == "" ? null : var.project
  location = var.location
  name     = var.domain

  metadata {
    namespace = data.google_project.current.project_id
  }

  spec {
    route_name = google_cloud_run_v2_service.this.name
  }
}
