###############################################################################
# Account-wide CloudTrail (SOC 2 audit logging).
#
# CloudTrail's built-in event history keeps management events for 90 days
# without integrity proof. This trail keeps a durable, verifiable copy: every
# management event (read and write) in every region, including global
# services (IAM, STS, sign-in), with digest files so log tampering is
# detectable. It does not record data events (S3 object reads, Lambda
# invokes); they are billed per event and can be added later as advanced
# event selectors scoped to specific buckets.
#
# Cost: the first copy of management events is free. What remains is S3
# storage and CloudTrail's delivery PUTs, on the order of $1-2/month at this
# account's volume.
#
# Retention: 400 days in S3, so a year-long SOC 2 observation period is covered
# with margin. Objects move to Glacier Instant Retrieval after 90 days.
#
# Tamper resistance: S3 Object Lock (governance mode) gives every log and
# digest version a 400-day retention, so no version can be removed early,
# including by the lifecycle rule. The bucket policy additionally denies
# object and version deletes, governance bypass and retention changes to every
# principal. Removing evidence therefore first needs a bucket-policy change,
# which this trail itself records. Governance (not compliance) mode keeps the
# lock reversible by an administrator who removes that deny on purpose.
###############################################################################

locals {
  trail_name   = "ditto-account-trail"
  trail_bucket = "ditto-cloudtrail-${local.account_id}"
  trail_arn    = "arn:aws:cloudtrail:${local.region}:${local.account_id}:trail/${local.trail_name}"

  # One retention period for the Object Lock default and the lifecycle expiry.
  trail_retention_days = 400
}

resource "aws_s3_bucket" "trail" {
  bucket = local.trail_bucket
  # Object Lock can only be turned on when the bucket is created.
  object_lock_enabled = true
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_ownership_controls" "trail" {
  bucket = aws_s3_bucket.trail.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "trail" {
  bucket                  = aws_s3_bucket.trail.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "trail" {
  bucket = aws_s3_bucket.trail.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Every new log and digest version is locked for 400 days from creation.
resource "aws_s3_bucket_object_lock_configuration" "trail" {
  bucket = aws_s3_bucket.trail.id
  rule {
    default_retention {
      mode = "GOVERNANCE"
      days = local.trail_retention_days
    }
  }
  depends_on = [aws_s3_bucket_versioning.trail]
}

resource "aws_s3_bucket_server_side_encryption_configuration" "trail" {
  bucket = aws_s3_bucket.trail.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "trail" {
  bucket = aws_s3_bucket.trail.id
  rule {
    id     = "retain-400-days"
    status = "Enabled"
    filter {}
    transition {
      days          = 90
      storage_class = "GLACIER_IR"
    }
    expiration {
      days = local.trail_retention_days
    }
    # Clears versions made noncurrent by the expiration above. Object Lock
    # keeps any version until 400 days after its creation, so a version
    # hidden early by a delete marker is not removed before then.
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
  depends_on = [
    aws_s3_bucket_versioning.trail,
    aws_s3_bucket_object_lock_configuration.trail,
  ]
}

# CloudTrail may check the ACL and write under AWSLogs/<account>/ for this one
# trail only (aws:SourceArn). No principal may delete or unlock log objects,
# and everything else must use TLS.
data "aws_iam_policy_document" "trail_bucket" {
  statement {
    sid       = "CloudTrailAclCheck"
    actions   = ["s3:GetBucketAcl"]
    resources = [aws_s3_bucket.trail.arn]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.trail_arn]
    }
  }
  statement {
    sid       = "CloudTrailWrite"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.trail.arn}/AWSLogs/${local.account_id}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.trail_arn]
    }
  }
  # Lifecycle expiration is not subject to bucket policy, so this does not
  # stop the 400-day cleanup; it stops people and roles (including the root
  # user) from deleting, unlocking or shortening the retention of log objects.
  statement {
    sid    = "DenyLogTampering"
    effect = "Deny"
    actions = [
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "s3:BypassGovernanceRetention",
      "s3:PutObjectRetention",
      "s3:PutObjectLegalHold",
    ]
    resources = ["${aws_s3_bucket.trail.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
  }
  # Nobody may shorten or remove the default retention for later logs. To
  # change it deliberately, remove this statement first (a PutBucketPolicy the
  # trail records), then update aws_s3_bucket_object_lock_configuration.trail.
  statement {
    sid       = "DenyObjectLockConfigurationChanges"
    effect    = "Deny"
    actions   = ["s3:PutBucketObjectLockConfiguration"]
    resources = [aws_s3_bucket.trail.arn]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
  }
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.trail.arn,
      "${aws_s3_bucket.trail.arn}/*",
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

resource "aws_s3_bucket_policy" "trail" {
  bucket = aws_s3_bucket.trail.id
  policy = data.aws_iam_policy_document.trail_bucket.json
  # The lock configuration must exist before the policy denies changing it.
  depends_on = [
    aws_s3_bucket_public_access_block.trail,
    aws_s3_bucket_object_lock_configuration.trail,
  ]
}

resource "aws_cloudtrail" "account" {
  name                          = local.trail_name
  s3_bucket_name                = aws_s3_bucket.trail.id
  is_multi_region_trail         = true
  include_global_service_events = true
  enable_log_file_validation    = true
  enable_logging                = true

  event_selector {
    read_write_type           = "All"
    include_management_events = true
  }

  depends_on = [aws_s3_bucket_policy.trail]
}

output "cloudtrail_arn" {
  description = "Account-wide CloudTrail trail."
  value       = aws_cloudtrail.account.arn
}

output "cloudtrail_bucket" {
  description = "S3 bucket holding CloudTrail logs and digest files."
  value       = aws_s3_bucket.trail.bucket
}
