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


def test_production_intent_names_one_custodian_without_database_activation() -> None:
    prod = (STACK / "prod.auto.tfvars").read_text()
    assert re.search(r"(?m)^enable_coding_hosted_host\s*=\s*true\s*$", prod)
    assert re.search(
        r'(?m)^coding_hosted_operators\s*=\s*\["user:brian@omniaura\.ai"\]\s*$',
        prod,
    )
    assert re.search(r"(?m)^enable_coding_hosted_postgres\s*=\s*false\s*$", prod)
    assert "coding_executor_host_count = 0" in prod
    # The reusable module still refuses activation without an explicit override.
    assert re.search(r'variable "enabled"\s*\{[^}]*default\s*=\s*false', VARIABLES)


def test_all_native_resources_are_conditional() -> None:
    blocks = re.split(r'\n(?:resource|module) "', SOURCE)[1:]
    assert len(blocks) == 14
    for block in blocks:
        assert re.search(
            r"(?:count\s*= var.enabled \? 1 : 0|"
            r"for_each\s*= var.enabled \? toset\(|for_each\s*= local.operators)",
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


def test_operators_are_explicit_and_instance_scoped() -> None:
    assert "!var.enabled || length(var.operators) > 0" in VARIABLES
    assert 'variable "operators"' in VARIABLES and "default     = []" in VARIABLES
    assert 'resource "google_iap_tunnel_instance_iam_member" "ssh"' in SOURCE
    assert 'expression  = "destination.port == 22"' in SOURCE
    assert 'resource "google_project_iam_member" "ssh"' not in SOURCE
    assert "roles/compute.viewer" not in SOURCE


def test_tests_are_mocked_plan_only_and_run_in_ci() -> None:
    tests = (MODULE / "tests/host.tftest.hcl").read_text()
    assert 'mock_provider "google" {}' in tests
    assert len(re.findall(r'^run "', tests, re.MULTILINE)) == 10
    assert len(re.findall(r"command\s*= plan", tests)) == 10
    assert "command = apply" not in tests
    assert "expect_failures = [var.operators]" in tests
    assert "expect_failures = [var.boot_disk_gb]" in tests
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
