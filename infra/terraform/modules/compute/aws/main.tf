data "aws_ssm_parameter" "image" {
  count = local.image_is_ami_id ? 0 : 1
  # A null name would fail before the precondition can explain why.
  name = coalesce(local.image_parameter, "/unsupported-logical-image")

  lifecycle {
    precondition {
      condition     = local.image_parameter != null
      error_message = "Unsupported logical image '${var.image}'. Add it to modules/compute/aws/locals.tf or pass an exact ami- ID."
    }
  }
}

resource "aws_instance" "this" {
  ami               = local.image_is_ami_id ? var.image : nonsensitive(data.aws_ssm_parameter.image[0].value)
  instance_type     = local.instance_type
  availability_zone = var.location
  subnet_id         = var.subnetwork

  vpc_security_group_ids      = var.firewall_id == "" ? null : [var.firewall_id]
  associate_public_ip_address = var.assign_public_ip
  iam_instance_profile        = var.instance_profile_name == "" ? null : var.instance_profile_name

  user_data                   = var.user_data
  user_data_replace_on_change = false

  disable_api_termination = var.deletion_protection
  ebs_optimized           = true
  monitoring              = false

  # IMDSv2 only. Hop limit 1 means a container behind the Docker bridge cannot
  # reach the role credentials; tags stay out of metadata.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = var.metadata_hop_limit
    instance_metadata_tags      = "disabled"
  }

  root_block_device {
    volume_size           = var.boot_disk_gb
    volume_type           = var.boot_disk_type
    encrypted             = true
    kms_key_id            = var.boot_disk_kms_key_id
    delete_on_termination = true
    tags                  = merge(var.labels, { Name = "${var.name}-root" })
  }

  dynamic "credit_specification" {
    for_each = local.burstable ? [1] : []
    content {
      cpu_credits = var.cpu_credits
    }
  }

  tags = merge(var.labels, { Name = var.name })

  lifecycle {
    precondition {
      condition     = local.instance_type != null
      error_message = "Unsupported logical size '${var.size}'. Add it to modules/compute/aws/locals.tf and every other provider map."
    }

    ignore_changes = [
      # The image is chosen at create time; in-place apt upgrades must not
      # force a replacement. Re-imaging is a manual decision.
      ami,
      # cloud-init user data runs once. Editing it would stop the instance.
      user_data,
    ]
  }
}
