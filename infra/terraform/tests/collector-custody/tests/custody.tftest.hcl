mock_provider "google" {
  mock_data "google_project" {
    defaults = { number = "123456" }
  }
}

override_resource {
  target          = google_service_account.collector_delegate["registration"]
  override_during = plan
  values = {
    name  = "projects/ditto-app-dev/serviceAccounts/sn118-collector-registration@ditto-app-dev.iam.gserviceaccount.com"
    email = "sn118-collector-registration@ditto-app-dev.iam.gserviceaccount.com"
  }
}

override_resource {
  target          = google_service_account.collector_delegate["transfer"]
  override_during = plan
  values = {
    name  = "projects/ditto-app-dev/serviceAccounts/sn118-collector-transfer@ditto-app-dev.iam.gserviceaccount.com"
    email = "sn118-collector-transfer@ditto-app-dev.iam.gserviceaccount.com"
  }
}

variables {
  project                    = "ditto-app-dev"
  region                     = "us-central1"
  zone                       = "us-central1-a"
  collector_custody_revision = "0123456789012345678901234567890123456789"
  collector_custody_operator = "operator@example.com"
  collector_custody_offline_addresses = [
    "5Fhnko5TCPtR1hyD8aua6n23XRM8cxcBM6TgNEqqUhcxWs1t",
    "5H93a8uVJC6ve2bNVZu9jAGEuBHDFspct3UbHRYGsxUNpA1e",
    "5HeA6bjjt1apDMhLSqKkCWZCEGvZBPByjvMxZ7mKiUk67AGT",
    "5CD8TZWNSNtARxmB5VQF9qTNfCVjhdMdjmwBrniqU3E71ohQ",
    "5F2821EzMcBC2b8hWmrMWiNRcx6DgdaTQQiqmPkbAk7a6yn9",
  ]
}

run "disabled_creates_no_custody" {
  command = plan
  assert {
    condition     = length(google_compute_instance.collector_delegate) == 0 && length(google_secret_manager_secret.collector_delegate) == 0 && length(google_service_account.collector_delegate) == 0 && length(google_compute_network.collector_custody) == 0
    error_message = "Disabled custody must create no host, secret, identity or network."
  }
}

run "bootstrap_has_two_private_hosts_and_no_secret_authority" {
  command = plan
  variables { enable_collector_custody = true }
  assert {
    condition     = length(google_compute_instance.collector_delegate) == 2 && length(google_service_account.collector_delegate) == 2 && length(google_secret_manager_secret.collector_delegate) == 2
    error_message = "Two distinct signer hosts, principals and secret containers are required."
  }
  assert {
    condition     = length(google_secret_manager_secret_iam_member.collector_generator) == 0 && length(google_secret_manager_secret_iam_member.collector_reader) == 0
    error_message = "Bootstrap must have no secret read/add grants."
  }
  assert {
    condition     = alltrue([for host in google_compute_instance.collector_delegate : length(host.network_interface[0].access_config) == 0 && host.metadata["enable-oslogin"] == "TRUE" && host.metadata["block-project-ssh-keys"] == "TRUE" && host.shielded_instance_config[0].enable_secure_boot && host.deletion_protection && !host.boot_disk[0].auto_delete])
    error_message = "Private shielded hosts must use OS Login and preserve their disks."
  }
  assert {
    condition     = length(google_service_account_iam_member.collector_operator) == 2 && alltrue([for role, binding in google_service_account_iam_member.collector_operator : binding.role == "roles/iam.serviceAccountUser" && binding.member == "user:operator@example.com" && binding.service_account_id == google_service_account.collector_delegate[role].name])
    error_message = "OS Login actAs must apply only to the operator on each exact attached identity, never a project token-creator grant."
  }
}

run "armed_has_only_restricted_api_egress_and_own_add_only_grants" {
  command = plan
  variables {
    enable_collector_custody = true
    collector_custody_phases = { registration = "armed", transfer = "armed" }
  }
  assert {
    condition     = length(google_compute_firewall.collector_bootstrap) == 0 && length(google_compute_router_nat.collector_custody) == 0 && length(google_secret_manager_secret_iam_member.collector_reader) == 0 && length(google_secret_manager_secret_iam_member.collector_generator) == 2
    error_message = "Armed creation must remove internet/bootstrap NAT and have no payload read grant."
  }
  assert {
    condition     = toset(google_project_iam_custom_role.collector_generator[0].permissions) == toset(["secretmanager.versions.add", "secretmanager.versions.list"]) && google_compute_firewall.collector_googleapis[0].destination_ranges == toset(["199.36.153.4/30"]) && google_compute_firewall.collector_deny_private[0].priority < google_compute_firewall.collector_googleapis[0].priority && google_compute_firewall.collector_deny_other[0].priority > google_compute_firewall.collector_googleapis[0].priority
    error_message = "Writer permissions and ordered egress allowlist must stay narrow."
  }
  assert {
    condition     = google_secret_manager_secret_iam_member.collector_generator["registration"].secret_id == "sn118-collector-registration-delegate" && google_secret_manager_secret_iam_member.collector_generator["transfer"].secret_id == "sn118-collector-transfer-delegate"
    error_message = "Each role must bind only its matching secret."
  }
}

run "mixed_roles_cannot_share_bootstrap_tag" {
  command = plan
  variables {
    enable_collector_custody = true
    collector_custody_phases = { registration = "armed", transfer = "bootstrap" }
  }
  assert {
    condition     = length(google_compute_firewall.collector_bootstrap) == 1 && !contains(google_compute_instance.collector_delegate["registration"].tags, "collector-transfer-bootstrap") && google_compute_firewall.collector_bootstrap["transfer"].target_tags == toset(["collector-transfer-bootstrap"])
    error_message = "A peer bootstrap must not reopen the armed role's internet access."
  }
}

