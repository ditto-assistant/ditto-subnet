# The shared state holds ONLY project/billing association and the empty state
# bucket. It never holds verifier, bank, key, or provider-grant metadata.
terraform {
  backend "gcs" {
    bucket = "ditto-app-dev-tfstate"
    prefix = "gcp-v13-private-bootstrap"
  }
}
