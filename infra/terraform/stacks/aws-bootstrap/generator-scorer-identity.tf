###############################################################################
# Generator scorer instance identity (infra/terraform/stacks/aws-generator-scorer).
#
# The role and its instance profile live here, applied by an SSO
# administrator, and not in the CI-applied aws-generator-scorer root. GCP
# federation (gcp-platform/generator-scorer.tf) hands the scorer's Platform
# bearer to every session of this exact role name, so whoever can create the
# role can choose its trust policy. The CI apply role may only read the role
# and profile and pass the role to EC2; main.tf NeverWriteScorerIdentity denies
# it every IAM write on both, including delete-and-recreate.
#
# The aws-generator-scorer root reads the profile by name and checks that it
# wraps this role (data "aws_iam_instance_profile" "scorer").
###############################################################################

variable "enable_generator_scorer_identity" {
  description = "Create the generator scorer's instance role and profile. Off by default so applying this root changes nothing for the scorer."
  type        = bool
  default     = false
}

locals {
  scorer_identity_count = var.enable_generator_scorer_identity ? 1 : 0
}

data "aws_iam_policy_document" "scorer_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

# The instance role carries only SSM registration. It needs no S3 grant for
# Ansible: the aws_ssm connection moves files through presigned URLs minted by
# the operator's own session. In GCP it maps to a principal set that can read
# exactly generator-scorer-token-prod.
resource "aws_iam_role" "scorer" {
  count                = local.scorer_identity_count
  name                 = local.scorer_role_name
  description          = "Generator competition scorer host: SSM registration; federated to GCP for its one bearer."
  assume_role_policy   = data.aws_iam_policy_document.scorer_assume.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "scorer_ssm" {
  count      = local.scorer_identity_count
  role       = aws_iam_role.scorer[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "scorer" {
  count = local.scorer_identity_count
  name  = local.scorer_role_name
  role  = aws_iam_role.scorer[0].name
}

output "generator_scorer_role_name" {
  description = "Instance role GCP federates (gcp-platform generator_scorer_aws_role_name). Empty when disabled."
  value       = var.enable_generator_scorer_identity ? aws_iam_role.scorer[0].name : ""
}

output "generator_scorer_instance_profile_name" {
  description = "aws-generator-scorer's instance_profile_name must equal it. Empty when disabled."
  value       = var.enable_generator_scorer_identity ? aws_iam_instance_profile.scorer[0].name : ""
}
