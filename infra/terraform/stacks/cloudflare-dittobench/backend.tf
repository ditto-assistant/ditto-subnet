terraform {
  backend "gcs" {
    bucket = "ditto-app-dev-tfstate"
    prefix = "cloudflare-dittobench-ai"
  }
}
