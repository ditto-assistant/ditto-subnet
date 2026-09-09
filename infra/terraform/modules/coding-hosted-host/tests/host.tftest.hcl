# No cloud credentials, state backend, live reads, or apply operations.
mock_provider "google" {}

variables {
  project = "synthetic-coding-project"
  region  = "us-central1"
  zone    = "us-central1-a"
}

run "absent_by_default" {
  command = plan
  assert {
    condition = alltrue([
      length(module.host) == 0,
      length(google_compute_network.host) == 0,
      length(google_compute_subnetwork.host) == 0,
      length(google_compute_router.host) == 0,
      length(google_compute_router_nat.host) == 0,
      length(google_compute_firewall.iap_ssh) == 0,
      length(google_compute_firewall.deny_private) == 0,
      length(google_compute_firewall.host_web) == 0,
      length(google_compute_firewall.deny_other_egress) == 0,
      length(google_service_account.host) == 0,
      length(google_project_iam_member.telemetry) == 0,
      length(google_compute_instance_iam_member.osadmin) == 0,
      length(google_project_iam_member.ssh) == 0,
      length(google_service_account_iam_member.actas) == 0,
      output.host == null,
    ])
    error_message = "Disabled means no host, network, IAM, or readiness identity."
  }
}

run "disabled_operators_grant_nothing" {
  command = plan
  variables {
    operators = ["user:owner@example.com"]
  }
  assert {
    condition = (
      length(google_compute_instance_iam_member.osadmin) == 0 &&
      length(google_project_iam_member.ssh) == 0 &&
      length(google_service_account_iam_member.actas) == 0
    )
    error_message = "Listing custodians must not activate IAM."
  }
}

run "requires_explicit_custodian" {
  command = plan
  variables {
    enabled = true
  }
  expect_failures = [var.operators]
}

run "reject_public_custodian" {
  command = plan
  variables {
    enabled   = true
    operators = ["allUsers"]
  }
  expect_failures = [var.operators]
}

run "reject_group_custodian" {
  command = plan
  variables {
    operators = ["group:validators@example.com"]
  }
  expect_failures = [var.operators]
}

run "reject_service_account_custodian" {
  command = plan
  variables {
    operators = ["serviceAccount:worker@synthetic.iam.gserviceaccount.com"]
  }
  expect_failures = [var.operators]
}

run "reject_small_disk" {
  command = plan
  variables {
    boot_disk_gb = 199
  }
  expect_failures = [var.boot_disk_gb]
}

run "reject_unbounded_disk" {
  command = plan
  variables {
    boot_disk_gb = 1001
  }
  expect_failures = [var.boot_disk_gb]
}

run "reject_fractional_disk" {
  command = plan
  variables {
    boot_disk_gb = 200.5
  }
  expect_failures = [var.boot_disk_gb]
}

run "enabled_foundation" {
  command = plan
  variables {
    enabled   = true
    operators = ["user:owner@example.com"]
  }
  assert {
    condition = (
      length(module.host) == 1 &&
      !google_compute_network.host[0].auto_create_subnetworks &&
      google_compute_subnetwork.host[0].ip_cidr_range == "10.33.0.0/24" &&
      google_compute_subnetwork.host[0].private_ip_google_access &&
      google_compute_router_nat.host[0].source_subnetwork_ip_ranges_to_nat == "LIST_OF_SUBNETWORKS"
    )
    error_message = "One host must use its separate private network and scoped NAT."
  }
  assert {
    condition = (
      toset(keys(google_project_iam_member.telemetry)) == toset(["roles/logging.logWriter", "roles/monitoring.metricWriter"]) &&
      google_compute_instance_iam_member.osadmin["user:owner@example.com"].role == "roles/compute.osAdminLogin" &&
      google_project_iam_member.ssh["user:owner@example.com"].condition[0].expression == "resource.name.extract('/instances/{name}') == 'ditto-coding-hosted-v2' && destination.port == 22" &&
      google_project_iam_member.ssh["user:owner@example.com"].role == "roles/iap.tunnelResourceAccessor" &&
      length(google_service_account_iam_member.actas) == 1
    )
    error_message = "Runtime IAM must be telemetry-only, with explicit instance-scoped SSH custodians."
  }
  assert {
    condition = (
      google_compute_firewall.iap_ssh[0].direction == "INGRESS" &&
      toset(google_compute_firewall.iap_ssh[0].source_ranges) == toset(["35.235.240.0/20"]) &&
      one(google_compute_firewall.iap_ssh[0].allow).ports == tolist(["22"]) &&
      google_compute_firewall.deny_private[0].priority < google_compute_firewall.host_web[0].priority &&
      google_compute_firewall.host_web[0].priority < google_compute_firewall.deny_other_egress[0].priority &&
      toset(google_compute_firewall.deny_private[0].destination_ranges) == toset(["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"]) &&
      toset(one(google_compute_firewall.host_web[0].allow).ports) == toset(["80", "443"]) &&
      one(google_compute_firewall.deny_other_egress[0].deny).protocol == "all"
    )
    error_message = "IAP ingress and ordered host-only web egress restrictions must remain intact."
  }
}
