# GCS backend. Auth is Application Default Credentials / WIF — `gcloud auth`
# locally or google-github-actions/auth in CI. No static keys. This env is
# GCS-native (never lived on Backblaze B2). See
# ../../../docs/tfstate-gcs-migration.md for the single-writer cutover.
terraform {
  backend "gcs" {
    bucket = "ditto-app-dev-tfstate"
    prefix = "gcp-platform"
  }
}
