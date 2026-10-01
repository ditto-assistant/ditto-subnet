# Credentials-free fixture. The QA driver copies actual production custody
# resources/template into this directory; no duplicate implementation.
terraform {
  required_version = ">= 1.10.0"
  required_providers {
    google = { source = "hashicorp/google", version = "6.50.0" }
  }
}
variable "project" { default = "test-project" }
variable "region" { default = "us-central1" }
variable "zone" { default = "us-central1-a" }
variable "enable_treasury_host" { default = false }
