###############################################################################
# DittoBench generator competition scorer on AWS.
#
# The same challenge-scorer host as gcp-platform/generator-scorer.tf, placed on
# EC2 through modules/compute/aws so the Ansible role is unchanged. It executes
# untrusted miner Go code in a networkless Docker sandbox, so it gets its own
# VPC, no ingress at all, and an identity that can do nothing in AWS beyond
# registering with SSM. Its one secret, the Platform bearer, stays in GCP
# Secret Manager and is read through Workload Identity Federation from this
# instance role (gcp-platform/generator-scorer.tf, generator_scorer_aws_*).
#
# Network: a dedicated VPC with one public subnet and an internet gateway, not
# the account's default VPC (shared with unrelated CDK stacks and editable out
# of band) and not a NAT gateway (~$33/month for one host). The public IPv4 is
# egress only: the security group admits nothing inbound and allows only
# 80/443 out (apt, Docker registries, GitHub, the Go proxy, SSM, Platform).
# DNS and Amazon Time Sync use link-local addresses that groups do not filter.
###############################################################################

locals {
  scorer_count = var.enable_generator_scorer ? 1 : 0
  scorer_name  = "ditto-generator-scorer-prod"
  # The GCP provider's attribute condition names this exact role.
  scorer_role_name = "ditto-generator-scorer"
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

# Strip the VPC's default security group so nothing can attach to it by
# accident and inherit its allow-all-from-self rule.
resource "aws_default_security_group" "scorer" {
  count  = local.scorer_count
  vpc_id = aws_vpc.scorer[0].id
  tags   = { Name = "${local.scorer_name}-default-closed" }
}

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
  description = "Generator scorer: no ingress; HTTP/HTTPS egress only."
  vpc_id      = aws_vpc.scorer[0].id
  tags        = { Name = "${local.scorer_name}-egress-only" }
}

resource "aws_vpc_security_group_egress_rule" "scorer" {
  for_each          = var.enable_generator_scorer ? toset(["80", "443"]) : toset([])
  security_group_id = aws_security_group.scorer[0].id
  description       = "TCP ${each.key} egress"
  ip_protocol       = "tcp"
  from_port         = tonumber(each.key)
  to_port           = tonumber(each.key)
  cidr_ipv4         = "0.0.0.0/0"
}

# --- identity ---------------------------------------------------------------

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
  count                = local.scorer_count
  name                 = local.scorer_role_name
  description          = "Generator competition scorer host: SSM registration; federated to GCP for its one bearer."
  assume_role_policy   = data.aws_iam_policy_document.scorer_assume.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "scorer_ssm" {
  count      = local.scorer_count
  role       = aws_iam_role.scorer[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "scorer" {
  count = local.scorer_count
  name  = local.scorer_role_name
  role  = aws_iam_role.scorer[0].name
}

# --- Ansible SSM transfer bucket ---------------------------------------------

# amazon.aws.aws_ssm stages module payloads here. Objects are transient, so
# they expire after a day and the bucket may be emptied on destroy.
resource "aws_s3_bucket" "ssm_transfer" {
  count         = local.scorer_count
  bucket        = "ditto-generator-scorer-ssm-${data.aws_caller_identity.current.account_id}"
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

module "generator_scorer" {
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

  instance_profile_name = aws_iam_instance_profile.scorer[0].name
  cpu_credits           = var.cpu_credits
  deletion_protection   = var.deletion_protection

  user_data = templatefile("${path.module}/user-data.sh.tftpl", {
    region            = var.region
    ssm_agent_version = var.ssm_agent_version
    ssm_agent_sha256  = var.ssm_agent_sha256
  })

  # Same env/role/managed labels as the GCE VM, so both inventories put it in
  # role_generator_scorer. The inventory reads the transfer bucket from a tag.
  labels = {
    env                = "prod"
    role               = "generator-scorer"
    managed            = "terraform"
    ansible-ssm-bucket = aws_s3_bucket.ssm_transfer[0].bucket
  }

  depends_on = [aws_route_table_association.scorer, aws_route.scorer_egress]
}
