terraform {
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.aws_account_id]

  # stack = aws-screener scopes the CI apply role's EC2 grants and is the tag
  # the capacity controller's wake role may start (aws-bootstrap/screener.tf).
  default_tags {
    tags = {
      system = "sn118"
      stack  = "aws-screener"
    }
  }
}
