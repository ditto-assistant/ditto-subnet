# S3 backend with native S3 locking (use_lockfile writes
# aws-generator-scorer/terraform.tfstate.tflock beside the state). Auth is the
# GitHub OIDC role assumed by infra-plan-apply.yml, or an operator's SSO
# session locally. No static keys. The bucket and those roles are created once
# by ../aws-bootstrap.
terraform {
  backend "s3" {
    bucket       = "ditto-subnet-tfstate-339713011188"
    key          = "aws-generator-scorer/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}
