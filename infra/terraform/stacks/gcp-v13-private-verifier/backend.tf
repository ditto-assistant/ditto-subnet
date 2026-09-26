# This bucket is created in the dedicated project by the bootstrap root.
# Never initialize this root against the shared ditto-app-dev state bucket.
terraform {
  backend "gcs" {
    bucket = "ditto-v13-private-verifier-tfstate"
    prefix = "verifier"
  }
}
