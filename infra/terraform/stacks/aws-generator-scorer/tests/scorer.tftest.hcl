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
      length(aws_vpc.scorer) + length(aws_iam_role.scorer) + length(aws_s3_bucket.ssm_transfer) +
      length(module.generator_scorer) + length(aws_vpc_security_group_egress_rule.scorer)
    ) == 0
    error_message = "The root must create nothing until enable_generator_scorer is set."
  }
  assert {
    condition     = output.generator_scorer_instance_id == ""
    error_message = "Disabled outputs are empty."
  }
}

run "enabled_scorer_shape" {
  command = plan
  variables {
    enable_generator_scorer = true
  }

  assert {
    condition     = module.generator_scorer[0].hostname == "ditto-generator-scorer-prod"
    error_message = "The AWS scorer keeps the GCE scorer's logical name."
  }
  assert {
    condition     = toset(keys(aws_vpc_security_group_egress_rule.scorer)) == toset(["80", "443"])
    error_message = "Egress is HTTP/HTTPS only."
  }
  assert {
    condition     = aws_iam_role.scorer[0].name == "ditto-generator-scorer"
    error_message = "The GCP attribute condition names this exact role."
  }
  assert {
    condition     = !aws_subnet.scorer[0].map_public_ip_on_launch
    error_message = "Only the scorer instance asks for a public address."
  }
  assert {
    condition     = aws_s3_bucket.ssm_transfer[0].bucket == "ditto-generator-scorer-ssm-339713011188"
    error_message = "The transfer bucket name is account-scoped."
  }
}
