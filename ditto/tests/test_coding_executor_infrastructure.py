"""Regression checks for the dormant dedicated coding-executor cohort."""

from pathlib import Path

ROOT = Path(__file__).parents[2]
STACK = ROOT / "infra/terraform/stacks/gcp-platform"
SOURCE = (STACK / "coding-executor.tf").read_text()
PROD_TFVARS = (STACK / "prod.auto.tfvars").read_text()
DOC = (ROOT / "infra/docs/coding-executor-hosts.md").read_text()
WORKER_DOC = (ROOT / "docs/coding-shadow-worker.md").read_text()


def test_coding_executor_cohort_is_absent_by_default_and_fixed_at_k3() -> None:
    assert 'variable "coding_executor_host_count"' in SOURCE
    assert "default     = 0" in SOURCE
    assert "coding_executor_host_count == 0 ||" in SOURCE
    assert "coding_executor_host_count == 3" in SOURCE
    assert "coding_executor_host_count = 0" in PROD_TFVARS
    assert "count    = var.coding_executor_host_count" in SOURCE


def test_coding_executor_hosts_are_private_and_secret_free() -> None:
    assert 'account_id   = "ditto-coding-executor"' in SOURCE
    assert '"roles/logging.logWriter"' in SOURCE
    assert '"roles/monitoring.metricWriter"' in SOURCE
    assert "google_secret_manager" not in SOURCE
    assert "roles/secretmanager" not in SOURCE
    assert "google_artifact_registry" not in SOURCE
    assert "assign_public_ip = false" in SOURCE
    assert "enable_secure_boot          = true" in SOURCE
    assert "enable_vtpm                 = true" in SOURCE
    assert "enable_integrity_monitoring = true" in SOURCE


def test_coding_executor_boundary_requires_separate_runtime_activation() -> None:
    assert 'role    = "coding_executor"' in SOURCE
    assert "coding_executor_deny_private_egress" in SOURCE
    assert "var.subnet_cidr, var.validator_prod_subnet_cidr" in SOURCE
    assert "rootless daemon" in DOC
    assert "does not install Docker" in DOC
    assert "does not" in DOC and "enable a coding gate" in DOC
    assert "coding-executor-hosts.md" in WORKER_DOC
