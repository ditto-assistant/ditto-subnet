from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/coding_hosted_custody_key"


def test_custody_key_bootstrap_is_default_off_and_confirmation_gated() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    tasks = (ROLE / "tasks/main.yml").read_text()
    assert defaults == {
        "coding_hosted_custody_key_enabled": False,
        "coding_hosted_custody_key_confirmation": "",
        "coding_hosted_custody_source_revision": "",
    }
    assert "BOOTSTRAP NATIVE CODING RSA CUSTODY" in tasks
    assert "ditto-coding-hosted-v2" in tasks
    assert "^[0-9a-f]{40}$" in tasks


def test_private_key_never_leaves_the_distinct_custodian() -> None:
    tasks = (ROLE / "tasks/main.yml").read_text()
    assert "ditto-coding-custody" in tasks
    assert "ditto-coding-hosted" in tasks
    assert "rsa_keygen_bits:3072" in tasks
    assert "rsa_keygen_pubexp:65537" in tasks
    assert "bootstrap-consumed" in tasks
    assert "private-input-rsa.pem" in tasks
    assert "private-input-rsa-public.pem" in tasks
    assert "private-input-rsa-receipt.json" in tasks
    assert "public_spki_sha256" in tasks
    assert tasks.count('mode: "0700"') >= 1
    assert tasks.count('mode: "0600"') >= 3
    for forbidden in (
        "systemctl",
        "systemd",
        "secretmanager",
        "gcloud",
        "copy private key",
        "state: absent",
    ):
        assert forbidden not in tasks.lower()


def test_partial_state_is_refused_and_receipt_is_written_last() -> None:
    tasks = (ROLE / "tasks/main.yml").read_text()
    assert "Partial native Coding key state is retained" in tasks
    assert tasks.index("Consume the one-shot custody bootstrap") < tasks.index(
        "Generate the RSA-3072 private key"
    )
    assert "force: false" in tasks
    assert tasks.index("Verify the private key without emitting key material") < (
        tasks.index("Write the redacted key receipt last")
    )
    assert tasks.index("Compute the public SPKI identity") < tasks.index(
        "Write the redacted key receipt last"
    )


def test_account_discovery_is_one_complete_passwd_snapshot() -> None:
    tasks = (ROLE / "tasks/main.yml").read_text()
    assert "Inspect existing host accounts once" in tasks
    assert "Refresh all host accounts after custodian creation" in tasks
    assert tasks.count("database: passwd") == 2
    assert "Inspect existing worker and custodian accounts" not in tasks
    assert "getent_passwd.get('ditto-coding-hosted')" in tasks
    assert "getent_passwd.get('ditto-coding-custody')" in tasks


def test_custody_playbook_and_ci_never_enable_the_role() -> None:
    playbook = yaml.safe_load(
        (ROOT / "infra/ansible/playbooks/gcp-coding-hosted-custody-key.yml").read_text()
    )[0]
    fixture = yaml.safe_load(
        (ROOT / "infra/ansible/tests/coding-hosted-custody-key.yml").read_text()
    )[0]
    workflow = (ROOT / ".github/workflows/infra-ci.yml").read_text()
    assert playbook["hosts"] == "role_coding_hosted"
    assert playbook["roles"] == ["coding_hosted_custody_key"]
    assert fixture["hosts"] == "localhost"
    assert fixture["connection"] == "local"
    assert fixture["roles"] == ["coding_hosted_custody_key"]
    assert "coding_hosted_custody_key_enabled=true" not in workflow
    assert "tests/coding-hosted-custody-key.yml" in workflow
