from __future__ import annotations

import importlib.util
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "infra/ansible/scripts/generate_validator_hotkey.py"
MATERIALIZER = (
    ROOT / "infra/ansible/roles/validator_stack/templates/materialize_wallet.py.j2"
)
ENV_TEMPLATE = ROOT / "infra/ansible/roles/validator_stack/templates/validator.env.j2"
PROD_TASKS = ROOT / "infra/ansible/roles/validator_stack/tasks/main.yml"
PROD_TERRAFORM = ROOT / "infra/terraform/stacks/gcp-platform/validator-prod.tf"
VALIDATOR_TERRAFORM = ROOT / "infra/terraform/stacks/gcp-platform/validator.tf"
ADMIN_TERRAFORM = ROOT / "infra/terraform/stacks/gcp-platform/validator-hotkey-admin.tf"
ADMIN_STARTUP = (
    ROOT
    / "infra/terraform/stacks/gcp-platform/files/validator-hotkey-admin-startup.sh.tpl"
)
PROD_TFVARS = ROOT / "infra/terraform/stacks/gcp-platform/prod.auto.tfvars"
INFRA_WORKFLOW = ROOT / ".github/workflows/infra-plan-apply.yml"


def _load_module():
    spec = importlib.util.spec_from_file_location("generate_validator_hotkey", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generator_streams_mnemonic_only_to_secret_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    mnemonic = "sensitive mnemonic that must never be printed"
    calls: list[tuple[list[str], str | None]] = []

    class FakeKeypair:
        ss58_address = "5" + "A" * 47

        @staticmethod
        def generate_mnemonic(*, n_words: int) -> str:
            assert n_words == 24
            return mnemonic

        @staticmethod
        def create_from_mnemonic(value: str) -> FakeKeypair:
            assert value == mnemonic
            return FakeKeypair()

    def fake_run(
        command: list[str],
        *,
        input: str | None,
        text: bool,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert text and capture_output and not check
        calls.append((command, input))
        if command[1:4] == ["secrets", "versions", "list"]:
            stdout = ""
        elif command[1:4] == ["secrets", "versions", "add"]:
            stdout = "projects/p/secrets/s/versions/1\n"
        else:
            stdout = "projects/p/secrets/s\n"
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(module.bt, "Keypair", FakeKeypair)
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--lock-file",
            str(tmp_path / "generator.lock"),
            "--confirm",
            module.CONFIRMATION,
        ],
    )

    assert module.main() == 0
    output = capsys.readouterr()
    assert FakeKeypair.ss58_address in output.out
    assert "secret_version=1" in output.out
    assert "versions/1" not in output.out
    assert mnemonic not in output.out
    assert mnemonic not in output.err
    assert all(mnemonic not in " ".join(command) for command, _ in calls)
    assert [stdin for _, stdin in calls] == [None, None, f"{mnemonic}\n"]


def test_generator_refuses_to_replace_any_existing_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()

    def fake_run(
        command: list[str],
        *,
        input: str | None,
        text: bool,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        del input, text, capture_output, check
        stdout = (
            "1\n" if command[1:4] == ["secrets", "versions", "list"] else "secret\n"
        )
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--lock-file",
            str(tmp_path / "generator.lock"),
            "--confirm",
            module.CONFIRMATION,
        ],
    )

    with pytest.raises(RuntimeError, match="already has a version"):
        module.main()


def test_generator_suppresses_gcloud_stderr_that_could_echo_the_mnemonic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    mnemonic = "sensitive mnemonic echoed by a failed dependency"

    class FakeKeypair:
        ss58_address = "5" + "Z" * 47

        @staticmethod
        def generate_mnemonic(*, n_words: int) -> str:
            assert n_words == 24
            return mnemonic

        @staticmethod
        def create_from_mnemonic(value: str) -> FakeKeypair:
            assert value == mnemonic
            return FakeKeypair()

    def fake_run(
        command: list[str],
        *,
        input: str | None,
        text: bool,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        del text, capture_output, check
        if command[1:4] == ["secrets", "versions", "list"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1:4] == ["secrets", "versions", "add"]:
            assert input == f"{mnemonic}\n"
            return subprocess.CompletedProcess(command, 1, "", mnemonic)
        return subprocess.CompletedProcess(command, 0, "secret", "")

    monkeypatch.setattr(module.bt, "Keypair", FakeKeypair)
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--lock-file",
            str(tmp_path / "generator.lock"),
            "--confirm",
            module.CONFIRMATION,
        ],
    )

    with pytest.raises(RuntimeError) as error:
        module.main()
    assert mnemonic not in str(error.value)


