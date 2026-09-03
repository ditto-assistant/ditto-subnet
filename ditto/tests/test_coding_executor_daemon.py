"""Regression checks for the dormant dedicated rootless coding daemon role."""

from pathlib import Path

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/coding_executor"
DEFAULTS = (ROLE / "defaults/main.yml").read_text()
TASKS = (ROLE / "tasks/main.yml").read_text()
INSTALLER = (ROLE / "files/install-rootless-docker.sh").read_text()
GUARD = (ROLE / "files/executor-egress-guard.sh").read_text()
DAEMON = (ROLE / "files/rootless-daemon.json").read_text()
PLAYBOOK = (ROOT / "infra/ansible/playbooks/gcp-coding-executor.yml").read_text()
WORKFLOW = (ROOT / ".github/workflows/infra-ci.yml").read_text()
DOC = (ROOT / "infra/docs/coding-executor-hosts.md").read_text()


def test_coding_executor_daemon_is_default_off_and_has_no_client() -> None:
    assert "coding_executor_daemon_enabled: false" in DEFAULTS
    assert "when: coding_executor_daemon_enabled | bool" in TASKS
    assert "when: not (coding_executor_daemon_enabled | bool)" in TASKS
    assert "coding_executor_user_unit.stat.exists" in TASKS
    assert "coding_executor_policy_files.changed" in TASKS
    assert "ditto-coding-executor" in DEFAULTS
    assert "ditto-coding-client" in DEFAULTS
    assert "no scorer, validator, wallet, image" in TASKS
    assert "or coding gate has been installed or enabled" in TASKS


def test_rootless_daemon_is_pinned_to_the_isolated_empty_identity() -> None:
    assert '"io.heyditto.dittobench.isolated=true"' in DAEMON
    assert "no-new-privileges" in DAEMON
    assert "CODING_EXECUTOR_HOME" in INSTALLER
    assert "dockerd-rootless.sh" in INSTALLER
    assert "systemctl disable --now docker.service docker.socket" in INSTALLER
    assert '"${user_systemctl[@]}" disable --now "$unit"' in INSTALLER
    assert "io.heyditto.dittobench.isolated=true" in INSTALLER
    assert "CODING_EXECUTOR_CLIENT_GROUP" in INSTALLER
    assert "gcloud secrets" not in INSTALLER.lower()
    assert "openrouter_api_key" not in INSTALLER.lower()
    assert "VALIDATOR_MNEMONIC" not in INSTALLER


def test_rootless_daemon_private_egress_guard_and_ci_coverage_are_present() -> None:
    for cidr in (
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
    ):
        assert cidr in GUARD
    assert 'chain="DCE-EXEC-EGRESS"' in GUARD
    assert "169.254.169.254/32 --dport 53 -j ACCEPT" in GUARD
    assert "hosts: role_coding_executor" in PLAYBOOK
    assert "gcp-coding-executor.yml" in WORKFLOW
    assert "docker-ce-rootless-extras" in TASKS
    assert "coding_executor_daemon_enabled" in DOC
    assert "neither a client service nor a candidate image" in DOC
