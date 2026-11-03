# Payload is added directly to Secret Manager, never Terraform state or GitHub.
resource "google_secret_manager_secret" "github_link_client_secret" {
  project   = var.project
  secret_id = "platform-github-link-client-secret"
  replication {
    auto {}
  }
  lifecycle {
    prevent_destroy = true
  }
}

# Match the identity attached to the Platform app VMs; do not grant CI access.
resource "google_secret_manager_secret_iam_member" "github_link_platform_access" {
  project   = var.project
  secret_id = google_secret_manager_secret.github_link_client_secret.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.platform_dedicated_identity_attached ? local.platform_api_sa_email : local.run_sa_email}"
}
