mock_provider "google" {}
variables {
  project                  = "sn118-gamma-custody"
  region                   = "us-central1"
  zone                     = "us-central1-a"
  enable_collector_custody = true
  collector_custody_offline_addresses = [
    "5Fhnko5TCPtR1hyD8aua6n23XRM8cxcBM6TgNEqqUhcxWs1t",
    "5H93a8uVJC6ve2bNVZu9jAGEuBHDFspct3UbHRYGsxUNpA1e",
    "5HeA6bjjt1apDMhLSqKkCWZCEGvZBPByjvMxZ7mKiUk67AGT",
    "5CD8TZWNSNtARxmB5VQF9qTNfCVjhdMdjmwBrniqU3E71ohQ",
    "5F2821EzMcBC2b8hWmrMWiNRcx6DgdaTQQiqmPkbAk7a6yn9",
  ]
  collector_custody_revision = "0123456789012345678901234567890123456789"
  collector_custody_operator = "operator@example.com"
  collector_custody_phases   = { registration = "sealed", transfer = "sealed" }
}
run "mailbox_default_off" {
  command = plan
  assert {
    condition     = length(google_pubsub_topic.manual) == 0 && length(google_pubsub_subscription.manual) == 0
    error_message = "Default-off must confer no mailbox authority."
  }
}
run "explicit_mailbox_has_only_directional_grants" {
  command = plan
  variables {
    enable_manual_mailbox                   = true
    manual_mailbox_platform_service_account = "ditto-platform-api@ditto-app-dev.iam.gserviceaccount.com"
  }
  assert {
    condition     = length(google_pubsub_topic.manual) == 2 && length(google_pubsub_subscription.manual) == 2 && google_pubsub_topic_iam_member.manual_requests[0].role == "roles/pubsub.publisher" && google_pubsub_subscription_iam_member.manual_reports[0].role == "roles/pubsub.subscriber" && google_pubsub_topic_iam_member.manual_requests[0].member == "serviceAccount:ditto-platform-api@ditto-app-dev.iam.gserviceaccount.com" && google_pubsub_subscription_iam_member.manual_reports[0].member == google_pubsub_topic_iam_member.manual_requests[0].member
    error_message = "Platform must only publish requests and consume reports."
  }
  assert {
    condition     = google_pubsub_topic_iam_member.manual_reports[0].role == "roles/pubsub.publisher" && google_pubsub_subscription_iam_member.manual_requests[0].role == "roles/pubsub.subscriber" && google_pubsub_subscription.manual["requests"].ack_deadline_seconds == 600 && google_pubsub_subscription.manual["requests"].message_retention_duration == "604800s"
    error_message = "Custody result authority and bounded redelivery must stay separate."
  }
}
