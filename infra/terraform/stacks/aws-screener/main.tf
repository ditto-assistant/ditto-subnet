###############################################################################
# On-demand SN118 screener node on AWS EC2.
#
# A stopped instance that the GCE capacity controller starts when screening
# backlog needs it (through the wake role in ../aws-bootstrap/screener.tf) and
# that powers itself off when its worker has been idle. EC2's
# instance-initiated shutdown behavior is `stop`, so the persistent gp3 root
# volume keeps the node credential, release checkout and build caches between
# runs. This root never stops, starts or terminates the instance.
#
# First boot converges the host with no operator SSH or Ansible run: the user
# data checks out one exact release commit and runs
# infra/ansible/playbooks/aws-screener-node.yml against localhost. The node
# reads its review keys and single-use registration grant from GCP Secret
# Manager through Workload Identity Federation of the instance role
# (gcp-platform/screener-aws.tf). That role and its instance profile are
# created by ../aws-bootstrap and only referenced here by name, so the CI apply
# role can never change who GCP trusts.
#
# Network: a dedicated VPC with one public subnet and an internet gateway, no
# NAT gateway. The public IPv4 is egress only: the security group admits
# nothing inbound and allows only 80/443 out (apt, Docker registries, crates,
# PyPI, GitHub, SSM, GCP, Platform). DNS and Amazon Time Sync use link-local
# addresses that security groups do not filter.
###############################################################################

locals {
  screener_count = var.enable_aws_screener ? 1 : 0
  network_name   = "ditto-screener-aws"
  # Ansible and the node-agent contract: do not rename keys (see
  # infra/ansible/playbooks/aws-screener-node.yml). The instance id is added
  # on the host at first boot as screener_fleet_provider_resource_id.
  bootstrap_vars = {
    screener_fleet_node_id                   = var.screener_fleet_node_id
    screener_fleet_provider                  = "aws"
    screener_fleet_release_sha               = var.screener_fleet_release_sha
    screener_fleet_builder_image             = var.screener_fleet_builder_image
    screener_fleet_google_identity           = "aws"
    screener_fleet_aws_wif_provider          = var.screener_fleet_aws_wif_provider
    screener_fleet_gcp_project               = var.screener_fleet_gcp_project
    screener_fleet_registration_token_secret = var.screener_fleet_registration_token_secret
    screener_fleet_kvm_enabled               = false
    screener_fleet_idle_exit_seconds         = var.screener_fleet_idle_exit_seconds
    screener_fleet_worker_processes          = var.screener_fleet_worker_processes
    screener_fleet_runtime_enabled           = true
    screener_fleet_platform_url              = var.screener_fleet_platform_url
  }
}

data "aws_caller_identity" "current" {}

# The profile is aws-bootstrap's, never this root's. Reading it fails the plan
# early when the bootstrap has not been applied, and pins the one role GCP
# federates.
data "aws_iam_instance_profile" "node" {
  count = local.screener_count
  name  = var.instance_profile_name

  lifecycle {
    postcondition {
      condition     = self.role_name == "ditto-screener-node"
      error_message = "The instance profile must carry ditto-screener-node, the only AWS role gcp-platform/screener-aws.tf federates."
    }
  }
}

# --- network ----------------------------------------------------------------

resource "aws_vpc" "screener" {
  count                = local.screener_count
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${local.network_name}-vpc" }

  lifecycle {
    precondition {
      condition = (
        can(regex("^[0-9a-f]{40}$", var.screener_fleet_release_sha)) &&
        var.screener_fleet_builder_image != "" &&
        var.screener_fleet_aws_wif_provider != ""
      )
      error_message = "enable_aws_screener requires screener_fleet_release_sha, screener_fleet_builder_image and screener_fleet_aws_wif_provider."
    }
  }
}

# The VPC's AWS-created default security group is left as AWS made it
# (allow all from itself, all egress) and untouched by Terraform: managing it
# would mean tagging an existing group into the CI apply role's scope, and any
# grant that allows that would reach every group in the region. Residual:
# nothing attaches to it. The only instance in this VPC uses its own
# egress-only group (module firewall_id), and the VPC holds no other ENIs.

