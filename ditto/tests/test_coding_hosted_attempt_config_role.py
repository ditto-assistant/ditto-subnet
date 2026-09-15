"""Hosted attempt config wrapper; synthetic checks never touch a host or unit."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/coding_hosted_attempt_config"
TASKS_SOURCE = (ROLE / "tasks/main.yml").read_text()
PLATFORM_CLI = (
    ROOT / "apps/platform/ditto/coding_hosted_attempt_config.py"
).read_text()
PLATFORM = (
    ROOT / "apps/platform/ditto/api_server/coding_hosted_attempt_config.py"
).read_text()
KINDS = [
    "budget_profile",
    "execution_profile",
    "grading_profile",
    "inference_policy",
    "probe_receipt",
]
STAGING = "/var/lib/ditto-coding-hosted-attempt-inputs"
WORKER_HOME = "/var/lib/ditto-coding-hosted/"
MATERIALIZE = (
    "Materialize the configuration as the worker with the installed interpreter"
)


def block():
    return yaml.safe_load(TASKS_SOURCE)[1]["block"]


def task(name):
    return next(item for item in block() if item["name"] == name)


def module(item):
    return next(key for key in item if key.startswith("ansible.builtin."))


def test_role_is_default_off_with_closed_pins_and_no_credential_inputs():
    assert yaml.safe_load((ROLE / "defaults/main.yml").read_text()) == {
        "coding_hosted_attempt_config_enabled": False,
        "coding_hosted_attempt_config_confirmation": "",
        "coding_hosted_attempt_config_runtime_revision": "",
        "coding_hosted_attempt_config_evaluation_id": "",
        "coding_hosted_attempt_config_assignment_sha256": "",
        "coding_hosted_attempt_config_evidence_wrapping_key_sha256": "",
        "coding_hosted_attempt_config_inputs": {},
    }
    tasks = yaml.safe_load(TASKS_SOURCE)
    assert tasks[1]["when"] == "coding_hosted_attempt_config_enabled | bool"
    assert tasks[1]["vars"]["coding_hosted_attempt_config_kinds"] == KINDS
    that = task(
        "Require the dedicated host, exact pins and the attempt-bound confirmation"
    )["ansible.builtin.assert"]["that"]
    for expected in (
        "ansible_facts['hostname'] == 'ditto-coding-hosted-v2'",
        "inventory_hostname in groups.get('role_coding_hosted', [])",
        "coding_hosted_attempt_config_inputs.keys() | sort == "
        "coding_hosted_attempt_config_kinds",
    ):
        assert expected in that
    confirmation = next(item for item in that if "CONFIRMATION" in item.upper())
    assert "'MATERIALIZE HOSTED CODING ATTEMPT CONFIG '" in confirmation
    assert "coding_hosted_attempt_config_evaluation_id" in confirmation
    assert "coding_hosted_attempt_config_assignment_sha256" in confirmation
    assert "lookup(" not in TASKS_SOURCE
    assert not re.search(
        r"\b(password|secret|provider_key|hippius|postgres)", TASKS_SOURCE.lower()
    )


def test_runtime_revision_is_the_one_the_worker_unit_runs():
    tasks = yaml.safe_load(TASKS_SOURCE)
    python = tasks[1]["vars"]["coding_hosted_attempt_config_python"]
    assert python == (
        "/opt/ditto-coding-hosted/{{ coding_hosted_attempt_config_runtime_revision }}"
        "/apps/platform/.venv/bin/python"
    )
    that = task(
        "Require the dedicated host, exact pins and the attempt-bound confirmation"
    )["ansible.builtin.assert"]["that"]
    # The connectivity role's worker unit ExecStart interpreter, not a free choice.
    assert (
        "coding_hosted_worker_python | default('') == "
        "coding_hosted_attempt_config_python"
    ) in that
    unit = ROOT / "infra/ansible/roles/coding_hosted_connectivity/templates"
    assert "coding_hosted_worker_python" in (unit / "worker.service.j2").read_text()
    argv = task(MATERIALIZE)["ansible.builtin.command"]["argv"]
    assert argv[9] == "{{ coding_hosted_attempt_config_python }}"
    assert argv[argv.index("--runtime-revision") + 1] == (
        "{{ coding_hosted_attempt_config_runtime_revision }}"
    )
    assert "request.runtime_revision == layout.runtime_revision" in PLATFORM


def test_staged_names_match_the_materializer_input_scheme():
    staging = yaml.safe_load(TASKS_SOURCE)[1]["vars"][
        "coding_hosted_attempt_config_staging"
    ]
    assert staging == STAGING
    assert f'INPUTS = Path("{STAGING}")' in PLATFORM
    assert 'return self.inputs / f"{kind}-{digest}.json"' in PLATFORM
    transfer = task(
        "Stage each input under its digest name without replacing existing bytes"
    )["ansible.builtin.copy"]
    assert transfer["dest"] == (
        "{{ coding_hosted_attempt_config_staging }}/"
        "{{ item.key | replace('_', '-') }}-{{ item.value.sha256 }}.json"
    )
    for kind in KINDS:
        assert f'layout.input("{kind.replace("_", "-")}"' in PLATFORM
    verify = task("Require sealed root-owned inputs with their reviewed digest")[
        "ansible.builtin.assert"
    ]["that"]
    for expected in (
        "item.stat.checksum == item.item.value.sha256",
        "item.stat.nlink == 1",
        "item.stat.pw_name == 'root'",
        "item.stat.mode == '0440'",
    ):
        assert expected in verify
    controller = task("Refuse a controller input that is not the reviewed regular file")
    assert (
        "item.stat.checksum == item.item.value.sha256"
        in controller["ansible.builtin.assert"]["that"]
    )


def test_root_never_writes_below_the_worker_owned_home():
    writes = [
        item
        for item in block()
        if module(item)
        in {"ansible.builtin.copy", "ansible.builtin.file", "ansible.builtin.template"}
    ]
    assert [item["name"] for item in writes] == [
        "Create the root-owned input staging directory",
        "Stage each input under its digest name without replacing existing bytes",
    ]
    for item in writes:
        body = item[module(item)]
        target = body.get("dest", body.get("path"))
        assert target.startswith("{{ coding_hosted_attempt_config_staging }}")
        assert body["owner"] == "root"
        assert body["group"] == "ditto-coding-hosted"
    directory = writes[0]["ansible.builtin.file"]
    assert (directory["state"], directory["mode"]) == ("directory", "0750")
    copy = writes[1]["ansible.builtin.copy"]
    assert copy["mode"] == "0440"
    assert (copy["force"], copy["follow"], copy["unsafe_writes"]) == (
        False,
        False,
        False,
    )
    # Staging is a sibling of the worker home, not below it; nothing else writes
    # there or changes directory into it as root.
    assert not STAGING.startswith(WORKER_HOME)
    assert WORKER_HOME not in TASKS_SOURCE
    stat = task("Refuse an unsafe existing staging directory without repairing it")
    condition = stat["ansible.builtin.assert"]["that"][0]
    assert "not coding_hosted_attempt_config_staging_stat.stat.islnk" in condition
    assert "stat.pw_name == 'root'" in condition
    command = task(MATERIALIZE)["ansible.builtin.command"]
    assert command["chdir"] == "/"


def test_materializer_runs_as_the_worker_with_every_reviewed_pin():
    argv = task(MATERIALIZE)["ansible.builtin.command"]["argv"]
    assert argv[:9] == [
        "/usr/sbin/runuser",
        "-u",
        "ditto-coding-hosted",
        "--",
        "/usr/bin/env",
        "-i",
        "PATH=/usr/bin:/bin",
        "LANG=C.UTF-8",
        "LC_ALL=C.UTF-8",
    ]
    assert argv[10:14] == ["-B", "-I", "-m", "ditto.coding_hosted_attempt_config"]
    flags = [item for item in argv if item.startswith("--") and item != "--"]
    declared = re.findall(r'add_argument\(\s*"(--[a-z0-9-]+)"', PLATFORM_CLI)
    assert flags == declared
    # Each reviewed input SHA-256 is passed, so the materializer compares it with
    # the assignment before selecting the staged file.
    for kind in KINDS:
        flag = f"--{kind.replace('_', '-')}-sha256"
        assert argv[argv.index(flag) + 1] == (
            f"{{{{ coding_hosted_attempt_config_inputs.{kind}.sha256 }}}}"
        )


def test_unit_liveness_has_one_source_in_the_materializer():
    # The Ansible side no longer parses systemctl output; the materializer's
    # allow-list (inactive or failed; unparseable lines refused) is the only rule.
    assert "systemctl" not in TASKS_SOURCE
    assert "list-units" not in TASKS_SOURCE
    assert "search(" not in TASKS_SOURCE
    assert "def idle_units(" in PLATFORM
    assert "    units()\n    refuse_live_custody(layout)\n    host = verify_host" in (
        PLATFORM
    )


def test_every_check_precedes_writes_and_nothing_is_started():
    names = [item["name"] for item in block()]
    first_write = names.index("Create the root-owned input staging directory")
    for check in (
        "Require the dedicated host, exact pins and the attempt-bound confirmation",
        "Require each input to be an absolute controller file and SHA-256",
        "Refuse check mode for an enabled materialization",
        "Refuse a controller input that is not the reviewed regular file",
        "Require the protected installed interpreter",
        "Refuse an unsafe existing staging directory without repairing it",
    ):
        assert names.index(check) < first_write
    assert names.index(
        "Require sealed root-owned inputs with their reviewed digest"
    ) < names.index(MATERIALIZE)
    for forbidden in (
        "state: started",
        "state: restarted",
        "enabled: true",
        "systemd_service",
        "daemon_reload",
        "ansible.builtin.shell",
        "state: absent",
        "force: true",
        "- start",
    ):
        assert forbidden not in TASKS_SOURCE


def test_playbook_fixture_and_ci_never_enable_the_role():
    playbook = yaml.safe_load(
        (
            ROOT / "infra/ansible/playbooks/gcp-coding-hosted-attempt-config.yml"
        ).read_text()
    )[0]
    assert playbook["hosts"] == "role_coding_hosted"
    assert playbook["roles"] == ["coding_hosted_attempt_config"]
    fixture_source = (
        ROOT / "infra/ansible/tests/coding-hosted-attempt-config.yml"
    ).read_text()
    fixture = yaml.safe_load(fixture_source)
    assert [play["hosts"] for play in fixture] == ["localhost"]
    assert "vars" not in fixture[0]
    assert "not coding_hosted_attempt_config_enabled" in fixture_source
    workflow = (ROOT / ".github/workflows/infra-ci.yml").read_text()
    assert "playbooks/gcp-coding-hosted-attempt-config.yml" in workflow
    assert "-i localhost, tests/coding-hosted-attempt-config.yml" in workflow
    assert "coding_hosted_attempt_config_enabled" not in workflow
