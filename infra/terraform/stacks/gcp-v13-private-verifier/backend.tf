# Separate state prefix. Values and bank objects are never written through
# Terraform. A protected apply identity is required before any creation.
terraform {
  backend "gcs" {
    bucket = "ditto-app-dev-tfstate"
    prefix = "gcp-v13-private-verifier"
  }
}
