###############################################################################
# SN118 CPM ("model_context") scorer on AWS (docs/research/cpm/competition-v1.md
# §1, §6.1).
#
# One GPU host (g6e.xlarge, one L40S 48 GB) that leases model_context runs
# from Platform over HTTPS, pulls the miner's model at its pinned revision from
# Hugging Face, scores it, and posts the result. Nothing calls in. It loads
# untrusted miner weights, so it gets its own VPC, no ingress at all, and an
# identity that can do nothing in AWS beyond registering with SSM. That
# identity is created in aws-bootstrap (cpm-scorer-identity.tf), not here. Its
# Platform bearer will follow the generator scorer's pattern (GCP Secret
# Manager read through Workload Identity Federation from this instance role) in
# a later change; this root creates no secret and no federation.
#
# The first boot installs only a pinned SSM agent. The NVIDIA driver, CUDA
# runtime and the scorer itself belong to the cpm_scorer Ansible role (role
# group role_cpm_scorer from the `role` tag), run over SSM.
#
# Network: a dedicated VPC with one public subnet and an internet gateway, not
# the account's default VPC and not a NAT gateway (~$33/month for one host).
# The public IPv4 is egress only: the security group admits nothing inbound and
# allows only 80/443 out (apt, the NVIDIA and PyPI repositories, Hugging Face,
# GitHub, SSM, GCP, Platform). DNS and Amazon Time Sync use link-local
# addresses that security groups do not filter.
###############################################################################

locals {
  scorer_count = var.enable_cpm_scorer ? 1 : 0
  scorer_name  = "ditto-cpm-scorer-prod"
  # aws-bootstrap creates this exact role and denies the CI apply role every
  # IAM write on it (NeverWriteCpmScorerIdentity).
  scorer_role_name = "ditto-cpm-scorer"

  user_data = templatefile("${path.module}/user-data.sh.tftpl", {
    region            = var.region
    ssm_agent_version = var.ssm_agent_version
    ssm_agent_sha256  = var.ssm_agent_sha256
  })
}

data "aws_caller_identity" "current" {}

# --- network ----------------------------------------------------------------

resource "aws_vpc" "scorer" {
  count                = local.scorer_count
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${local.scorer_name}-vpc" }
}

# The VPC's AWS-created default security group is left as AWS made it
# (allow all from itself, all egress) and untouched by Terraform: managing it
# would mean tagging an existing group into the apply role's scope, and any
# grant that allows that would reach every group in the region. Residual:
# nothing attaches to it. The only instance in this VPC uses its own
# egress-only group (module firewall_id), and the VPC holds no other ENIs.

resource "aws_internet_gateway" "scorer" {
  count  = local.scorer_count
  vpc_id = aws_vpc.scorer[0].id
  tags   = { Name = "${local.scorer_name}-igw" }
}

resource "aws_subnet" "scorer" {
  count                   = local.scorer_count
  vpc_id                  = aws_vpc.scorer[0].id
  cidr_block              = var.vpc_cidr
  availability_zone       = var.availability_zone
  map_public_ip_on_launch = false
  tags                    = { Name = "${local.scorer_name}-subnet" }
}

resource "aws_route_table" "scorer" {
  count  = local.scorer_count
  vpc_id = aws_vpc.scorer[0].id
  tags   = { Name = "${local.scorer_name}-egress" }
}

resource "aws_route" "scorer_egress" {
  count                  = local.scorer_count
  route_table_id         = aws_route_table.scorer[0].id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.scorer[0].id
}

resource "aws_route_table_association" "scorer" {
  count          = local.scorer_count
  subnet_id      = aws_subnet.scorer[0].id
  route_table_id = aws_route_table.scorer[0].id
}

resource "aws_security_group" "scorer" {
  count       = local.scorer_count
  name        = "${local.scorer_name}-egress-only"
  description = "CPM scorer: no ingress; HTTP/HTTPS egress only."
  vpc_id      = aws_vpc.scorer[0].id
  tags        = { Name = "${local.scorer_name}-egress-only" }
}