def test_generator_persists_and_replays_only_public_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    mnemonic = "mnemonic must not enter the result file"
    hotkey = "5" + "B" * 47
    version_name = "projects/p/secrets/s/versions/7"
    version = "7"
    result_file = tmp_path / "result.env"
    generated = 0
    versions: list[str] = []

    class FakeKeypair:
        ss58_address = hotkey

        @staticmethod
        def generate_mnemonic(*, n_words: int) -> str:
            nonlocal generated
            assert n_words == 24
            generated += 1
            return mnemonic

        @staticmethod
        def create_from_mnemonic(value: str) -> FakeKeypair:
            assert value == mnemonic
            return FakeKeypair()

    def fake_run(
        command: list[str],
        *,
        input: str | None,
        text: bool,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        del text, capture_output, check
        if command[1:4] == ["secrets", "versions", "list"]:
            stdout = "\n".join(versions)
        elif command[1:4] == ["secrets", "versions", "add"]:
            assert input == f"{mnemonic}\n"
            versions.append(version_name)
            stdout = version_name
        else:
            assert input is None
            stdout = "projects/p/secrets/s"
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(module.bt, "Keypair", FakeKeypair)
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--result-file",
            str(result_file),
            "--lock-file",
            str(tmp_path / "generator.lock"),
            "--confirm",
            module.CONFIRMATION,
        ],
    )

    assert module.main() == 0
    assert generated == 1
    result = result_file.read_text()
    assert hotkey in result
    assert version in result
    assert mnemonic not in result
    assert result_file.stat().st_mode & 0o777 == 0o600

    capsys.readouterr()
    assert module.main() == 0
    replay = capsys.readouterr()
    assert generated == 1
    assert hotkey in replay.out
    assert version in replay.out
    assert mnemonic not in replay.out


def test_generator_recovers_public_result_after_post_add_disconnect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    hotkey = "5" + "C" * 47
    version_name = "projects/p/secrets/s/versions/9"
    version = "9"
    result_file = tmp_path / "result.env"
    result_file.write_text(f"validator_hotkey={hotkey}\nsecret_version=pending\n")

    def fake_run(
        command: list[str],
        *,
        input: str | None,
        text: bool,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        del input, text, capture_output, check
        assert command[1:4] != ["secrets", "versions", "add"]
        stdout = (
            version_name
            if command[1:4] == ["secrets", "versions", "list"]
            else "projects/p/secrets/s"
        )
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--result-file",
            str(result_file),
            "--lock-file",
            str(tmp_path / "generator.lock"),
            "--confirm",
            module.CONFIRMATION,
        ],
    )

    assert module.main() == 0
    output = capsys.readouterr()
    assert hotkey in output.out
    assert version in output.out
    assert "pending" not in result_file.read_text()


