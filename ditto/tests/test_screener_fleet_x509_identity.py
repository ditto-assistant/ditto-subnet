"""Least-privilege contracts for the bare-metal screener Google identity."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
TERRAFORM = ROOT / "infra/terraform/stacks/gcp-platform/screener-fleet-x509.tf"
ROLE = ROOT / "infra/ansible/roles/hetzner_screener_fleet"
INFRA_WORKFLOW = ROOT / ".github/workflows/infra-plan-apply.yml"
SUBJECT = "spiffe://dittobench.ai/screener/subnet-screener-1"


def test_terraform_binds_one_exact_x509_subject_to_one_secret() -> None:
    terraform = TERRAFORM.read_text()

    assert f'screener_fleet_x509_subject = "{SUBJECT}"' in terraform
    assert '"google.subject" = "assertion.san.uri"' in terraform
    assert "assertion.san.uri == '${local.screener_fleet_x509_subject}'" in terraform
    assert 'role      = "roles/secretmanager.secretAccessor"' in terraform
    assert 'secret_id = "validator-openrouter-key"' in terraform
    assert 'role               = "roles/iam.workloadIdentityUser"' in terraform
    assert "principalSet://" not in terraform
    project_grants = re.findall(
        r'resource "google_project_iam_member" "([^"]+)" \{(.*?)\n\}',
        terraform,
        flags=re.DOTALL,
    )
    assert project_grants == [
        (
            "screener_fleet_x509_pool_admin",
            "\n  count   = local.screener_fleet_x509_count\n"
            "  project = var.project\n"
            '  role    = "roles/iam.workloadIdentityPoolAdmin"\n'
            '  member  = "serviceAccount:github-actions-terraform-apply@'
            '${var.project}.iam.gserviceaccount.com"',
        )
    ]
    assert 'resource "google_service_account_key"' not in terraform
    assert "PRIVATE KEY" not in terraform


def test_host_generates_private_key_and_never_accepts_it_as_input() -> None:
    tasks = (ROLE / "tasks/main.yml").read_text()
    defaults = (ROLE / "defaults/main.yml").read_text()

    assert "openssl\n      - genpkey" in tasks
    assert 'creates: "{{ screener_fleet_x509_private_key_file }}"' in tasks
    assert 'mode: "0400"' in tasks
    assert "screener_fleet_x509_private_key_pem" not in defaults
    assert "'PRIVATE KEY' not in screener_fleet_x509_ca_certificate_pem" in tasks
    assert "'PRIVATE KEY' not in screener_fleet_x509_certificate_pem" in tasks


def test_x509_credential_renderer_is_idempotent() -> None:
    tasks = (ROLE / "tasks/main.yml").read_text()

    assert 'creates: "{{ screener_fleet_x509_credential_file }}"' in tasks


def test_repeat_converge_reuses_verified_installed_public_certificates() -> None:
    tasks = (ROLE / "tasks/main.yml").read_text()

    assert "Inspect the installed X.509 public trust anchor" in tasks
    assert "Inspect the installed X.509 client certificate" in tasks
    assert "screener_fleet_x509_ca_certificate_state.stat.isreg" in tasks
    assert "screener_fleet_x509_certificate_state.stat.isreg" in tasks
    assert tasks.count("screener_fleet_x509_ca_certificate_pem | length > 0") == 1
    assert tasks.count("screener_fleet_x509_certificate_pem | length > 0") == 1
    for task_name in (
        "Verify the client certificate chain and purpose",
        "Require at least fourteen days of client certificate validity",
        "Read the client certificate URI SAN",
        "Read the private-key public modulus",
        "Read the certificate public modulus",
    ):
        task = tasks[tasks.index(f"- name: {task_name}") :]
        task = task[: task.index("\n- name:")]
        assert "check_mode: false" in task


def test_signed_updater_installs_the_split_debian_docker_cli() -> None:
    tasks = (ROLE / "tasks/main.yml").read_text()

    assert "Install the Docker CLI split from docker.io on Debian 13" in tasks
    assert "    name: docker-cli" in tasks
    assert "when: ansible_facts['distribution_major_version'] == '13'" in tasks


def test_materializer_reads_only_named_secret_without_printing_it() -> None:
    script = (ROLE / "templates/materialize-source-review-secret.sh.j2").read_text()
    service = (
        ROLE / "templates/ditto-screener-source-review-secret.service.j2"
    ).read_text()

    assert "gcloud secrets versions access latest" in script
    assert "--secret={{ screener_fleet_source_review_secret_id | quote }}" in script
    assert '>"$temporary"' in script
    assert 'test -s "$temporary"' in script
    assert "User={{ screener_fleet_secret_agent_user }}" in service
    assert "User=root" not in service
    assert "PrivateDevices=true" in service
    assert "CapabilityBoundingSet=" in service
    assert "ReadWritePaths=" in service


def test_signing_services_have_a_private_writable_bittensor_home() -> None:
    defaults = (ROLE / "defaults/main.yml").read_text()
    tasks = (ROLE / "tasks/main.yml").read_text()
    enrollment = (ROLE / "templates/ditto-screener-enroll.service.j2").read_text()
    worker = (ROLE / "templates/ditto-screener-worker@.service.j2").read_text()

    assert (
        'screener_fleet_bittensor_dir: "{{ screener_fleet_root }}/.bittensor"'
        in defaults
    )
    assert '- { path: "{{ screener_fleet_bittensor_dir }}", mode: "0700" }' in tasks
    assert "{{ screener_fleet_bittensor_dir }}" in enrollment
    assert "{{ screener_fleet_bittensor_dir }}" in worker


def test_fleet_build_timeout_satisfies_worker_contract() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())

    assert 300 <= defaults["screener_fleet_build_timeout_seconds"] <= 2400


def test_disposable_guest_base_survives_libvirt_ownership_changes() -> None:
    tasks = (ROLE / "tasks/main.yml").read_text()

    assert (
        "Keep the public guest base readable after libvirt ownership changes" in tasks
    )
    assert 'path: "{{ screener_fleet_base_image }}"' in tasks
    assert 'mode: "0644"' in tasks


def test_infra_workflow_keeps_x509_identity_opt_in() -> None:
    workflow = yaml.safe_load(INFRA_WORKFLOW.read_text())
    input_config = workflow[True]["workflow_dispatch"]["inputs"][
        "screener_fleet_x509_identity_enabled"
    ]

    assert input_config["default"] is False
    text = INFRA_WORKFLOW.read_text()
    assert "SCREENER_FLEET_X509_CA_CERTIFICATE_PEM" in text
    assert "enable_screener_fleet_x509_identity" in text
    assert "SERVICE_ACCOUNT_KEY" not in text
