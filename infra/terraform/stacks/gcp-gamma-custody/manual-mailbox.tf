# One-way authority: Platform can send bounded requests, custody can return
# public observations. Neither side gains the other's wallet/secret/IAM access.
variable "enable_manual_mailbox" {
  type    = bool
  default = false
}
variable "manual_mailbox_platform_service_account" {
  type    = string
  default = ""
  validation {
    condition     = var.manual_mailbox_platform_service_account == "" || var.manual_mailbox_platform_service_account == "ditto-platform-api@ditto-app-dev.iam.gserviceaccount.com"
    error_message = "Only the dedicated Platform API identity may publish requests."
  }
}
check "manual_mailbox_sealed" {
  assert {
    condition     = !var.enable_manual_mailbox || (var.enable_collector_custody && var.collector_custody_phases.transfer == "sealed" && var.manual_mailbox_platform_service_account != "")
    error_message = "Manual mailbox requires sealed transfer custody and an explicit Platform identity."
  }
}
resource "google_pubsub_topic" "manual" {
  for_each = var.enable_manual_mailbox ? toset(["requests", "reports"]) : toset([])
  project  = var.project
  name     = "sn118-manual-${each.key}"
  message_storage_policy { allowed_persistence_regions = [var.region] }
  lifecycle {
    precondition {
      condition     = var.enable_collector_custody && var.collector_custody_phases.transfer == "sealed" && var.manual_mailbox_platform_service_account != ""
      error_message = "Manual mailbox requires sealed transfer custody and an explicit Platform identity."
    }
  }
}
resource "google_pubsub_subscription" "manual" {
  for_each = google_pubsub_topic.manual
  project  = var.project
  name     = "sn118-manual-${each.key}"
  # Keep the exact fully-qualified name known in the first saved plan; the
  # scope fence must not accept an arbitrary unknown topic destination.
  topic                      = "projects/${var.project}/topics/sn118-manual-${each.key}"
  depends_on                 = [google_pubsub_topic.manual]
  ack_deadline_seconds       = 600
  message_retention_duration = "604800s"
  expiration_policy { ttl = "" }
  retry_policy {
    minimum_backoff = "15s"
    maximum_backoff = "300s"
  }
}
resource "google_pubsub_topic_iam_member" "manual_requests" {
  count   = var.enable_manual_mailbox ? 1 : 0
  project = var.project
  topic   = google_pubsub_topic.manual["requests"].name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${var.manual_mailbox_platform_service_account}"
}
resource "google_pubsub_subscription_iam_member" "manual_requests" {
  count        = var.enable_manual_mailbox ? 1 : 0
  project      = var.project
  subscription = google_pubsub_subscription.manual["requests"].name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${google_service_account.collector_delegate["transfer"].email}"
}
resource "google_pubsub_topic_iam_member" "manual_reports" {
  count   = var.enable_manual_mailbox ? 1 : 0
  project = var.project
  topic   = google_pubsub_topic.manual["reports"].name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.collector_delegate["transfer"].email}"
}
resource "google_pubsub_subscription_iam_member" "manual_reports" {
  count        = var.enable_manual_mailbox ? 1 : 0
  project      = var.project
  subscription = google_pubsub_subscription.manual["reports"].name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${var.manual_mailbox_platform_service_account}"
}