resource "aws_vpc_security_group_egress_rule" "scorer" {
  for_each          = var.enable_cpm_scorer ? toset(["80", "443"]) : toset([])
  security_group_id = aws_security_group.scorer[0].id
  description       = "TCP ${each.key} egress"
  ip_protocol       = "tcp"
  from_port         = tonumber(each.key)
  to_port           = tonumber(each.key)
  cidr_ipv4         = "0.0.0.0/0"
}

# --- identity ---------------------------------------------------------------

# The instance role and profile are created by an administrator in
# aws-bootstrap (cpm-scorer-identity.tf), never by this CI-applied root: a
# later GCP federation will trust every session of the role name, so the
# identity that applies this root must not be able to create, delete or
# retrust it. This root only reads the profile and checks that it wraps the
# expected role.
data "aws_iam_instance_profile" "scorer" {
  count = local.scorer_count
  name  = var.instance_profile_name

  lifecycle {
    postcondition {
      condition     = self.role_name == local.scorer_role_name
      error_message = "The CPM scorer instance profile must wrap the ${local.scorer_role_name} role. Apply aws-bootstrap with enable_cpm_scorer_identity = true first."
    }
  }
}

# --- Ansible SSM transfer bucket ---------------------------------------------

# amazon.aws.aws_ssm stages module payloads here (inventory/aws.yml reads the
# ansible-ssm-bucket tag). Objects are transient, so they expire after a day
# and the bucket may be emptied on destroy.
resource "aws_s3_bucket" "ssm_transfer" {
  count         = local.scorer_count
  bucket        = "ditto-cpm-scorer-ssm-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_ownership_controls" "ssm_transfer" {
  count  = local.scorer_count
  bucket = aws_s3_bucket.ssm_transfer[0].id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "ssm_transfer" {
  count                   = local.scorer_count
  bucket                  = aws_s3_bucket.ssm_transfer[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "ssm_transfer" {
  count  = local.scorer_count
  bucket = aws_s3_bucket.ssm_transfer[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "ssm_transfer" {
  count  = local.scorer_count
  bucket = aws_s3_bucket.ssm_transfer[0].id
  rule {
    id     = "expire-transfers"
    status = "Enabled"
    filter {}
    expiration {
      days = 1
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

data "aws_iam_policy_document" "ssm_transfer_tls" {
  count = local.scorer_count
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.ssm_transfer[0].arn,
      "${aws_s3_bucket.ssm_transfer[0].arn}/*",
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

resource "aws_s3_bucket_policy" "ssm_transfer" {
  count      = local.scorer_count
  bucket     = aws_s3_bucket.ssm_transfer[0].id
  policy     = data.aws_iam_policy_document.ssm_transfer_tls[0].json
  depends_on = [aws_s3_bucket_public_access_block.ssm_transfer]
}

# --- host -------------------------------------------------------------------

module "cpm_scorer" {
  source   = "../../modules/compute/aws"
  count    = local.scorer_count
  name     = local.scorer_name
  size     = var.size
  image    = var.image
  location = var.availability_zone

  subnetwork       = aws_subnet.scorer[0].id
  firewall_id      = aws_security_group.scorer[0].id
  assign_public_ip = true
  boot_disk_gb     = var.boot_disk_gb

  instance_profile_name = data.aws_iam_instance_profile.scorer[0].name
  deletion_protection   = var.deletion_protection

  # IMDSv2 with hop limit 1 (the module default), so a container on the host
  # cannot reach the role credentials.
  metadata_hop_limit = 1

  user_data = local.user_data

  # env/role/managed put it in the aws_ec2 inventory's role_cpm_scorer group.
  # The inventory reads the transfer bucket from a tag. stack = aws-cpm-scorer
  # comes from the provider default_tags.
  labels = {
    env                = "prod"
    role               = "cpm-scorer"
    managed            = "terraform"
    ansible-ssm-bucket = aws_s3_bucket.ssm_transfer[0].bucket
  }

  depends_on = [aws_route_table_association.scorer, aws_route.scorer_egress]
}
