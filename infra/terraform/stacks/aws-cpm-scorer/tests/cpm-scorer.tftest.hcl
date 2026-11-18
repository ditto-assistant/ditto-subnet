mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "339713011188"
    }
  }
  mock_data "aws_ssm_parameter" {
    defaults = {
      value = "ami-0123456789abcdef0"
    }
  }
  mock_data "aws_iam_instance_profile" {
    defaults = {
      name      = "ditto-cpm-scorer"
      role_name = "ditto-cpm-scorer"
    }
  }
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{}"
    }
  }
}

run "merging_creates_nothing" {
  command = plan

  assert {
    condition = (
      length(aws_vpc.scorer) + length(aws_internet_gateway.scorer) + length(aws_subnet.scorer) +
      length(aws_route_table.scorer) + length(aws_route.scorer_egress) + length(aws_security_group.scorer) +
      length(aws_vpc_security_group_egress_rule.scorer) + length(data.aws_iam_instance_profile.scorer) +
      length(aws_s3_bucket.ssm_transfer) + length(module.cpm_scorer)
    ) == 0
    error_message = "The root must create nothing until enable_cpm_scorer is set."
  }
  assert {
    condition = (
      output.cpm_scorer_instance_id == "" && output.cpm_scorer_role_name == "" &&
      output.cpm_scorer_ssm_transfer_bucket == ""
    )
    error_message = "Disabled outputs are empty."
  }
}

run "enabled_scorer_shape" {
  command = plan
  variables {
    enable_cpm_scorer = true
  }

  assert {
    condition     = module.cpm_scorer[0].hostname == "ditto-cpm-scorer-prod"
    error_message = "The host's Name tag is its inventory hostname."
  }
  assert {
    condition     = toset(keys(aws_vpc_security_group_egress_rule.scorer)) == toset(["80", "443"])
    error_message = "Egress is HTTP/HTTPS only."
  }
  assert {
    condition = alltrue([
      for rule in aws_vpc_security_group_egress_rule.scorer :
      rule.ip_protocol == "tcp" && rule.from_port == rule.to_port && contains([80, 443], rule.from_port)
    ])
    error_message = "Each egress rule opens exactly one of TCP 80 or 443."
  }
  assert {
    condition     = !aws_subnet.scorer[0].map_public_ip_on_launch
    error_message = "Only the scorer instance asks for a public address."
  }
  assert {
    condition     = aws_vpc.scorer[0].cidr_block == "10.118.2.0/24" && aws_subnet.scorer[0].availability_zone == "us-east-1a"
    error_message = "The CPM VPC does not overlap the generator scorer's or the screener's, and defaults to us-east-1a."
  }
  assert {
    condition     = data.aws_iam_instance_profile.scorer[0].name == "ditto-cpm-scorer"
    error_message = "The root reads the bootstrap-created profile by name; it never creates one."
  }
  assert {
    condition     = output.cpm_scorer_role_name == "ditto-cpm-scorer"
    error_message = "The profile wraps the bootstrap's CPM scorer role."
  }
  assert {
    condition     = aws_s3_bucket.ssm_transfer[0].bucket == "ditto-cpm-scorer-ssm-339713011188"
    error_message = "The transfer bucket name is account-scoped and matches the CI grant."
  }
  assert {
    condition = (
      strcontains(local.user_data, "snap remove --purge amazon-ssm-agent") &&
      strcontains(local.user_data, "sha256sum --check --strict") &&
      strcontains(local.user_data, "d0330f570309ad3468c75f5b12da95d394385680de820f1f2628506244a4ab6d") &&
      strcontains(local.user_data, "version='3.3.5538.0'") &&
      strcontains(local.user_data, "https://s3.us-east-1.amazonaws.com/amazon-ssm-us-east-1/") &&
      strcontains(local.user_data, "/debian_amd64/amazon-ssm-agent.deb")
    )
    error_message = "First boot replaces the snap agent with the pinned, hash-checked release."
  }
  assert {
    condition     = length(regexall("(?m)^[^#\\n]*(?i:nvidia|cuda|docker)", local.user_data)) == 0
    error_message = "User data installs only the SSM agent; the GPU runtime belongs to Ansible."
  }
}

run "rejects_a_profile_for_another_role" {
  command = plan
  variables {
    enable_cpm_scorer = true
  }

  override_data {
    target = data.aws_iam_instance_profile.scorer[0]
    values = {
      role_name = "ditto-generator-scorer"
    }
  }

  expect_failures = [data.aws_iam_instance_profile.scorer]
}

run "rejects_a_small_root_volume" {
  command = plan
  variables {
    enable_cpm_scorer = true
    boot_disk_gb      = 100
  }

  expect_failures = [var.boot_disk_gb]
}

# The instance the root asks for, planned through the shared module with the
# root's exact inputs: the module's resources are not addressable from a root
# run, so this run tests the module directly.
run "host_is_hardened" {
  command = plan

  module {
    source = "../../modules/compute/aws"
  }

  variables {
    name                  = "ditto-cpm-scorer-prod"
    size                  = "gpu-l40s"
    image                 = "ubuntu-24"
    location              = "us-east-1a"
    subnetwork            = "subnet-0123456789abcdef0"
    firewall_id           = "sg-0123456789abcdef0"
    assign_public_ip      = true
    boot_disk_gb          = 200
    instance_profile_name = "ditto-cpm-scorer"
    deletion_protection   = true
    metadata_hop_limit    = 1
    labels = {
      env     = "prod"
      role    = "cpm-scorer"
      managed = "terraform"
    }
  }

  assert {
    condition     = aws_instance.this.instance_type == "g6e.xlarge"
    error_message = "gpu-l40s maps to g6e.xlarge (one L40S 48 GB)."
  }
  assert {
    condition     = aws_instance.this.metadata_options[0].http_tokens == "required" && aws_instance.this.metadata_options[0].http_put_response_hop_limit == 1
    error_message = "IMDSv2 only, unreachable from containers."
  }
  assert {
    condition     = aws_instance.this.root_block_device[0].encrypted && aws_instance.this.root_block_device[0].volume_size >= 200
    error_message = "The root volume is encrypted and holds the model cache."
  }
  assert {
    condition     = aws_instance.this.disable_api_termination
    error_message = "Termination protection is on."
  }
  assert {
    condition     = aws_instance.this.vpc_security_group_ids == toset(["sg-0123456789abcdef0"])
    error_message = "Only the egress-only group is attached."
  }
  assert {
    condition     = length(aws_instance.this.credit_specification) == 0
    error_message = "The GPU class is not burstable."
  }
  assert {
    condition     = aws_instance.this.tags["role"] == "cpm-scorer"
    error_message = "The role tag places the host in role_cpm_scorer."
  }
}