run "locked_has_no_writer_or_reader" {
  command = plan
  variables {
    enable_collector_custody = true
    collector_custody_phases = { registration = "locked", transfer = "locked" }
  }
  assert {
    condition     = length(google_secret_manager_secret_iam_member.collector_generator) == 0 && length(google_secret_manager_secret_iam_member.collector_reader) == 0 && length(google_compute_firewall.collector_bootstrap) == 0
    error_message = "Locked phase must remove writer authority before the separate sealed apply."
  }
}

run "sealed_only_matches_numeric_project_first_versions" {
  command = plan
  variables {
    enable_collector_custody = true
    collector_custody_phases = { registration = "sealed", transfer = "sealed" }
  }
  assert {
    condition     = length(google_secret_manager_secret_iam_member.collector_generator) == 0 && length(google_secret_manager_secret_iam_member.collector_reader) == 2
    error_message = "Sealed identity must no longer add versions."
  }
  assert {
    condition     = alltrue([for role, binding in google_secret_manager_secret_iam_member.collector_reader : binding.condition[0].expression == "resource.name == 'projects/123456/secrets/sn118-collector-${role}-delegate/versions/1'" && binding.role == "roles/secretmanager.secretAccessor"])
    error_message = "Read access must be limited to own numeric-project version 1."
  }
}

run "sealed_runtime_rpc_is_one_tls_destination_without_public_hosts" {
  command = plan
  variables {
    enable_collector_custody     = true
    collector_custody_phases     = { registration = "sealed", transfer = "sealed" }
    collector_runtime_rpc_egress = true
  }
  assert {
    condition     = length(google_compute_router_nat.collector_custody) == 1 && length(google_compute_router.collector_custody) == 1 && length(google_compute_firewall.collector_bootstrap) == 0
    error_message = "Runtime NAT must not reintroduce bootstrap internet grants."
  }
  assert {
    condition     = google_compute_firewall.collector_runtime_rpc[0].disabled == false
    error_message = "The private plan must explicitly prove the reviewed RPC rule is enabled."
  }
  assert {
    condition     = google_compute_firewall.collector_runtime_rpc[0].destination_ranges == toset(["65.109.251.221/32"]) && google_compute_firewall.collector_runtime_rpc[0].target_tags == toset(["collector-registration-sealed", "collector-transfer-sealed"]) && one(google_compute_firewall.collector_runtime_rpc[0].allow).protocol == "tcp" && toset(one(google_compute_firewall.collector_runtime_rpc[0].allow).ports) == toset(["443"]) && google_compute_firewall.collector_deny_private[0].priority < google_compute_firewall.collector_runtime_rpc[0].priority && google_compute_firewall.collector_deny_other[0].priority > google_compute_firewall.collector_runtime_rpc[0].priority
    error_message = "Only exact Finney TLS must be reachable by sealed roles; private and other traffic stays denied."
  }
  assert {
    condition     = alltrue([for host in google_compute_instance.collector_delegate : length(host.network_interface[0].access_config) == 0]) && length(google_secret_manager_secret_iam_member.collector_generator) == 0 && alltrue([for role, binding in google_secret_manager_secret_iam_member.collector_reader : binding.condition[0].expression == "resource.name == 'projects/123456/secrets/sn118-collector-${role}-delegate/versions/1'"])
    error_message = "Runtime network must preserve private hosts and own-version-only reader authority."
  }
}

run "runtime_rpc_refuses_unsealed_role" {
  command = plan
  variables {
    enable_collector_custody     = true
    collector_custody_phases     = { registration = "sealed", transfer = "armed" }
    collector_runtime_rpc_egress = true
  }
  expect_failures = [var.collector_runtime_rpc_egress]
}

run "runtime_rpc_refuses_disabled_custody" {
  command = plan
  variables { collector_runtime_rpc_egress = true }
  expect_failures = [var.collector_runtime_rpc_egress]
}

run "active_missing_revision_refused" {
  command = plan
  variables {
    enable_collector_custody   = true
    collector_custody_revision = ""
  }
  expect_failures = [check.collector_custody_explicit_inputs, google_compute_instance.collector_delegate]
}

run "duplicate_offline_addresses_refused" {
  command = plan
  variables {
    collector_custody_offline_addresses = [
      "5Fhnko5TCPtR1hyD8aua6n23XRM8cxcBM6TgNEqqUhcxWs1t",
      "5Fhnko5TCPtR1hyD8aua6n23XRM8cxcBM6TgNEqqUhcxWs1t",
    ]
  }
  expect_failures = [var.collector_custody_offline_addresses]
}

run "wrong_project_refused" {
  command = plan
  variables { project = "other-project" }
  expect_failures = [var.project]
}
run "wrong_region_refused" {
  command = plan
  variables { region = "europe-west1" }
  expect_failures = [var.region]
}
run "wrong_zone_refused" {
  command = plan
  variables { zone = "us-central1-b" }
  expect_failures = [var.zone]
}
run "legacy_treasury_refused" {
  command = plan
  variables { enable_treasury_host = true }
  expect_failures = [var.enable_treasury_host]
}
run "malformed_source_refused" {
  command = plan
  variables { collector_custody_revision = "main" }
  expect_failures = [var.collector_custody_revision]
}
run "rendered_source_and_all_public_bindings_match" {
  command = plan
  variables { enable_collector_custody = true }
  assert {
    condition     = alltrue([for role, host in google_compute_instance.collector_delegate : strcontains(host.metadata["startup-script"], var.collector_custody_revision) && alltrue([for address in var.collector_custody_offline_addresses : strcontains(host.metadata["startup-script"], address)])])
    error_message = "Both roles must render the exact selected source and all five public identities."
  }
}
