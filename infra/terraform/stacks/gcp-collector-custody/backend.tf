# Custody has never been applied. This new state owns custody only; no state
# migration, forgetting, targeted apply or Platform backend reuse is permitted.
terraform {
  backend "gcs" {
    bucket = "ditto-app-dev-tfstate"
    prefix = "gcp-collector-custody"
  }
}
