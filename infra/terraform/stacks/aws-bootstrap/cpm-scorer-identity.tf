###############################################################################
# CPM scorer instance identity (infra/terraform/stacks/aws-cpm-scorer).
#
# The role and its instance profile live here, applied by an SSO
# administrator, and not in the CI-applied aws-cpm-scorer root, for the same
# reason as the generator scorer's (generator-scorer-identity.tf): the
# scorer's Platform bearer will be handed to every session of this exact role
# name through AWS -> GCP Workload Identity Federation, so whoever can create
# the role can choose its trust policy. That federation is a follow-up change
# in gcp-platform; until it lands the role carries SSM registration and
# nothing else. The CI apply role may only read the role and profile and pass
# the role to EC2; main.tf NeverWriteCpmScorerIdentity denies it every IAM
# write on both, including delete-and-recreate.
#
# The aws-cpm-scorer root reads the profile by name and checks that it wraps
# this role (data "aws_iam_instance_profile" "scorer").
###############################################################################

variable "enable_cpm_scorer_identity" {
  description = "Create the CPM scorer's instance role and profile. Off by default so applying this root changes nothing for the CPM scorer."
  type        = bool
  default     = false
}

locals {
  cpm_scorer_identity_count = var.enable_cpm_scorer_identity ? 1 : 0
}

data "aws_iam_policy_document" "cpm_scorer_assume" {
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
# the operator's own session. Model weights come from public Hugging Face
# revisions, and the Platform bearer will come from GCP, so it reads nothing
# in AWS.
resource "aws_iam_role" "cpm_scorer" {
  count                = local.cpm_scorer_identity_count
  name                 = local.cpm_scorer_role_name
  description          = "SN118 CPM scorer GPU host: SSM registration only."
  assume_role_policy   = data.aws_iam_policy_document.cpm_scorer_assume.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "cpm_scorer_ssm" {
  count      = local.cpm_scorer_identity_count
  role       = aws_iam_role.cpm_scorer[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "cpm_scorer" {
  count = local.cpm_scorer_identity_count
  name  = local.cpm_scorer_role_name
  role  = aws_iam_role.cpm_scorer[0].name
}

output "cpm_scorer_role_name" {
  description = "CPM scorer instance role (a later GCP federation pins assumed-role/<this>/). Empty when disabled."
  value       = var.enable_cpm_scorer_identity ? aws_iam_role.cpm_scorer[0].name : ""
}

output "cpm_scorer_instance_profile_name" {
  description = "aws-cpm-scorer's instance_profile_name must equal it. Empty when disabled."
  value       = var.enable_cpm_scorer_identity ? aws_iam_instance_profile.cpm_scorer[0].name : ""
}
