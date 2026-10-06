terraform {
  backend "gcs" {
    bucket = "ditto-subnet-recovery-tfstate"
    prefix = "gcp-subnet-recovery"
  }
}
