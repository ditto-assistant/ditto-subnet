###############################################################################
# One-time AWS bootstrap for the protected infrastructure workflow.
#
# Owns the Terraform state bucket (imported after create-state-bucket.sh), the
# GitHub Actions OIDC provider, and two roles that infra-plan-apply.yml
# assumes: a plan role (read state, refresh managed resources, take the lock)
# and an apply role (scoped mutation of the aws-generator-scorer and
# aws-screener roots). It also owns the generator scorer's instance identity
# (generator-scorer-identity.tf) and the on-demand screener's two identities
# (screener.tf), so no CI root can change which roles GCP trusts or who may
# start the screener. There are no long-lived AWS keys. Apply locally as an SSO
# administrator; this root is deliberately not selectable in the workflow,
# since it creates the identity that authorizes the workflow itself.
###############################################################################

terraform {
  required_version = ">= 1.10.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

locals {
  account_id   = "339713011188"
  region       = "us-east-1"
  state_bucket = "ditto-subnet-tfstate-${local.account_id}"
  repository   = "ditto-assistant/ditto-subnet"

  # Exact GitHub environment subjects, matching the GCP WIF conditions the
  # same workflow already uses (gcp-gamma-custody-bootstrap).
  plan_subject  = "repo:${local.repository}:environment:infra-plan"
  apply_subject = "repo:${local.repository}:environment:infra-apply"

  # Roots this workflow may plan/apply in AWS. Each name is the root's
  # directory, its state prefix, and the `stack` default tag its provider puts
  # on every resource it creates; the CI roles' state and EC2 grants are keyed
  # on these names and nothing else.
  ci_roots = ["aws-generator-scorer", "aws-screener"]

  scorer_role_name  = "ditto-generator-scorer"
  scorer_ssm_bucket = "ditto-generator-scorer-ssm-${local.account_id}"

  # Created by this root (screener.tf), only referenced by aws-screener.
  screener_node_role_name = "ditto-screener-node"
  screener_wake_role_name = "ditto-screener-capacity-wake"
  screener_stack_tag      = "aws-screener"
}

provider "aws" {
  region              = local.region
  allowed_account_ids = [local.account_id]

  default_tags {
    tags = {
      system = "sn118"
      stack  = "aws-bootstrap"
    }
  }
}

# --- state bucket -----------------------------------------------------------

import {
  to = aws_s3_bucket.state
  id = local.state_bucket
}

import {
  to = aws_s3_bucket_ownership_controls.state
  id = local.state_bucket
}

import {
  to = aws_s3_bucket_public_access_block.state
  id = local.state_bucket
}

import {
  to = aws_s3_bucket_versioning.state
  id = local.state_bucket
}

import {
  to = aws_s3_bucket_server_side_encryption_configuration.state
  id = local.state_bucket
}

resource "aws_s3_bucket" "state" {
  bucket = local.state_bucket
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_ownership_controls" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Old state versions are kept for recovery but not forever.
resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    id     = "expire-noncurrent-state"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
  depends_on = [aws_s3_bucket_versioning.state]
}

data "aws_iam_policy_document" "state_tls" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.state.arn,
      "${aws_s3_bucket.state.arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "state" {
  bucket     = aws_s3_bucket.state.id
  policy     = data.aws_iam_policy_document.state_tls.json
  depends_on = [aws_s3_bucket_public_access_block.state]
}

# --- GitHub OIDC --------------------------------------------------------------

# IAM validates GitHub's token signing certificate through its own trust store,
# so no thumbprint is pinned here.
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_policy_document" "github_assume" {
  for_each = {
    plan  = local.plan_subject
    apply = local.apply_subject
  }
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [each.value]
    }
  }
}

resource "aws_iam_role" "github_plan" {
  name                 = "ditto-subnet-infra-plan"
  description          = "infra-plan-apply.yml plan job (GitHub environment infra-plan): read state and managed resources, take the state lock."
  assume_role_policy   = data.aws_iam_policy_document.github_assume["plan"].json
  max_session_duration = 3600
}

resource "aws_iam_role" "github_apply" {
  name                 = "ditto-subnet-infra-apply"
  description          = "infra-plan-apply.yml apply job (GitHub environment infra-apply, required reviewers): scoped mutation of AWS roots."
  assume_role_policy   = data.aws_iam_policy_document.github_assume["apply"].json
  max_session_duration = 3600
}

