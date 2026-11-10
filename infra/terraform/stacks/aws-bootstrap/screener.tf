###############################################################################
# On-demand AWS screener identities (infra/terraform/stacks/aws-screener).
#
# Both live here, applied by an SSO administrator, and not in the CI-applied
# aws-screener root. The CI apply role can launch the instance with the node
# profile but can never create, retrust, or re-permission either role
# (main.tf NeverRetrustFederatedRoles, NeverManageScreenerIdentity), so a
# protected-workflow run cannot change which instances GCP trusts with the
# review keys or which Google identity may start the instance.
#
# - ditto-screener-node: the instance role. It carries only SSM registration
#   (operator break-glass). GCP maps sessions of this exact role name to a
#   principal set that reads the review keys and the registration grant
#   (gcp-platform/screener-aws.tf).
# - ditto-screener-capacity-wake: assumed with a Google ID token by the GCE
#   capacity controller's service account. It may describe instances and start
#   only aws-screener-tagged ones. It never stops or terminates anything; the
#   host powers itself off when idle.
###############################################################################

variable "enable_aws_screener" {
  description = "Create the on-demand screener's instance role and profile. Off by default so applying this root changes nothing for the screener."
  type        = bool
  default     = false
}

variable "screener_capacity_controller_unique_id" {
  description = "Numeric unique ID of the GCP capacity controller service account (gcp-platform output screener_capacity_controller_unique_id). Empty creates no wake role."
  type        = string
  default     = ""

  validation {
    condition     = var.screener_capacity_controller_unique_id == "" || can(regex("^[0-9]{10,30}$", var.screener_capacity_controller_unique_id))
    error_message = "screener_capacity_controller_unique_id must be empty or the service account's numeric unique ID (not its email)."
  }
}

variable "screener_capacity_wake_audience" {
  description = "Audience the controller requests for its Google ID token (the token's aud claim, AWS key accounts.google.com:oaud)."
  type        = string
  default     = "ditto-screener-capacity"

  validation {
    condition     = length(var.screener_capacity_wake_audience) > 0
    error_message = "screener_capacity_wake_audience must not be empty."
  }
}

locals {
  screener_node_count = var.enable_aws_screener ? 1 : 0
  screener_wake_count = var.screener_capacity_controller_unique_id == "" ? 0 : 1
}

# --- node instance role -------------------------------------------------------

data "aws_iam_policy_document" "screener_node_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "screener_node" {
  count                = local.screener_node_count
  name                 = local.screener_node_role_name
  description          = "On-demand AWS screener host: SSM registration only; federated to GCP for its review keys and registration grant."
  assume_role_policy   = data.aws_iam_policy_document.screener_node_assume.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "screener_node_ssm" {
  count      = local.screener_node_count
  role       = aws_iam_role.screener_node[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "screener_node" {
  count = local.screener_node_count
  name  = local.screener_node_role_name
  role  = aws_iam_role.screener_node[0].name
}

# --- capacity controller wake role ------------------------------------------

# For a Google ID token AWS sets accounts.google.com:aud from the token's azp
# claim when present (a GCE metadata-server token sets azp to the service
# account's unique ID), :oaud from the token's aud claim (the requested
# audience), and :sub from sub (the same unique ID). Pinning all three admits
# exactly one service account presenting exactly one audience.
data "aws_iam_policy_document" "screener_wake_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = ["accounts.google.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "accounts.google.com:aud"
      values   = [var.screener_capacity_controller_unique_id]
    }
    condition {
      test     = "StringEquals"
      variable = "accounts.google.com:sub"
      values   = [var.screener_capacity_controller_unique_id]
    }
    condition {
      test     = "StringEquals"
      variable = "accounts.google.com:oaud"
      values   = [var.screener_capacity_wake_audience]
    }
  }
}

data "aws_iam_policy_document" "screener_wake" {
  statement {
    sid       = "FindScreener"
    actions   = ["ec2:DescribeInstances"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [local.region]
    }
  }
  statement {
    sid       = "StartTaggedScreener"
    actions   = ["ec2:StartInstances"]
    resources = ["arn:aws:ec2:${local.region}:${local.account_id}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/stack"
      values   = [local.screener_stack_tag]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [local.region]
    }
  }
}

resource "aws_iam_role" "screener_wake" {
  count                = local.screener_wake_count
  name                 = local.screener_wake_role_name
  description          = "GCP screener capacity controller: describe instances and start the aws-screener-tagged instance. Never stops or terminates."
  assume_role_policy   = data.aws_iam_policy_document.screener_wake_assume.json
  max_session_duration = 3600

  lifecycle {
    precondition {
      condition     = var.enable_aws_screener
      error_message = "The wake role starts the on-demand screener; set enable_aws_screener first."
    }
  }
}

resource "aws_iam_role_policy" "screener_wake" {
  count  = local.screener_wake_count
  name   = "start-aws-screener"
  role   = aws_iam_role.screener_wake[0].id
  policy = data.aws_iam_policy_document.screener_wake.json
}

output "screener_node_role_name" {
  description = "Instance role GCP federates (gcp-platform screener-aws.tf pins assumed-role/<this>/). Empty when disabled."
  value       = var.enable_aws_screener ? aws_iam_role.screener_node[0].name : ""
}

output "screener_node_instance_profile_name" {
  description = "aws-screener's instance_profile_name must equal it. Empty when disabled."
  value       = var.enable_aws_screener ? aws_iam_instance_profile.screener_node[0].name : ""
}

output "screener_capacity_wake_role_arn" {
  description = "Role the capacity controller assumes with its Google ID token. Empty when disabled."
  value       = local.screener_wake_count == 1 ? aws_iam_role.screener_wake[0].arn : ""
}
