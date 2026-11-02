mock_provider "google" {}

variables {
  project  = "synthetic-recovery-project"
  name     = "synthetic-retirement-host"
  size     = "validator-prod"
  image    = "debian-13"
  location = "us-central1-a"
}

run "protected_by_default" {
  command = plan
  assert {
    condition     = google_compute_instance.this.deletion_protection
    error_message = "Existing callers must retain GCE deletion protection."
  }
}

run "explicit_retirement_preparation" {
  command = plan
  variables {
    deletion_protection = false
  }
  assert {
    condition     = !google_compute_instance.this.deletion_protection
    error_message = "Only an explicit override may prepare a reviewed retirement."
  }
}

run "running_by_default" {
  command = plan
  assert {
    condition     = google_compute_instance.this.desired_status == "RUNNING"
    error_message = "Existing callers must keep their VMs running."
  }
}

run "explicit_stop" {
  command = plan
  variables {
    desired_status = "TERMINATED"
  }
  assert {
    condition     = google_compute_instance.this.desired_status == "TERMINATED"
    error_message = "An explicit TERMINATED must stop the VM."
  }
}

run "rejects_unknown_status" {
  command = plan
  variables {
    desired_status = "SUSPENDED"
  }
  expect_failures = [var.desired_status]
}
