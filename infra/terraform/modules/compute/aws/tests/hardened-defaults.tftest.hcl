mock_provider "aws" {
  mock_data "aws_ssm_parameter" {
    defaults = {
      value = "ami-0123456789abcdef0"
    }
  }
}

variables {
  name       = "synthetic-scorer"
  size       = "app-small"
  image      = "debian-13"
  location   = "us-east-1a"
  subnetwork = "subnet-0123456789abcdef0"
  labels = {
    role    = "generator-scorer"
    managed = "terraform"
  }
}

run "hardened_defaults" {
  command = plan

  assert {
    condition     = aws_instance.this.instance_type == "t3.medium"
    error_message = "app-small must map to the x86 t3.medium class."
  }
  assert {
    condition     = aws_instance.this.metadata_options[0].http_tokens == "required"
    error_message = "IMDSv2 must be required."
  }
  assert {
    condition     = aws_instance.this.metadata_options[0].http_put_response_hop_limit == 1
    error_message = "Containers must not reach instance credentials."
  }
  assert {
    condition     = aws_instance.this.root_block_device[0].encrypted
    error_message = "The root volume must be encrypted."
  }
  assert {
    condition     = aws_instance.this.root_block_device[0].volume_type == "gp3"
    error_message = "The root volume defaults to gp3."
  }
  assert {
    condition     = aws_instance.this.disable_api_termination
    error_message = "Termination protection is on by default."
  }
  assert {
    condition     = !aws_instance.this.associate_public_ip_address
    error_message = "No public IP unless requested."
  }
  assert {
    condition     = aws_instance.this.tags["Name"] == "synthetic-scorer" && aws_instance.this.tags["managed"] == "terraform"
    error_message = "Labels become tags and the name becomes the Name tag."
  }
  assert {
    condition     = aws_instance.this.credit_specification[0].cpu_credits == "standard"
    error_message = "Burstable classes default to standard credits."
  }
}

run "exact_ami_passthrough" {
  command = plan
  variables {
    image = "ami-0fedcba9876543210"
    size  = "app-standard"
  }

  assert {
    condition     = aws_instance.this.ami == "ami-0fedcba9876543210"
    error_message = "An exact ami- ID must pass through."
  }
  assert {
    condition     = length(aws_instance.this.credit_specification) == 0
    error_message = "Non-burstable classes carry no credit specification."
  }
}

run "unsupported_size_fails" {
  command = plan
  variables {
    size = "graviton-small"
  }
  expect_failures = [aws_instance.this]
}

run "every_gcp_size_exists_on_aws" {
  command = plan

  assert {
    condition = alltrue([
      for size in ["prod-64gb", "staging-32gb", "db-prod", "db-staging", "app-small", "controller-small", "app-standard", "validator", "validator-prod", "screener-burst", "screener-burst-n2d"] :
      contains(keys(local.instance_type_map), size)
    ])
    error_message = "Every logical size in modules/compute/gcp must exist on AWS."
  }
}
