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
      name      = "ditto-screener-node"
      role_name = "ditto-screener-node"
    }
  }
}

variables {
  # Tests choose their enablement explicitly, independent of production intent.
  enable_aws_screener             = false
  screener_fleet_release_sha      = "0123456789abcdef0123456789abcdef01234567"
  screener_fleet_builder_image    = "ghcr.io/ditto-assistant/submission-builder@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  screener_fleet_aws_wif_provider = "projects/123456789012/locations/global/workloadIdentityPools/screener-aws/providers/aws-ec2"
  # Unset by default; the reviewed tfvars sets it with a supporting release.
  screener_fleet_idle_exit_seconds = 900
}

run "explicitly_disabled_creates_nothing" {
  command = plan
  variables {
    screener_fleet_release_sha      = ""
    screener_fleet_builder_image    = ""
    screener_fleet_aws_wif_provider = ""
  }

  assert {
    condition = (
      length(aws_vpc.screener) + length(module.screener) + length(data.aws_iam_instance_profile.node) +
      length(aws_vpc_security_group_egress_rule.screener)
    ) == 0
    error_message = "The disabled root must create nothing."
  }
  assert {
    condition     = output.screener_instance_id == "" && output.screener_vpc_id == ""
    error_message = "Disabled outputs are empty."
  }
}

run "enabled_screener_shape" {
  command = plan
  variables {
    enable_aws_screener = true
  }

  assert {
    condition     = module.screener[0].hostname == "subnet-screener-3"
    error_message = "The instance Name is the Platform node id."
  }
  assert {
    condition     = toset(keys(aws_vpc_security_group_egress_rule.screener)) == toset(["80", "443"])
    error_message = "Egress is HTTP/HTTPS only."
  }
  assert {
    condition     = aws_vpc.screener[0].cidr_block == "10.118.1.0/24"
    error_message = "The screener VPC does not overlap the generator scorer's."
  }
  assert {
    condition     = !aws_subnet.screener[0].map_public_ip_on_launch
    error_message = "Only the screener instance asks for a public address."
  }
  assert {
    condition     = output.screener_node_id == "subnet-screener-3"
    error_message = "The node id output follows the variable."
  }
}

run "enabling_requires_release_inputs" {
  command = plan
  variables {
    enable_aws_screener        = true
    screener_fleet_release_sha = ""
  }
  expect_failures = [aws_vpc.screener]
}

run "disabled_needs_no_idle_exit_setting" {
  command = plan
  variables {
    screener_fleet_idle_exit_seconds = null
  }

  assert {
    condition     = length(module.screener) == 0
    error_message = "With the root disabled, idle exit may stay unset."
  }
}

run "enabling_requires_an_explicit_idle_exit" {
  command = plan
  variables {
    enable_aws_screener              = true
    screener_fleet_idle_exit_seconds = null
  }
  expect_failures = [aws_vpc.screener]
}

run "rejects_unpinned_builder_image" {
  command = plan
  variables {
    screener_fleet_builder_image = "ghcr.io/ditto-assistant/submission-builder:latest"
  }
  expect_failures = [var.screener_fleet_builder_image]
}

run "rejects_foreign_instance_profile" {
  command = plan
  variables {
    enable_aws_screener = true
  }
  override_data {
    target = data.aws_iam_instance_profile.node[0]
    values = {
      name      = "ditto-screener-node"
      role_name = "ditto-generator-scorer"
    }
  }
  expect_failures = [data.aws_iam_instance_profile.node[0]]
}