# --- shared permission fragments ----------------------------------------------

locals {
  state_keys = [for root in local.ci_roots : "${aws_s3_bucket.state.arn}/${root}/terraform.tfstate"]
  lock_keys  = [for key in local.state_keys : "${key}.tflock"]
  scorer_iam = ["arn:aws:iam::${local.account_id}:role/${local.scorer_role_name}", "arn:aws:iam::${local.account_id}:instance-profile/${local.scorer_role_name}"]
  # Roles whose trust decides who GCP federates or who may start the screener.
  federated_role_arns = [
    "arn:aws:iam::${local.account_id}:role/${local.scorer_role_name}",
    "arn:aws:iam::${local.account_id}:role/${local.screener_node_role_name}",
    "arn:aws:iam::${local.account_id}:role/${local.screener_wake_role_name}",
  ]
  # The screener node identity: read and pass only, never written by CI.
  screener_node_iam = [
    "arn:aws:iam::${local.account_id}:role/${local.screener_node_role_name}",
    "arn:aws:iam::${local.account_id}:instance-profile/${local.screener_node_role_name}",
  ]
  public_amis = [
    "arn:aws:ssm:${local.region}::parameter/aws/service/debian/*",
    "arn:aws:ssm:${local.region}::parameter/aws/service/canonical/*",
  ]

  # Each CI root's provider default_tags put stack = <root> on every resource
  # it creates; the apply role's EC2 grants are keyed on exactly these values.
  ci_stack_tags = local.ci_roots
  ec2_arn       = "arn:aws:ec2:${local.region}:${local.account_id}"
  # EC2 create call -> resource types it must tag with a stack tag.
  tag_on_create = {
    CreateVpc                    = ["vpc"]
    CreateDhcpOptions            = ["dhcp-options"]
    CreateSubnet                 = ["subnet"]
    CreateInternetGateway        = ["internet-gateway"]
    CreateRouteTable             = ["route-table"]
    CreateSecurityGroup          = ["security-group"]
    AuthorizeSecurityGroupEgress = ["security-group-rule"]
    RunInstances                 = ["instance", "volume"]
  }
}

data "aws_iam_policy_document" "plan" {
  statement {
    sid       = "ListRootState"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.state.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = [for root in local.ci_roots : "${root}/*"]
    }
  }
  statement {
    sid       = "ReadRootState"
    actions   = ["s3:GetObject"]
    resources = local.state_keys
  }
  statement {
    sid       = "HoldRootLocks"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = local.lock_keys
  }
  statement {
    sid       = "DescribeEc2"
    actions   = ["ec2:Describe*"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [local.region]
    }
  }
  statement {
    sid = "ReadHostIdentities"
    actions = [
      "iam:GetRole",
      "iam:GetInstanceProfile",
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
      "iam:ListInstanceProfilesForRole",
    ]
    resources = concat(local.scorer_iam, local.screener_node_iam)
  }
  statement {
    sid       = "ReadScorerTransferBucket"
    actions   = ["s3:Get*", "s3:ListBucket"]
    resources = ["arn:aws:s3:::${local.scorer_ssm_bucket}"]
  }
  statement {
    sid       = "ResolvePublicAmis"
    actions   = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = local.public_amis
  }
}

