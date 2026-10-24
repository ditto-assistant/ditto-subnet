###############################################################################
# One-time AWS bootstrap for the protected infrastructure workflow.
#
# Owns the Terraform state bucket (imported after create-state-bucket.sh), the
# GitHub Actions OIDC provider, and two roles that infra-plan-apply.yml
# assumes: a plan role (read state, refresh managed resources, take the lock)
# and an apply role (scoped mutation of the aws-generator-scorer root). There
# are no long-lived AWS keys. Apply locally as an SSO administrator; this root
# is deliberately not selectable in the workflow, since it creates the
# identity that authorizes the workflow itself.
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

  # Roots this workflow may plan/apply in AWS, keyed by state prefix.
  scorer_state_prefix = "aws-generator-scorer"
  scorer_role_name    = "ditto-generator-scorer"
  scorer_ssm_bucket   = "ditto-generator-scorer-ssm-${local.account_id}"
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
    bucket_key_enabled = true
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
  state_key  = "${aws_s3_bucket.state.arn}/${local.scorer_state_prefix}/terraform.tfstate"
  lock_key   = "${local.state_key}.tflock"
  scorer_iam = ["arn:aws:iam::${local.account_id}:role/${local.scorer_role_name}", "arn:aws:iam::${local.account_id}:instance-profile/${local.scorer_role_name}"]
  public_amis = [
    "arn:aws:ssm:${local.region}::parameter/aws/service/debian/*",
    "arn:aws:ssm:${local.region}::parameter/aws/service/canonical/*",
  ]
}

data "aws_iam_policy_document" "plan" {
  statement {
    sid       = "ListScorerState"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.state.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${local.scorer_state_prefix}/*"]
    }
  }
  statement {
    sid       = "ReadScorerState"
    actions   = ["s3:GetObject"]
    resources = [local.state_key]
  }
  statement {
    sid       = "HoldScorerLock"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = [local.lock_key]
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
    sid = "ReadScorerIdentity"
    actions = [
      "iam:GetRole",
      "iam:GetInstanceProfile",
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
      "iam:ListInstanceProfilesForRole",
    ]
    resources = local.scorer_iam
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
    sid       = "WriteScorerState"
    actions   = ["s3:PutObject"]
    resources = [local.state_key]
  }

  # Region-scoped EC2 mutation for the scorer VPC and instance. EC2's
  # resource-level permissions cannot express "only this root's resources"
  # without tag gymnastics on RunInstances' many resource types; the exact
  # saved plan and the infra-apply reviewers are the boundary.
  statement {
    sid = "MutateScorerNetworkAndHost"
    actions = [
      "ec2:CreateVpc", "ec2:DeleteVpc", "ec2:ModifyVpcAttribute",
      "ec2:CreateSubnet", "ec2:DeleteSubnet", "ec2:ModifySubnetAttribute",
      "ec2:CreateInternetGateway", "ec2:DeleteInternetGateway",
      "ec2:AttachInternetGateway", "ec2:DetachInternetGateway",
      "ec2:CreateRouteTable", "ec2:DeleteRouteTable", "ec2:CreateRoute", "ec2:DeleteRoute", "ec2:ReplaceRoute",
      "ec2:AssociateRouteTable", "ec2:DisassociateRouteTable",
      "ec2:CreateSecurityGroup", "ec2:DeleteSecurityGroup",
      "ec2:AuthorizeSecurityGroupEgress", "ec2:RevokeSecurityGroupEgress",
      "ec2:AuthorizeSecurityGroupIngress", "ec2:RevokeSecurityGroupIngress",
      "ec2:ModifySecurityGroupRules",
      "ec2:UpdateSecurityGroupRuleDescriptionsEgress", "ec2:UpdateSecurityGroupRuleDescriptionsIngress",
      "ec2:CreateTags", "ec2:DeleteTags",
      "ec2:RunInstances", "ec2:TerminateInstances", "ec2:StartInstances", "ec2:StopInstances",
      "ec2:ModifyInstanceAttribute", "ec2:ModifyInstanceMetadataOptions", "ec2:ModifyInstanceCreditSpecification",
      "ec2:AssociateIamInstanceProfile", "ec2:ReplaceIamInstanceProfileAssociation", "ec2:DisassociateIamInstanceProfile",
      "ec2:ModifyVolume",
    ]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [local.region]
    }
  }

  statement {
    sid = "ManageScorerIdentity"
    actions = [
      "iam:CreateRole", "iam:DeleteRole", "iam:UpdateRole", "iam:UpdateRoleDescription", "iam:UpdateAssumeRolePolicy",
      "iam:TagRole", "iam:UntagRole",
      "iam:CreateInstanceProfile", "iam:DeleteInstanceProfile",
      "iam:AddRoleToInstanceProfile", "iam:RemoveRoleFromInstanceProfile",
      "iam:TagInstanceProfile", "iam:UntagInstanceProfile",
    ]
    resources = local.scorer_iam
  }

  # The scorer role may carry exactly one managed policy, and no inline policy.
  statement {
    sid       = "AttachOnlySsmCore"
    actions   = ["iam:AttachRolePolicy", "iam:DetachRolePolicy"]
    resources = ["arn:aws:iam::${local.account_id}:role/${local.scorer_role_name}"]
    condition {
      test     = "ArnEquals"
      variable = "iam:PolicyARN"
      values   = ["arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"]
    }
  }

  statement {
    sid       = "PassScorerRoleToEc2"
    actions   = ["iam:PassRole"]
    resources = ["arn:aws:iam::${local.account_id}:role/${local.scorer_role_name}"]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ec2.amazonaws.com"]
    }
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