def test_materializer_writes_only_the_hotkey_and_verifies_expected_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bittensor as bt

    mnemonic = bt.Keypair.generate_mnemonic(n_words=24)
    expected = bt.Keypair.create_from_mnemonic(mnemonic).ss58_address
    wallet_path = tmp_path / "wallets"
    environment = {
        "VALIDATOR_MNEMONIC": mnemonic,
        "EXPECTED_HOTKEY": expected,
        "BT_WALLET_PATH": str(wallet_path),
        "BT_WALLET_NAME": "validator",
        "BT_HOTKEY_NAME": "gcp-prod",
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    runpy.run_path(str(MATERIALIZER), run_name="__main__")

    hotkey_file = wallet_path / "validator/hotkeys/gcp-prod"
    assert hotkey_file.is_file()
    assert not (wallet_path / "validator/coldkey").exists()

    monkeypatch.delenv("VALIDATOR_MNEMONIC")
    monkeypatch.setenv("VERIFY_ONLY", "1")
    runpy.run_path(str(MATERIALIZER), run_name="__main__")


def test_production_environment_never_contains_signing_seed_or_coldkey() -> None:
    template = ENV_TEMPLATE.read_text()

    assert "MNEMONIC" not in template
    assert "COLDKEY" not in template
    assert "VALIDATOR_MNEMONIC" not in template


def test_production_wandb_key_uses_secret_manager_without_terraform_value() -> None:
    template = ENV_TEMPLATE.read_text()
    tasks = PROD_TASKS.read_text()
    production_terraform = PROD_TERRAFORM.read_text()
    shared_terraform = VALIDATOR_TERRAFORM.read_text()

    assert "WANDB_MODE=online" in template
    assert "WANDB_API_KEY={{ validator_stack_wandb_api_key }}" in template
    assert "WANDB_PROJECT=" not in template
    assert "WANDB_ENTITY=" not in template

    read = tasks.split("- name: Read the W&B API key from Secret Manager", 1)[1]
    read = read.split("- name: Require a non-empty single-line W&B API key", 1)[0]
    assert "validator_stack_wandb_secret" in read
    assert "latest" in read
    assert "no_log: true" in read

    access = production_terraform.split(
        'resource "google_secret_manager_secret_iam_member" '
        '"validator_prod_wandb_access"',
        1,
    )[1].split("resource ", 1)[0]
    assert "google_secret_manager_secret.validator_wandb_key[0].secret_id" in access
    assert 'role      = "roles/secretmanager.secretAccessor"' in access
    assert "google_service_account.validator_prod[0].email" in access
    assert "google_secret_manager_secret_version" not in access
    assert "validator_wandb_secret_count" in shared_terraform
    assert "var.enable_validator || var.enable_validator_prod" in shared_terraform


def test_production_materializer_pins_the_recorded_numeric_secret_version() -> None:
    tasks = PROD_TASKS.read_text()

    materialize = tasks.split("- name: Materialize the production hotkey once", 1)[1]
    materialize = materialize.split(
        "- name: Verify the materialized wallet matches", 1
    )[0]
    assert '"{{ validator_stack_hotkey_secret_version }}"' in materialize
    assert "latest" not in materialize
    assert "validator_stack_hotkey_secret_version is match('^[1-9][0-9]*$')" in tasks


def test_disposable_admin_is_absent_by_default_and_has_no_dormant_principal() -> None:
    terraform = ADMIN_TERRAFORM.read_text()
    tfvars = PROD_TFVARS.read_text()

    assert 'default     = "absent"' in terraform
    assert 'validator_hotkey_admin_phase = "absent"' in tfvars
    assert "google_service_account.validator_hotkey_admin" in terraform
    assert "count        = local.validator_hotkey_admin_count" in terraform
    assert "deletion_protection      = false" in terraform
    assert "auto_delete  = true" in terraform
    assert "access_config {" not in terraform
    assert "roles/secretmanager.secretAccessor" not in terraform
    actas = terraform.split(
        'resource "google_service_account_iam_member" '
        '"validator_hotkey_admin_operator_actas"',
        1,
    )[1].split("output ", 1)[0]
    assert "local.validator_hotkey_admin_active" in actas
    assert "google_service_account.validator_hotkey_admin[0].name" in actas
    assert 'role               = "roles/iam.serviceAccountUser"' in actas


def test_validator_iap_uses_exact_instance_project_conditions() -> None:
    production = PROD_TERRAFORM.read_text()
    admin = ADMIN_TERRAFORM.read_text()

    assert 'resource "google_iap_tunnel_instance_iam_member"' not in production
    assert 'resource "google_iap_tunnel_instance_iam_member"' not in admin

    prod_iap = production.split(
        'resource "google_project_iam_member" "validator_prod_operator_iap"',
        1,
    )[1].split("resource ", 1)[0]
    assert 'role     = "roles/iap.tunnelResourceAccessor"' in prod_iap
    assert "resource.name.extract('/instances/{name}') ==" in prod_iap
    assert "module.validator_prod_vm[0].hostname" in prod_iap

    admin_iap = admin.split(
        'resource "google_project_iam_member" "validator_hotkey_admin_operator_iap"',
        1,
    )[1].split("resource ", 1)[0]
    assert 'role     = "roles/iap.tunnelResourceAccessor"' in admin_iap
    assert "resource.name.extract('/instances/{name}') ==" in admin_iap
    assert "google_compute_instance_from_template.validator_hotkey_admin" in admin_iap


def test_disposable_admin_arms_only_after_egress_is_restricted() -> None:
    terraform = ADMIN_TERRAFORM.read_text()
    startup = ADMIN_STARTUP.read_text()

    generator_role = terraform.split(
        'resource "google_project_iam_custom_role" "validator_hotkey_generator"',
        1,
    )[1].split("resource ", 1)[0]
    assert '"secretmanager.versions.add"' in generator_role
    assert '"secretmanager.versions.access"' not in generator_role
    assert '"secretmanager.versions.enable"' not in generator_role
    assert '"secretmanager.versions.disable"' not in generator_role
    assert '"secretmanager.versions.destroy"' not in generator_role

    binding = terraform.split(
        'resource "google_secret_manager_secret_iam_member" '
        '"validator_hotkey_generator"',
        1,
    )[1].split("resource ", 1)[0]
    assert "local.validator_hotkey_admin_armed ? 1 : 0" in binding
    assert "google_compute_instance_from_template.validator_hotkey_admin" in binding
    assert "google_compute_firewall.validator_hotkey_admin_deny_other_egress" in binding
    assert 'destination_ranges = ["199.36.153.4/30"]' in terraform
    assert 'destination_ranges = ["0.0.0.0/0"]' in terraform
    assert "validator_hotkey_admin_bootstrap_tag" in terraform
    assert "UV_OFFLINE=1" in startup
    assert "merge-base --is-ancestor" in startup
    assert "diff-index --quiet HEAD" in startup
    assert "ls-files --others --exclude-standard" in startup
    assert "--require-hashes" in startup
    assert "--result-file" in startup
    restricted_hosts = [
        line for line in startup.splitlines() if line.startswith("199.36.153.")
    ]
    assert len(restricted_hosts) == 4
    assert all("secretmanager.googleapis.com" in line for line in restricted_hosts)
    assert all("iamcredentials.googleapis.com" in line for line in restricted_hosts)


def test_disposable_admin_verifies_checkout_as_its_temporary_owner() -> None:
    startup = ADMIN_STARTUP.read_text()

    checkout = startup.split(
        'git -C "$${SOURCE}" checkout --detach "$${GIT_REVISION}"', 1
    )[1].split('chown -R root:root "$${ROOT}"', 1)[0]
    assert checkout.count('runuser -u "$${BOOTSTRAP_USER}" --') >= 5
    assert (
        'runuser -u "$${BOOTSTRAP_USER}" -- \\\n'
        '  git -C "$${SOURCE}" remote get-url origin'
    ) in checkout
    assert (
        'runuser -u "$${BOOTSTRAP_USER}" -- \\\n  git -C "$${SOURCE}" rev-parse HEAD'
    ) in checkout


def test_disposable_admin_uses_a_user_readable_requirements_file() -> None:
    startup = ADMIN_STARTUP.read_text()

    install = startup.split("readonly UV_REQUIREMENTS=", 1)[1].split(
        'runuser -u "$${BOOTSTRAP_USER}" -- env', 1
    )[0]
    assert (
        'chown "$${BOOTSTRAP_USER}:$${BOOTSTRAP_USER}" "$${UV_REQUIREMENTS}"' in install
    )
    assert 'chmod 0400 "$${UV_REQUIREMENTS}"' in install
    assert '--require-hashes -r "$${UV_REQUIREMENTS}"' in install
    assert 'rm -f "$${UV_REQUIREMENTS}"' in install
    assert "/dev/stdin" not in install


def test_protected_workflow_requires_bootstrap_revision_for_arming() -> None:
    workflow = INFRA_WORKFLOW.read_text()

    assert "options: [absent, bootstrap, armed]" in workflow
    assert 'test "$HOTKEY_ADMIN_REVISION" = "$(git rev-parse HEAD)"' in workflow
    assert "targeted plans are forbidden during hotkey bootstrap/arm" in workflow
    assert "targeted plans are forbidden while the hotkey admin exists" in workflow
    assert "Reject replacement of an armed hotkey admin" in workflow
    assert "TF_VAR_validator_hotkey_admin_phase" in workflow
    assert "TF_VAR_validator_hotkey_admin_revision" in workflow
    plan = workflow.split("- name: Create exact private plan", 1)[1].split(
        "- name: Reject replacement of an armed hotkey admin", 1
    )[0]
    assert (
        '-var="validator_hotkey_admin_phase=${TF_VAR_validator_hotkey_admin_phase}"'
        in plan
    )
    assert (
        '-var="validator_hotkey_admin_revision=${TF_VAR_validator_hotkey_admin_revision}"'
        in plan
    )
    assert "google_project_iam_member" in workflow