data "aws_iam_policy_document" "apply" {
  # Everything the plan role reads, plus the state object itself.
  source_policy_documents = [data.aws_iam_policy_document.plan.json]

  statement {
    sid       = "WriteRootState"
    actions   = ["s3:PutObject"]
    resources = local.state_keys
  }

  # EC2 mutation is scoped by the `stack` default tag each CI root puts on
  # everything it creates (versions.tf default_tags). A resource is created
  # only when the request carries one of those tags, and an existing resource
  # is changed only when it already carries one, so the role cannot terminate,
  # modify, open or delete unrelated instances, VPCs or security groups in
  # us-east-1. No statement grants ingress: neither design has any.
  dynamic "statement" {
    for_each = local.tag_on_create
    content {
      sid       = "CreateTagged${statement.key}"
      actions   = ["ec2:${statement.key}"]
      resources = [for type in statement.value : "${local.ec2_arn}:${type}/*"]
      condition {
        test     = "StringEquals"
        variable = "aws:RequestTag/stack"
        values   = local.ci_stack_tags
      }
    }
  }

  # Tag-on-create needs CreateTags, limited to those create calls.
  statement {
    sid       = "TagOnCreate"
    actions   = ["ec2:CreateTags"]
    resources = ["${local.ec2_arn}:*/*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:CreateAction"
      values   = keys(local.tag_on_create)
    }
  }

  # Everything that touches an existing resource, including the parent VPC,
  # subnet and security group a create or launch names, requires the tag.
  statement {
    sid = "MutateTaggedStackResources"
    actions = [
      "ec2:CreateSubnet", "ec2:CreateRouteTable", "ec2:CreateSecurityGroup",
      "ec2:RunInstances", "ec2:AuthorizeSecurityGroupEgress",
      "ec2:DeleteVpc", "ec2:ModifyVpcAttribute",
      "ec2:AssociateDhcpOptions", "ec2:DeleteDhcpOptions",
      "ec2:DeleteSubnet", "ec2:ModifySubnetAttribute",
      "ec2:DeleteInternetGateway", "ec2:AttachInternetGateway", "ec2:DetachInternetGateway",
      "ec2:DeleteRouteTable", "ec2:CreateRoute", "ec2:DeleteRoute", "ec2:ReplaceRoute",
      "ec2:AssociateRouteTable", "ec2:DisassociateRouteTable",
      "ec2:DeleteSecurityGroup", "ec2:RevokeSecurityGroupEgress", "ec2:RevokeSecurityGroupIngress",
      "ec2:ModifySecurityGroupRules", "ec2:UpdateSecurityGroupRuleDescriptionsEgress",
      "ec2:CreateTags", "ec2:DeleteTags",
      "ec2:TerminateInstances", "ec2:StartInstances", "ec2:StopInstances",
      "ec2:ModifyInstanceAttribute", "ec2:ModifyInstanceMetadataOptions", "ec2:ModifyInstanceCreditSpecification",
      "ec2:AssociateIamInstanceProfile", "ec2:ReplaceIamInstanceProfileAssociation", "ec2:DisassociateIamInstanceProfile",
      "ec2:ModifyVolume",
    ]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/stack"
      values   = local.ci_stack_tags
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [local.region]
    }
  }

  # RunInstances also authorizes the image and the network interface it
  # creates. Images come only from the publishers modules/compute/aws resolves
  # through SSM: Debian (136693071363) and Canonical (099720109477).
  # ec2:Owner can be the alias "amazon" even when DescribeImages returns a
  # numeric publisher ID. ResourceAccount compares the actual owning account
  # and preserves this two-publisher boundary without admitting all Amazon AMIs.
  statement {
    sid       = "LaunchPublishedImages"
    actions   = ["ec2:RunInstances"]
    resources = ["arn:aws:ec2:${local.region}::image/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceAccount"
      values   = ["136693071363", "099720109477"]
    }
  }
  # The primary interface RunInstances creates is untagged when IAM evaluates
  # the launch, so this grant cannot carry a stack-tag condition, and an
  # aws:ResourceTag...IfExists condition would match every untagged ENI in the
  # region anyway. Residual: a launch request may name an existing, unattached
  # ENI anywhere in us-east-1 as the new instance's interface (bringing that
  # ENI's subnet and security groups). That instance must still be created
  # with a CI stack tag, from a Debian or Canonical image, with at most a host
  # role (PassHostRolesToEc2), and becomes a tagged resource this role can
  # terminate. This grant reaches no ENI API other than RunInstances, so it
  # cannot modify, detach or delete an ENI. Neither CI root names an existing
  # ENI.
  statement {
    sid       = "LaunchNetworkInterface"
    actions   = ["ec2:RunInstances"]
    resources = ["${local.ec2_arn}:network-interface/*"]
  }

  # A new security group's default allow-all egress rule is created by AWS
  # untagged; Terraform revokes it. Revoke also authorizes the group itself,
  # which must carry a stack tag (MutateTaggedStackResources). The role can
  # put that tag on a security group only while creating it (TagOnCreate);
  # nothing lets it tag an existing group into scope, so these revokes reach
  # only groups this role created. No grant here covers ingress.
  statement {
    sid       = "RevokeUntaggedDefaultRules"
    actions   = ["ec2:RevokeSecurityGroupEgress", "ec2:RevokeSecurityGroupIngress"]
    resources = ["${local.ec2_arn}:security-group-rule/*"]
  }

  # Every host identity is created by this root as an administrator
  # (generator-scorer-identity.tf, screener.tf). GCP federation trusts every
  # session of the scorer and screener node role names, and the wake role's
  # trust decides which Google identity may start the screener, so CI must not
  # be able to choose any of their trust policies, either by rewriting one or
  # by deleting and recreating the role. The apply role only reads the host
  # roles and profiles (ReadHostIdentities) and passes the two host roles to
  # EC2; every IAM write on any of them is denied explicitly, so a later
  # widening elsewhere in this policy cannot restore one.
  statement {
    sid       = "NeverRetrustFederatedRoles"
    effect    = "Deny"
    actions   = ["iam:UpdateAssumeRolePolicy"]
    resources = local.federated_role_arns
  }

  # The screener identities belong to this root alone. Nothing here allows
  # writing them; the explicit deny keeps a later widening from doing so.
  statement {
    sid    = "NeverManageScreenerIdentity"
    effect = "Deny"
    actions = [
      "iam:Create*", "iam:Delete*", "iam:Put*", "iam:Attach*", "iam:Detach*",
      "iam:Update*", "iam:Tag*", "iam:Untag*",
      "iam:AddRoleToInstanceProfile", "iam:RemoveRoleFromInstanceProfile",
      "iam:SetDefaultPolicyVersion",
    ]
    resources = concat(local.screener_node_iam, [
      "arn:aws:iam::${local.account_id}:role/${local.screener_wake_role_name}",
    ])
  }

  # Each host role may be passed to EC2 only. The wake role is never passable.
  statement {
    sid     = "PassHostRolesToEc2"
    actions = ["iam:PassRole"]
    resources = [
      "arn:aws:iam::${local.account_id}:role/${local.scorer_role_name}",
      "arn:aws:iam::${local.account_id}:role/${local.screener_node_role_name}",
    ]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ec2.amazonaws.com"]
    }
  }

  # The scorer identity: no IAM write on the role or profile, so CI can
  # neither retrust it nor delete and recreate it with a trust of its choosing.
  statement {
    sid    = "NeverWriteScorerIdentity"
    effect = "Deny"
    actions = [
      "iam:CreateRole", "iam:DeleteRole", "iam:UpdateRole", "iam:UpdateRoleDescription",
      "iam:UpdateAssumeRolePolicy", "iam:TagRole", "iam:UntagRole",
      "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:AttachRolePolicy", "iam:DetachRolePolicy",
      "iam:PutRolePermissionsBoundary", "iam:DeleteRolePermissionsBoundary",
      "iam:CreateInstanceProfile", "iam:DeleteInstanceProfile",
      "iam:AddRoleToInstanceProfile", "iam:RemoveRoleFromInstanceProfile",
      "iam:TagInstanceProfile", "iam:UntagInstanceProfile",
    ]
    resources = local.scorer_iam
  }

  statement {
    sid     = "ManageScorerTransferBucket"
    actions = ["s3:*"]
    resources = [
      "arn:aws:s3:::${local.scorer_ssm_bucket}",
      "arn:aws:s3:::${local.scorer_ssm_bucket}/*",
    ]
  }
}

resource "aws_iam_role_policy" "github_plan" {
  name   = "terraform-plan"
  role   = aws_iam_role.github_plan.id
  policy = data.aws_iam_policy_document.plan.json
}

resource "aws_iam_role_policy" "github_apply" {
  name   = "terraform-apply"
  role   = aws_iam_role.github_apply.id
  policy = data.aws_iam_policy_document.apply.json
}

output "state_bucket" {
  description = "Backend bucket for every AWS root in this repository."
  value       = aws_s3_bucket.state.bucket
}

output "github_plan_role_arn" {
  description = "Assumed by the infra-plan job; referenced literally in infra-plan-apply.yml."
  value       = aws_iam_role.github_plan.arn
}

output "github_apply_role_arn" {
  description = "Assumed by the infra-apply job; referenced literally in infra-plan-apply.yml."
  value       = aws_iam_role.github_apply.arn
}
