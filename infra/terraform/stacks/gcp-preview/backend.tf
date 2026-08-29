terraform {
  backend "gcs" {
    bucket = "ditto-app-dev-tfstate"
    prefix = "gcp-preview"
  }
}
