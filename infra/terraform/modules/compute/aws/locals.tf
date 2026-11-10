locals {
  # Logical -> native EC2 instance type. Keep every key in step with
  # ../gcp/locals.tf; the shared table is in ../interface.md. Every class is
  # x86_64: generator output bytes and fingerprints must match miners' x86
  # machines, and Go fuses FMA on arm64, so Graviton is deliberately absent.
  instance_type_map = {
    "prod-64gb"    = "m6i.4xlarge" # 16 vCPU / 64 GiB
    "staging-32gb" = "m6i.2xlarge" # 8 vCPU / 32 GiB

    "db-prod"    = "m6i.xlarge" # 4 vCPU / 16 GiB
    "db-staging" = "m6i.large"  # 2 vCPU / 8 GiB

    # Burstable 2 vCPU / 4 GiB, the e2-medium analogue. The generator scorer
    # fits here: one sandboxed build (2 GiB cap) plus one 1 GiB generator run
    # plus Docker. t3.small (2 GiB) does not.
    "app-small" = "t3.medium"

    "controller-small" = "t3.small" # 2 vCPU burstable / 2 GiB

    "app-standard"   = "m6i.xlarge"  # 4 vCPU / 16 GiB
    "validator"      = "m6i.xlarge"  # 4 vCPU / 16 GiB
    "validator-prod" = "m6i.2xlarge" # 8 vCPU / 32 GiB
    "screener-burst" = "m6i.2xlarge" # 8 vCPU / 32 GiB

    # AMD EPYC class, matching the GCP n2d variant.
    "screener-burst-n2d" = "m6a.2xlarge" # 8 vCPU / 32 GiB
  }

  # Logical -> SSM public parameter that resolves the newest official x86_64
  # AMI (the Debian cloud team and Canonical publish these). An explicit
  # `ami-...` passes through so a caller can pin an exact image.
  image_parameter_map = {
    "debian-13" = "/aws/service/debian/release/13/latest/amd64"
    "debian-12" = "/aws/service/debian/release/12/latest/amd64"
    "ubuntu-24" = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
  }

  instance_type   = lookup(local.instance_type_map, var.size, null)
  image_is_ami_id = can(regex("^ami-[0-9a-f]{8,17}$", var.image))
  image_parameter = lookup(local.image_parameter_map, var.image, null)
  burstable       = can(regex("^t[0-9]", coalesce(local.instance_type, "none")))
}

# Unsupported sizes and images fail plan through the preconditions on
# aws_instance.this (a `check` block would only warn).
