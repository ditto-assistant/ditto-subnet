mock_provider "google" {}

variables {
  project = "synthetic-coding-project"
  region  = "us-central1"
  zone    = "us-central1-a"
}

run "postgres_absent_by_default" {
  command = plan
  assert {
    condition = (
      length(google_compute_firewall.postgres_egress) == 0 &&
      length(google_compute_firewall.postgres_ingress) == 0 &&
      length(google_compute_network_peering.host_to_postgres) == 0 &&
      length(google_compute_network_peering.postgres_to_host) == 0 &&
      output.postgres_access == null
    )
    error_message = "The default profile must not grant private database networking."
  }
}

run "host_alone_grants_no_postgres_path" {
  command = plan
  variables {
    enabled   = true
    operators = ["user:owner@example.com"]
  }
  assert {
    condition     = length(google_compute_network_peering.host_to_postgres) == 0 && output.postgres_access == null
    error_message = "Provisioning the host alone must not connect it to Platform's VPC."
  }
}

run "reject_postgres_without_host" {
  command = plan
  variables {
    postgres_peer = {
      network_self_link = "https://www.googleapis.com/compute/v1/projects/synthetic-coding-project/global/networks/ditto-platform-net"
      private_ip        = "10.30.0.7"
      target_tag        = "postgres"
    }
  }
  expect_failures = [var.postgres_peer]
}

run "reject_public_database_address" {
  command = plan
  variables {
    enabled   = true
    operators = ["user:owner@example.com"]
    postgres_peer = {
      network_self_link = "https://www.googleapis.com/compute/v1/projects/synthetic-coding-project/global/networks/ditto-platform-net"
      private_ip        = "203.0.113.7"
      target_tag        = "postgres"
    }
  }
  expect_failures = [var.postgres_peer]
}

run "reject_other_project_network" {
  command = plan
  variables {
    enabled   = true
    operators = ["user:owner@example.com"]
    postgres_peer = {
      network_self_link = "https://www.googleapis.com/compute/v1/projects/other-project/global/networks/ditto-platform-net"
      private_ip        = "10.30.0.7"
      target_tag        = "postgres"
    }
  }
  expect_failures = [var.postgres_peer]
}

run "exact_private_postgres_path" {
  command = plan
  variables {
    enabled   = true
    operators = ["user:owner@example.com"]
    postgres_peer = {
      network_self_link = "https://www.googleapis.com/compute/v1/projects/synthetic-coding-project/global/networks/ditto-platform-net"
      private_ip        = "10.30.0.7"
      target_tag        = "postgres"
    }
  }
  override_module {
    target = module.host[0]
    outputs = {
      internal_ip = "10.33.0.9"
      hostname    = "ditto-coding-hosted-v2"
      id          = "synthetic-host-instance"
    }
  }
  assert {
    condition = (
      google_compute_firewall.postgres_egress[0].direction == "EGRESS" &&
      toset(google_compute_firewall.postgres_egress[0].destination_ranges) == toset(["10.30.0.7/32"]) &&
      one(google_compute_firewall.postgres_egress[0].allow).protocol == "tcp" &&
      one(google_compute_firewall.postgres_egress[0].allow).ports == tolist(["5432"]) &&
      google_compute_firewall.postgres_egress[0].priority < google_compute_firewall.deny_private[0].priority &&
      google_compute_firewall.postgres_ingress[0].direction == "INGRESS" &&
      toset(google_compute_firewall.postgres_ingress[0].source_ranges) == toset(["10.33.0.9/32"]) &&
      toset(google_compute_firewall.postgres_ingress[0].target_tags) == toset(["postgres"]) &&
      one(google_compute_firewall.postgres_ingress[0].allow).protocol == "tcp" &&
      one(google_compute_firewall.postgres_ingress[0].allow).ports == tolist(["5432"])
    )
    error_message = "Only the exact native host and exact PostgreSQL IP may exchange TCP 5432."
  }
  assert {
    condition = alltrue([for peer in [
      google_compute_network_peering.host_to_postgres[0],
      google_compute_network_peering.postgres_to_host[0],
      ] : (
      !peer.import_custom_routes && !peer.export_custom_routes &&
      !peer.import_subnet_routes_with_public_ip && !peer.export_subnet_routes_with_public_ip &&
      peer.stack_type == "IPV4_ONLY"
    )])
    error_message = "No custom, public-subnet or IPv6 routes may be exchanged."
  }
  assert {
    condition = (
      output.postgres_access.client_cidr == "10.33.0.9/32" &&
      output.postgres_access.postgres_private_ip == "10.30.0.7" &&
      output.postgres_access.port == 5432 &&
      !output.postgres_access.database_login_ready &&
      output.postgres_access.shadow_only && !output.postgres_access.weight_eligible
    )
    error_message = "Network coordinates are not database or private-execution readiness."
  }
}