# The rootless build sandbox's egress guard rejects RFC1918 and link-local
# destinations except DNS on 169.254.169.254 (GCE) and 169.254.169.253 (the
# Amazon resolver's link-local address). AmazonProvidedDNS would hand the host
# the VPC base+2 resolver, an RFC1918 address the guard blocks, so builds could
# not resolve registries. Point DHCP at the link-local resolver instead.
resource "aws_vpc_dhcp_options" "screener" {
  count               = local.screener_count
  domain_name_servers = ["169.254.169.253"]
  tags                = { Name = "${local.network_name}-dhcp" }
}

resource "aws_vpc_dhcp_options_association" "screener" {
  count           = local.screener_count
  vpc_id          = aws_vpc.screener[0].id
  dhcp_options_id = aws_vpc_dhcp_options.screener[0].id
}

resource "aws_internet_gateway" "screener" {
  count  = local.screener_count
  vpc_id = aws_vpc.screener[0].id
  tags   = { Name = "${local.network_name}-igw" }
}

resource "aws_subnet" "screener" {
  count                   = local.screener_count
  vpc_id                  = aws_vpc.screener[0].id
  cidr_block              = var.vpc_cidr
  availability_zone       = var.availability_zone
  map_public_ip_on_launch = false
  tags                    = { Name = "${local.network_name}-subnet" }
}

resource "aws_route_table" "screener" {
  count  = local.screener_count
  vpc_id = aws_vpc.screener[0].id
  tags   = { Name = "${local.network_name}-egress" }
}

resource "aws_route" "screener_egress" {
  count                  = local.screener_count
  route_table_id         = aws_route_table.screener[0].id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.screener[0].id
}

resource "aws_route_table_association" "screener" {
  count          = local.screener_count
  subnet_id      = aws_subnet.screener[0].id
  route_table_id = aws_route_table.screener[0].id
}

resource "aws_security_group" "screener" {
  count       = local.screener_count
  name        = "${local.network_name}-egress-only"
  description = "On-demand screener: no ingress; HTTP/HTTPS egress only."
  vpc_id      = aws_vpc.screener[0].id
  tags        = { Name = "${local.network_name}-egress-only" }
}

resource "aws_vpc_security_group_egress_rule" "screener" {
  for_each          = var.enable_aws_screener ? toset(["80", "443"]) : toset([])
  security_group_id = aws_security_group.screener[0].id
  description       = "TCP ${each.key} egress"
  ip_protocol       = "tcp"
  from_port         = tonumber(each.key)
  to_port           = tonumber(each.key)
  cidr_ipv4         = "0.0.0.0/0"
}

# --- host -------------------------------------------------------------------

module "screener" {
  source   = "../../modules/compute/aws"
  count    = local.screener_count
  name     = var.screener_fleet_node_id
  size     = var.size
  image    = var.image
  location = var.availability_zone

  subnetwork       = aws_subnet.screener[0].id
  firewall_id      = aws_security_group.screener[0].id
  assign_public_ip = true
  boot_disk_gb     = var.boot_disk_gb

  instance_profile_name = data.aws_iam_instance_profile.node[0].name
  deletion_protection   = var.deletion_protection

  # The idle worker powers the OS off; that must stop (not delete) the
  # instance so the controller can start it again with the same identity.
  instance_initiated_shutdown_behavior = "stop"
  # Containers on the Docker bridge must not reach the role credentials.
  metadata_hop_limit = 1

  user_data = templatefile("${path.module}/user-data.sh.tftpl", {
    region               = var.region
    ssm_agent_version    = var.ssm_agent_version
    ssm_agent_sha256     = var.ssm_agent_sha256
    ansible_core_version = var.ansible_core_version
    release_sha          = var.screener_fleet_release_sha
    bootstrap_vars_b64   = base64encode(jsonencode(local.bootstrap_vars))
  })

  # Not in any controller-run Ansible inventory group: the host converges
  # itself. stack = aws-screener comes from the provider default_tags.
  labels = {
    env     = "prod"
    role    = "screener-aws"
    managed = "terraform"
  }

  depends_on = [
    aws_route_table_association.screener,
    aws_route.screener_egress,
    # The first boot resolves through the link-local resolver.
    aws_vpc_dhcp_options_association.screener,
  ]
}
