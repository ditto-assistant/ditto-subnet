"""Source contracts supplement credential-free Terraform mocked plan tests."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
MODULE = ROOT / "infra/terraform/modules/coding-hosted-host"
STACK = ROOT / "infra/terraform/stacks/gcp-platform"
SOURCE = (MODULE / "main.tf").read_text()
VARIABLES = (MODULE / "variables.tf").read_text()
DOC = (ROOT / "infra/docs/coding-hosted-host-v2.md").read_text()


def test_native_foundation_is_independent_and_default_off() -> None:
    entry = (STACK / "coding-hosted.tf").read_text()
    prod = (STACK / "prod.auto.tfvars").read_text()
    assert 'source    = "../../modules/coding-hosted-host"' in entry
    assert 'variable "enable_coding_hosted_host"' in entry
    assert "default     = false" in entry
    assert "coding_executor_host_count = 0" in prod
    assert "var.coding_executor" not in entry + SOURCE + VARIABLES
    assert 'role    = "coding_hosted"' in SOURCE
    assert 'owner   = "platform"' in SOURCE
    assert "var.enabled ? var.operators : toset([])" in SOURCE


def test_production_intent_names_one_custodian_with_database_path() -> None:
    prod = (STACK / "prod.auto.tfvars").read_text()
    assert re.search(r"(?m)^enable_coding_hosted_host\s*=\s*true\s*$", prod)
    assert re.search(
        r'(?m)^coding_hosted_operators\s*=\s*\["user:brian@omniaura\.ai"\]\s*$',
        prod,
    )
    assert re.search(r"(?m)^enable_coding_hosted_postgres\s*=\s*true\s*$", prod)
    assert "coding_executor_host_count = 0" in prod
    # The reusable module still refuses activation without an explicit override.
    assert re.search(r'variable "enabled"\s*\{[^}]*default\s*=\s*false', VARIABLES)


def test_all_native_resources_are_conditional() -> None:
    blocks = re.split(r'\n(?:resource|module) "', SOURCE)[1:]
    assert len(blocks) == 19
    for block in blocks:
        assert re.search(
            r"(?:count\s*= var.enabled \? 1 : 0|"
            r"for_each\s*= var.enabled \? toset\(|"
            r"for_each\s*= local.(?:workflow_)?operators\n|"
            r"count\s*= length\(local.workflow_operators\)\n)",
            block,
        ), block.splitlines()[0]


def test_runtime_identity_cannot_access_private_data() -> None:
    roles = set(re.findall(r'"(roles/[^\"]+)"', SOURCE))
    assert roles == {
        "roles/logging.logWriter",
        "roles/monitoring.metricWriter",
        "roles/compute.osAdminLogin",
        "roles/iap.tunnelResourceAccessor",
        "roles/iam.serviceAccountUser",
    }
    for forbidden in (
        "google_secret_manager",
        "google_storage",
        "google_artifact_registry",
        "google_compute_network_peering",
        "google_compute_route",
        "metadata_startup_script",
        "user_data",
        "local-exec",
        "remote-exec",
    ):
        # Router and router_nat are ordinary scoped NAT, not private routes.
        assert not re.search(rf"\b{forbidden}\b", SOURCE)


def test_vm_uses_existing_protected_compute_module() -> None:
    assert 'source   = "../compute/gcp"' in SOURCE
    for field in (
        "enable_os_login",
        "enable_secure_boot",
        "enable_vtpm",
        "enable_integrity_monitoring",
    ):
        assert re.search(rf"{field}\s*= true", SOURCE)
    assert re.search(r"assign_public_ip\s*= false", SOURCE)
    assert re.search(
        r"service_account_email\s*= google_service_account.host\[0\].email",
        SOURCE,
    )
    compute = (ROOT / "infra/terraform/modules/compute/gcp/main.tf").read_text()
    assert "deletion_protection = true" in compute
    assert "prevent_destroy = true" in compute
    for rule in ("deny_private", "host_web", "deny_other_egress"):
        assert f"google_compute_firewall.{rule}," in SOURCE


def test_operators_are_explicit_and_destination_scoped() -> None:
    assert "!var.enabled || length(var.operators) > 0" in VARIABLES
    assert 'variable "operators"' in VARIABLES and "default     = []" in VARIABLES
    assert 'resource "google_project_iam_member" "ssh"' in SOURCE
    assert (
        "expression  = \"destination.ip == '${module.host[0].internal_ip}' "
        '&& destination.port == 22"' in SOURCE
    )
    assert "resource.name.extract(" not in SOURCE
    assert 'resource "google_iap_tunnel_instance_iam_member" "ssh"' not in SOURCE
    assert "roles/compute.viewer" not in SOURCE


def test_workflow_operator_is_one_service_account_and_never_a_custodian() -> None:
    assert 'variable "workflow_operator"' in VARIABLES
    assert "^serviceAccount:" in VARIABLES
    assert (
        'var.enabled && var.workflow_operator != "" '
        "? toset([var.workflow_operator]) : toset([])" in SOURCE
    )
    for resource in (
        'google_compute_instance_iam_member" "workflow_osadmin',
        'google_project_iam_member" "workflow_ssh',
        'google_project_iam_member" "workflow_project_get',
        'google_service_account_iam_member" "workflow_actas',
    ):
        block = SOURCE.split(f'"{resource}" {{', 1)[1].split("\n}", 1)[0]
        assert re.search(r"for_each\s*= local.workflow_operators\n", block)
    ssh = SOURCE.split('"workflow_ssh" {', 1)[1].split("\n}", 1)[0]
    assert (
        "expression  = \"destination.ip == '${module.host[0].internal_ip}' "
        '&& destination.port == 22"' in ssh
    )
    # gcloud compute ssh needs compute.projects.get; grant only that, never viewer.
    role = SOURCE.split(
        'resource "google_project_iam_custom_role" "workflow_project_get" {', 1
    )[1].split("\n}", 1)[0]
    assert re.search(r"count\s*= length\(local.workflow_operators\)\n", role)
    assert re.findall(r"permissions\s*= (\[.*\])", role) == ['["compute.projects.get"]']
    assert SOURCE.count("google_project_iam_custom_role") == 2
    assert SOURCE.count("permissions") == 1
    binding = SOURCE.split('"google_project_iam_member" "workflow_project_get" {', 1)[
        1
    ].split("\n}", 1)[0]
    assert "role     = google_project_iam_custom_role.workflow_project_get[0].name" in (
        binding
    )
    assert "condition" not in binding
    # Service accounts still cannot become human custodians.
    assert "not public, group, domain, or service-account principals" in VARIABLES


def test_tests_are_mocked_plan_only_and_run_in_ci() -> None:
    tests = (MODULE / "tests/host.tftest.hcl").read_text()
    assert 'mock_provider "google" {}' in tests
    assert len(re.findall(r'^run "', tests, re.MULTILINE)) == 20
    assert len(re.findall(r"command\s*= plan", tests)) == 20
    assert "command = apply" not in tests
    assert "expect_failures = [var.operators]" in tests
    assert "expect_failures = [var.workflow_operator]" in tests
    assert (
        'run "workflow_operator_is_destination_scoped_and_never_a_custodian"' in tests
    )
    assert 'run "workflow_project_read_is_one_custom_permission"' in tests
    assert 'run "custodians_do_not_receive_workflow_project_read"' in tests
    assert "expect_failures = [var.boot_disk_gb]" in tests
    assert 'run "iap_follows_resolved_private_address"' in tests
    workflow = (ROOT / ".github/workflows/infra-ci.yml").read_text()
    assert "terraform/modules/coding-hosted-host test" in workflow
    assert "terraform/modules/coding-hosted-host init -backend=false" in workflow


def test_doc_does_not_claim_host_is_execution_authority() -> None:
    normalized = " ".join(DOC.split())
    for boundary in (
        "not a deployed worker",
        "not a candidate sandbox",
        "metadata-server",
        "no private PostgreSQL route",
        "Hippius stays the sole",
        "`0600`",
        "`0700`",
        "`0660`",
        "decommission",
    ):
        assert boundary in normalized
