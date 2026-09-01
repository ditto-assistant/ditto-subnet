from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
GCP_ROOT = ROOT / "infra" / "terraform" / "stacks" / "gcp-platform"
TERRAFORM = GCP_ROOT / "coding-storage.tf"


def _resource_body(source: str, resource_type: str, name: str) -> str:
    marker = f'resource "{resource_type}" "{name}"'
    return source.split(marker, 1)[1].split("\n}\n", 1)[0]


def test_coding_storage_authorities_are_absent_by_default() -> None:
    variables = (GCP_ROOT / "variables.tf").read_text()
    intent = (GCP_ROOT / "prod.auto.tfvars").read_text()
    terraform = TERRAFORM.read_text()

    declaration = variables.split('variable "enable_coding_s3_authorities"', 1)[
        1
    ].split("\n}\n", 1)[0]
    assert "default     = false" in declaration
    assert re.search(
        r"^enable_coding_s3_authorities\s*=\s*false$", intent, re.MULTILINE
    )
    assert (
        "coding_storage_envs = var.enable_coding_s3_authorities "
        "? local.app_envs : {}" in terraform
    )
    assert terraform.count("var.enable_coding_s3_authorities ? 1 : 0") == 4


def test_coding_buckets_are_separate_private_retained_authorities() -> None:
    terraform = TERRAFORM.read_text()
    private = _resource_body(
        terraform, "google_storage_bucket", "coding_private_inputs"
    )
    evidence = _resource_body(
        terraform, "google_storage_bucket", "coding_sealed_evidence"
    )

    assert "coding-private-inputs" in private
    assert "coding-sealed-evidence" in evidence
    for body, retention in (
        (private, "var.coding_private_input_retention_seconds"),
        (evidence, "var.coding_sealed_evidence_retention_seconds"),
    ):
        assert 'public_access_prevention    = "enforced"' in body
        assert "uniform_bucket_level_access = true" in body
        assert "enabled = true" in body
        assert f"retention_period = {retention}" in body
        assert "is_locked        = false" in body
        assert "prevent_destroy = true" in body
        assert 'type = "AbortIncompleteMultipartUpload"' in body
        assert 'type = "Delete"' not in body


def test_runtime_identities_cannot_list_overwrite_or_delete() -> None:
    terraform = TERRAFORM.read_text()
    reader_role = _resource_body(
        terraform, "google_project_iam_custom_role", "coding_private_input_reader"
    )
    finalizer_role = _resource_body(
        terraform, "google_project_iam_custom_role", "coding_evidence_finalizer"
    )

    assert 'permissions = ["storage.objects.get"]' in reader_role
    assert '"storage.objects.create"' in finalizer_role
    assert '"storage.objects.get"' in finalizer_role
    for body in (reader_role, finalizer_role):
        assert "storage.objects.list" not in body
        assert "storage.objects.delete" not in body
        assert "storage.objects.update" not in body

    curator = _resource_body(
        terraform, "google_storage_bucket_iam_member", "coding_private_input_curator"
    )
    reader = _resource_body(
        terraform, "google_storage_bucket_iam_member", "coding_private_input_reader"
    )
    finalizer = _resource_body(
        terraform, "google_storage_bucket_iam_member", "coding_evidence_finalizer"
    )
    assert 'role   = "roles/storage.objectCreator"' in curator
    assert "google_storage_bucket.coding_private_inputs" in curator
    assert "google_storage_bucket.coding_private_inputs" in reader
    assert "google_storage_bucket.coding_sealed_evidence" not in reader
    assert "google_storage_bucket.coding_sealed_evidence" in finalizer
    assert "google_storage_bucket.coding_private_inputs" not in finalizer
    assert "roles/storage.objectAdmin" not in terraform
    assert "roles/storage.objectUser" not in terraform


def test_hmac_secrets_and_platform_binding_are_independently_gated() -> None:
    variables = (GCP_ROOT / "variables.tf").read_text()
    intent = (GCP_ROOT / "prod.auto.tfvars").read_text()
    terraform = TERRAFORM.read_text()

    for identity in (
        "coding_private_input_curator",
        "coding_private_input_reader",
        "coding_evidence_finalizer",
    ):
        assert f'resource "google_storage_hmac_key" "{identity}"' in terraform
        assert (
            f'resource "google_secret_manager_secret_version" "{identity}_hmac"'
            in terraform
        )

    binding = variables.split('variable "enable_coding_storage_platform_binding"', 1)[
        1
    ].split("\n}\n", 1)[0]
    assert "default     = false" in binding
    assert re.search(
        r"^enable_coding_storage_platform_binding\s*=\s*false$",
        intent,
        re.MULTILINE,
    )
    assert "coding_storage_platform_binding_requires_authorities" in terraform
    assert "var.platform_dedicated_identity_attached" in terraform

    reader_binding = _resource_body(
        terraform,
        "google_secret_manager_secret_iam_member",
        "coding_private_input_reader_platform",
    )
    evidence_binding = _resource_body(
        terraform,
        "google_secret_manager_secret_iam_member",
        "coding_evidence_finalizer_platform",
    )
    for body in (reader_binding, evidence_binding):
        assert "for_each = local.coding_platform_binding_envs" in body
        assert 'role      = "roles/secretmanager.secretAccessor"' in body
        assert "local.platform_api_sa_email" in body
        assert "coding_private_input_curator_hmac" not in body

    assert "google_service_account.ditto_platform" not in terraform
    assert "coding_private_input_reader_hmac" in reader_binding
    assert "coding_evidence_finalizer_hmac" in evidence_binding


def test_storage_data_access_audit_is_tied_to_the_disabled_gate() -> None:
    terraform = TERRAFORM.read_text()
    audit = _resource_body(
        terraform, "google_project_iam_audit_config", "coding_storage"
    )

    assert "count = var.enable_coding_s3_authorities ? 1 : 0" in audit
    assert 'service = "storage.googleapis.com"' in audit
    assert 'log_type = "DATA_READ"' in audit
    assert 'log_type = "DATA_WRITE"' in audit


def test_s3_compatible_transport_is_https_only_when_enabled() -> None:
    terraform = TERRAFORM.read_text()
    policy = _resource_body(
        terraform,
        "google_project_organization_policy",
        "coding_storage_secure_transport",
    )

    assert "count = var.enable_coding_s3_authorities ? 1 : 0" in policy
    assert 'constraint = "storage.secureHttpTransport"' in policy
    assert "enforced = true" in policy
