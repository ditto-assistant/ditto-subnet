# The bucket is created by ./create-state-bucket.sh before the first init and
# then imported below, so this root manages the bucket that holds its own
# state. Run it locally as an SSO administrator; it is not a workflow root.
terraform {
  backend "s3" {
    bucket       = "ditto-subnet-tfstate-339713011188"
    key          = "aws-bootstrap/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}
