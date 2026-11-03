terraform {
  backend "gcs" {
    bucket = "sn118-gamma-custody-tfstate"
    prefix = "gcp-gamma-custody"
  }
}
