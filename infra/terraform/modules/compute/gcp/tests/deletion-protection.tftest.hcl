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
