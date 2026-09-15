"""Native worker credential materialization is default-off, unforgeable, silent.

Structural tests parse the roles and assert their shape. The rehearsal, gated by
DITTO_ANSIBLE_REHEARSAL=1, runs the enabled tasks through ansible-core 2.21.2
against a temporary tree with obvious stand-in credentials and the real
role-local modules (found through ANSIBLE_LIBRARY, so the module transfer path is
exercised, not rewritten), and proves the guards refuse every forged, preset,
templated, wrong-state and wrong-metadata input, and that no stand-in value or
any digest of it in any algorithm or repr form ever reaches ansible output.
"""

import copy
import grp
import hashlib
import json
import os
import pwd
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/coding_hosted_worker_credentials"
CLEANUP_ROLE = ROOT / "infra/ansible/roles/coding_hosted_worker_credentials_cleanup"
MAIN = (ROLE / "tasks/main.yml").read_text()
MATERIALIZE = (ROLE / "tasks/materialize.yml").read_text()
CLEANUP_MAIN = (CLEANUP_ROLE / "tasks/main.yml").read_text()
REMOVE = (CLEANUP_ROLE / "tasks/remove.yml").read_text()
PARSED = yaml.safe_dump(yaml.safe_load(MATERIALIZE), width=10_000)
PLAYBOOK = ROOT / "infra/ansible/playbooks/gcp-coding-hosted-worker-credentials.yml"
CLEANUP_PLAYBOOK = (
    ROOT / "infra/ansible/playbooks/gcp-coding-hosted-worker-credentials-cleanup.yml"
)
WRITE_MODULE = ROLE / "library/coding_hosted_worker_credentials_write.py"
UNLINK_MODULE = CLEANUP_ROLE / "library/coding_hosted_worker_credentials_unlink.py"
LIBRARY_PATH = f"{ROLE / 'library'}:{CLEANUP_ROLE / 'library'}"

PREFIX = "coding_hosted_worker_credentials_"
CLEANUP_PREFIX = "coding_hosted_worker_credentials_cleanup_"
INPUTS = {
    f"{PREFIX}enabled": False,
    f"{PREFIX}confirmation": "",
    f"{PREFIX}source_revision": "",
}
CLEANUP_INPUTS = {
    f"{CLEANUP_PREFIX}enabled": False,
    f"{CLEANUP_PREFIX}confirmation": "",
    f"{CLEANUP_PREFIX}source_revision": "",
}
CONFIRMATION = "MATERIALIZE NATIVE CODING WORKER CREDENTIALS"
CLEANUP_CONFIRMATION = "REMOVE NATIVE CODING WORKER CREDENTIALS"
REVISION = "0123456789abcdef0123456789abcdef01234567"
REHEARSAL_GATE = "DITTO_ANSIBLE_REHEARSAL"
OWNER = "ditto-coding-hosted"

# main.yml
PRESET = "Refuse a preset gate, capture, result or credential-named variable"
GUARD_MARKER = "Require the guarded entry point marker, an accident guard only"
MARKER_ENV = "DITTO_CODING_HOSTED_GUARDED_RUN"
OPERATION = "worker-credentials-materialize"
CLEANUP_OPERATION = "worker-credentials-remove"
SPECS = ROOT / "infra/ansible/guarded-runs"
GATE_FREEZE = "Freeze the enabled gate once"
DORMANT = "Explain dormant native worker credential materialization"
INCLUDE = "Materialize the worker-owned credential files behind the enabled gate"
# materialize.yml
PRESET_INCLUDE = (
    "Refuse a preset internal name, a preset gate or a credential-named variable"
)
BATCH = "Require the run to target exactly the one dedicated host"
PIPELINING = (
    "Require SSH pipelining on and no kept remote files before carrying secrets"
)
CHECK_MODE = "Refuse check mode for an enabled materialization"
FREEZE_INPUTS = "Freeze the confirmation and source revision once"
GATE = "Require the exact confirmation and source revision as frozen literals"
IDENTITY = "Probe this machine's identity into a result extra vars cannot preset"
HOST = "Require the dedicated host"
CURATOR = "Refuse the offline curator secret key in the controller environment"
FREEZE_SECRETS = "Freeze the controller-environment credentials once"
CREDS = "Require each controller credential by name without printing its value"
DISTINCT = "Require eight distinct credential values without printing them"
RENDER = "Render the three credential documents once without printing them"
ACCOUNTS = "Inspect host accounts once"
ACCOUNT_CHECK = "Require the distinct worker and custodian identities"
LISTING = "List live worker and custody units"
LIVE = "Refuse to replace credentials unless every listed unit is inactive or failed"
WORKER_PROCS = "Require no process is running as the worker UID"
WORKER_PROCS_CHECK = "Refuse if any process runs as the worker UID"
DIRECTORIES = "Inspect the worker home and private directory without following links"
DIR_CHECK = "Require an existing owner-only private directory below a real worker home"
WRITE = "Write and verify the three files through the symlink-safe module"
WRITE_CHECK = "Require the module to have written and verified all three files"
RELIST = "Re-list live worker and custody units after writing"
LIVE_AFTER = "Refuse if any worker or custody unit went live during materialization"
REPORT = "Report only that the files exist and the revision that wrote them"

NO_LOG_TASKS = (FREEZE_INPUTS, CURATOR, FREEZE_SECRETS, CREDS, DISTINCT, RENDER, WRITE)

ENV_NAMES = [
    "DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_READER_ACCESS_KEY",
    "DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_READER_SECRET_KEY",
    "DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_CURATOR_ACCESS_KEY",
    "DITTO_CODING_WORKER_HIPPIUS_EVIDENCE_MEDIATOR_ACCESS_KEY",
    "DITTO_CODING_WORKER_HIPPIUS_EVIDENCE_MEDIATOR_SECRET_KEY",
    "DITTO_CODING_WORKER_IMAGE_STORAGE_ACCESS_KEY",
    "DITTO_CODING_WORKER_IMAGE_STORAGE_SECRET_KEY",
    "DITTO_CODING_WORKER_PROVIDER_KEY",
]


def _docs(text: str) -> list[dict]:
    return yaml.safe_load(text)


def _walk(tasks: list[dict]) -> Iterator[dict]:
    for task in tasks:
        yield task
        for section in ("block", "rescue", "always"):
            yield from _walk(task.get(section, []))


def _task(name: str, tasks: list[dict]) -> dict:
    (task,) = [t for t in _walk(tasks) if t.get("name") == name]
    return task


def _flat(text: object) -> str:
    return " ".join(str(text).split())


# ─── Structure ────────────────────────────────────────────────────────────────


def test_defaults_are_exactly_the_three_inputs() -> None:
    assert yaml.safe_load((ROLE / "defaults/main.yml").read_text()) == INPUTS
    assert (
        yaml.safe_load((CLEANUP_ROLE / "defaults/main.yml").read_text())
        == CLEANUP_INPUTS
    )


def test_main_refuses_presets_then_freezes_the_gate_and_includes() -> None:
    for main, prefix, include, inputs in (
        (MAIN, PREFIX, "materialize.yml", INPUTS),
        (CLEANUP_MAIN, CLEANUP_PREFIX, "remove.yml", CLEANUP_INPUTS),
    ):
        tasks = _docs(main)
        assert [t["name"] for t in tasks][:2] == [PRESET, GATE_FREEZE]
        # The preset guard runs before the gate is created and is the single
        # source of truth: only the three inputs may carry the prefix, and no
        # credential-named Ansible variable may be defined.
        that = tasks[0]["ansible.builtin.assert"]["that"]
        assert f"'^{prefix}" in _flat(that[0]) if prefix == CLEANUP_PREFIX else True
        assert _flat(that[0]).endswith(
            "| sort == [" + ", ".join(f"'{n}'" for n in sorted(inputs)) + "]"
        )
        assert "(?i)^(DITTO_CODING_WORKER_|DITTO_CODING_HIPPIUS_)" in _flat(that[1])
        assert tasks[0]["ansible.builtin.assert"]["quiet"] is True
        assert "{{" not in tasks[0]["ansible.builtin.assert"]["fail_msg"]
        # The gate is frozen with `is sameas true`, never `| bool`, under no_log.
        freeze = tasks[1]
        assert freeze["no_log"] is True
        assert "is sameas true" in _flat(
            freeze["ansible.builtin.set_fact"][f"{prefix}gate"]
        )
        assert "| bool" not in _flat(
            freeze["ansible.builtin.set_fact"][f"{prefix}gate"]
        )
        assert tasks[-1]["ansible.builtin.include_tasks"] == include
        assert tasks[-1]["when"] == f"{prefix}gate"
        assert (
            _task(DORMANT if prefix == PREFIX else tasks[2]["name"], tasks)["when"]
            == f"not {prefix}gate"
        )
    assert "ansible.builtin.copy" not in MAIN and "import_tasks" not in MAIN
    # The materialization prefix excludes the cleanup prefix so neither matches.
    assert "(?!cleanup_)" in _flat(_docs(MAIN)[0]["ansible.builtin.assert"]["that"][0])


def test_materialize_task_order() -> None:
    assert [t["name"] for t in _docs(MATERIALIZE)] == [
        PRESET_INCLUDE,
        GUARD_MARKER,
        BATCH,
        PIPELINING,
        CHECK_MODE,
        FREEZE_INPUTS,
        GATE,
        IDENTITY,
        HOST,
        CURATOR,
        FREEZE_SECRETS,
        CREDS,
        DISTINCT,
        RENDER,
        ACCOUNTS,
        ACCOUNT_CHECK,
        LISTING,
        LIVE,
        WORKER_PROCS,
        WORKER_PROCS_CHECK,
        DIRECTORIES,
        DIR_CHECK,
        WRITE,
        WRITE_CHECK,
        RELIST,
        LIVE_AFTER,
        REPORT,
    ]


def test_in_include_guards_are_start_at_task_proof_and_target_one_host() -> None:
    for tasks, prefix, first in (
        (_docs(MATERIALIZE), PREFIX, PRESET_INCLUDE),
        (_docs(REMOVE), CLEANUP_PREFIX, PRESET_INCLUDE),
    ):
        names = [t["name"] for t in tasks]
        # The first task inside the dynamic include repeats the refusal so
        # --start-at-task (which begins at a main.yml task) cannot skip it.
        assert names[0] == first
        that = tasks[0]["ansible.builtin.assert"]["that"]
        # A preset gate fact alone cannot open the run: the raw enabled flag must
        # be boolean true.
        assert any(
            f"({prefix}enabled | default(false, true)) is sameas true" in _flat(line)
            for line in that
        )
        assert (
            any(
                f"'^{prefix}" in _flat(line) and "(?!cleanup_)" in _flat(line)
                for line in that
            )
            or prefix == CLEANUP_PREFIX
        )
        # The guarded entry point's marker is next, then the batch guard pins
        # the exact single host and the whole play host set (so serial: 1 cannot
        # pass without --limit), not a -e-overridable inventory_hostname/group
        # check.
        assert names[1] == GUARD_MARKER
        assert names[2] == BATCH
        batch = tasks[2]["ansible.builtin.assert"]["that"]
        assert batch == [
            "ansible_play_batch == ['ditto-coding-hosted-v2']",
            "ansible_play_hosts_all == ['ditto-coding-hosted-v2']",
        ]
    # No role reads inventory_hostname or a groups membership for targeting.
    for text in (MATERIALIZE, REMOVE):
        assert "inventory_hostname in groups" not in text
        assert "ansible_play_hosts_all == ['ditto-coding-hosted-v2']" in text


def test_pipelining_and_keep_remote_files_guard_precedes_secrets() -> None:
    tasks = _docs(MATERIALIZE)
    names = [t["name"] for t in tasks]
    assert names[3] == PIPELINING
    that = _task(PIPELINING, tasks)["ansible.builtin.assert"]["that"]
    assert "(ansible_pipelining | default(false)) is sameas true" in that
    # ansible_ssh_pipelining wins over ansible_pipelining on 2.21.2, so it must
    # also be undefined or true.
    assert any(
        "ansible_ssh_pipelining is not defined" in line and "is sameas true" in line
        for line in that
    )
    assert any("DEFAULT_KEEP_REMOTE_FILES" in line for line in that)
    # It runs before the first secret-carrying task (the env freeze).
    assert names.index(PIPELINING) < names.index(FREEZE_SECRETS)
    # The unlink module carries no secret, so cleanup needs no pipelining guard.
    assert "DEFAULT_KEEP_REMOTE_FILES" not in REMOVE
    # The materialize playbook turns pipelining on in its play vars so the guard
    # confirms a genuinely pipelined run (the var is the SSH plugin's input).
    (play,) = yaml.safe_load(PLAYBOOK.read_text())
    assert play["vars"]["ansible_pipelining"] is True


def test_unlink_module_reports_every_path() -> None:
    src = UNLINK_MODULE.read_text()
    for field in (
        "refused",
        "not_attempted",
        "already_absent",
        "private_dir_mode",
        "private_dir_present",
    ):
        assert field in src, field
    # Cleanup requires the owner but tolerates a wrong-mode directory (asymmetry).
    assert "is not owned by the worker" in src


def test_no_role_internal_data_flows_through_overridable_include_vars() -> None:
    # The idle checks are inlined asserts reading the register directly, not an
    # include whose vars: an extra var could override.
    for text in (MATERIALIZE, REMOVE):
        assert "assert_units_idle" not in text
        assert "idle_units_listing" not in text
        assert "idle_units_fail_msg" not in text
    assert not (ROLE / "tasks/assert_units_idle.yml").exists()
    assert not (CLEANUP_ROLE / "tasks/assert_units_idle.yml").exists()


def test_frozen_captures_defeat_lazy_templating() -> None:
    tasks = _docs(MATERIALIZE)
    freeze = _task(FREEZE_INPUTS, tasks)
    assert freeze["no_log"] is True
    sf = freeze["ansible.builtin.set_fact"]
    assert f"{PREFIX}confirmation | default('', true)" in _flat(
        sf[f"{PREFIX}gate_confirmation"]
    )
    that = _task(GATE, tasks)["ansible.builtin.assert"]["that"]
    assert f"{PREFIX}gate_confirmation == '{CONFIRMATION}'" in that
    assert any("search('[{][{]|[{][%]|[{][#]')" in _flat(line) for line in that)
    secrets = _task(FREEZE_SECRETS, tasks)["ansible.builtin.set_fact"][
        f"{PREFIX}secrets"
    ]
    assert all("| default('', true)" in v for v in secrets.values())
    assert set(secrets) == set(ENV_NAMES)


def test_every_secret_touching_task_is_no_log_and_write_uses_a_no_log_module() -> None:
    tasks = _docs(MATERIALIZE)
    for name in NO_LOG_TASKS:
        assert _task(name, tasks).get("no_log") is True, name
    write = _task(WRITE, tasks)
    assert "coding_hosted_worker_credentials_write" in write
    module = write["coding_hosted_worker_credentials_write"]
    assert module["documents"] == f"{{{{ {PREFIX}documents }}}}"
    assert (
        "stdin" not in write and "cmd" not in write and "argv" not in json.dumps(write)
    )
    # The module declares documents no_log in its argument_spec, so the target's
    # invocation journal and -vvv redact it even apart from the task no_log.
    src = WRITE_MODULE.read_text()
    assert '"documents": {"type": "dict", "required": True, "no_log": True}' in src
    # No module reads bytes back, diffs, or shells to a secret store.
    for forbidden in ("slurp", "ansible.builtin.copy", "set -x", "gcloud secrets"):
        assert forbidden not in MATERIALIZE, forbidden
    report = _task(REPORT, tasks)["ansible.builtin.debug"]["msg"]
    assert "secret" not in report.lower()
    # Non-no_log asserts carry constant fail_msgs, except the module checks which
    # interpolate only the module's own non-secret message.
    for task in list(_walk(tasks)) + list(_walk(_docs(REMOVE))):
        assertion = task.get("ansible.builtin.assert")
        if assertion and not task.get("no_log"):
            msg = assertion.get("fail_msg", "")
            if "module" in task["name"] and "helper" in json.dumps(assertion):
                assert "| default(" in _flat(msg)
            else:
                assert "{{" not in msg, task["name"]


def test_write_and_unlink_modules_are_symlink_safe_and_silent() -> None:
    for src in (WRITE_MODULE.read_text(), UNLINK_MODULE.read_text()):
        assert "O_NOFOLLOW" in src and "O_DIRECTORY" in src
        assert 'os.open("/"' in src  # opens from root, component by component
        assert ".hexdigest()" not in src  # digests compared as bytes, never emitted
    assert (
        "os.rename(" in WRITE_MODULE.read_text()
        and "src_dir_fd" in WRITE_MODULE.read_text()
    )
    assert (
        "os.unlink(" in UNLINK_MODULE.read_text()
        and "dir_fd=dir_fd" in UNLINK_MODULE.read_text()
    )
    # The write module tracks temps and unlinks them on every failure path.
    assert "temps[name] = tmp" in WRITE_MODULE.read_text()
    assert "os.unlink(tmp, dir_fd=dir_fd)" in WRITE_MODULE.read_text()
    # Cleanup removes and reports leftover .<name>.*.tmp files.
    assert "leftover_temps" in UNLINK_MODULE.read_text()
    assert ".tmp" in UNLINK_MODULE.read_text()


def test_worker_uid_guard_uses_uid_index_one_in_both_roles() -> None:
    for text in (MATERIALIZE, REMOVE):
        assert "/proc" in text and "-uid" in text
        assert "getent_passwd['ditto-coding-hosted'][1]" in text
        assert "getent_passwd['ditto-coding-hosted'][2]" not in text  # that is the GID


def test_no_service_is_started() -> None:
    assert PARSED.count("systemctl") == 2
    for forbidden in ("systemd:", "service:", "state: stopped", "state: started"):
        assert forbidden not in PARSED, forbidden


def test_source_revision_is_bound_into_the_module_and_report() -> None:
    tasks = _docs(MATERIALIZE)
    module = _task(WRITE, tasks)["coding_hosted_worker_credentials_write"]
    assert module["source_revision"] == f"{{{{ {PREFIX}gate_source_revision }}}}"
    report = _task(REPORT, tasks)["ansible.builtin.debug"]["msg"]
    assert f"source_revision={{{{ {PREFIX}gate_source_revision }}}}" in report


def test_playbooks_and_ci_registration() -> None:
    for playbook, role in (
        (PLAYBOOK, "coding_hosted_worker_credentials"),
        (CLEANUP_PLAYBOOK, "coding_hosted_worker_credentials_cleanup"),
    ):
        (play,) = yaml.safe_load(playbook.read_text())
        assert play["hosts"] == "role_coding_hosted"
        assert play["become"] is True and play["gather_facts"] is False
        assert play["roles"] == [role]
    workflow = (ROOT / ".github/workflows/infra-ci.yml").read_text()
    for token in (
        "playbooks/gcp-coding-hosted-worker-credentials.yml",
        "playbooks/gcp-coding-hosted-worker-credentials-cleanup.yml",
        "tests/coding-hosted-worker-credentials.yml",
    ):
        assert token in workflow
    this_file = str(Path(__file__).relative_to(ROOT))
    infra = yaml.safe_load(workflow)
    for trigger in ("pull_request", "push"):
        assert this_file in infra[True][trigger]["paths"]
    (step,) = [
        s
        for job in infra["jobs"].values()
        for s in job["steps"]
        if REHEARSAL_GATE in s.get("env", {}) and this_file in s["run"]
    ]
    assert step in infra["jobs"]["ansible"]["steps"]
    platform = (ROOT / ".github/workflows/platform-ci.yml").read_text()
    assert "coding_hosted_worker_credentials" in platform


def test_guarded_entry_point_specs_and_markers() -> None:
    # The supported entry point is infra/scripts/coding-hosted-guarded-run.py;
    # each role's marker is an accident guard inside its dynamic include.
    for tasks, operation, nothing in (
        (_docs(MATERIALIZE), OPERATION, "Nothing was written."),
        (_docs(REMOVE), CLEANUP_OPERATION, "Nothing was removed."),
    ):
        marker = _task(GUARD_MARKER, tasks)["ansible.builtin.assert"]
        assert marker["that"] == [
            f"lookup('ansible.builtin.env', '{MARKER_ENV}') == '{operation}'"
        ]
        assert marker["quiet"] is True
        assert _flat(marker["fail_msg"]).endswith(nothing)
        assert "{{" not in marker["fail_msg"]
    assert MARKER_ENV not in MAIN and MARKER_ENV not in CLEANUP_MAIN
    base = {
        "schema": "ditto-coding-hosted-guarded-run/v1",
        "limit": "ditto-coding-hosted-v2",
        "nonsecret_env_vars": [],
    }
    creds = _task(CREDS, _docs(MATERIALIZE))["loop"]
    assert json.loads((SPECS / f"{OPERATION}.json").read_text()) == {
        **base,
        "operation": OPERATION,
        "playbook": "playbooks/gcp-coding-hosted-worker-credentials.yml",
        "enabled_var": f"{PREFIX}enabled",
        "confirmation_var": f"{PREFIX}confirmation",
        "confirmation": CONFIRMATION,
        "revision_var": f"{PREFIX}source_revision",
        # The guard applies the role's own bounds before ansible starts: each
        # value is printable ASCII without whitespace, longer than its prefix,
        # within its byte bound, and the eight values are distinct.
        "secret_env": [
            {
                "name": item["env"],
                "charset": "printable_ascii",
                "prefix": item["prefix"],
                "min_length": len(item["prefix"]) + 1,
                "max_length": item["max_bytes"],
            }
            for item in creds
        ],
        "distinct_secret_values": True,
        "forbidden_env": [
            "DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_CURATOR_SECRET_KEY",
            "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_CURATOR_SECRET_KEY",
        ],
        "forbidden_env_prefixes": [],
    }
    assert [item["env"] for item in creds] == ENV_NAMES
    assert json.loads((SPECS / f"{CLEANUP_OPERATION}.json").read_text()) == {
        **base,
        "operation": CLEANUP_OPERATION,
        "playbook": "playbooks/gcp-coding-hosted-worker-credentials-cleanup.yml",
        "enabled_var": f"{CLEANUP_PREFIX}enabled",
        "confirmation_var": f"{CLEANUP_PREFIX}confirmation",
        "confirmation": CLEANUP_CONFIRMATION,
        "revision_var": f"{CLEANUP_PREFIX}source_revision",
        "secret_env": [],
        "distinct_secret_values": False,
        # Removal needs no secret: an exported credential is refused.
        "forbidden_env": ["DITTO_CODING_HIPPIUS_PRIVATE_INPUT_CURATOR_SECRET_KEY"],
        "forbidden_env_prefixes": ["DITTO_CODING_WORKER_"],
    }
    for playbook, operation in (
        (PLAYBOOK, OPERATION),
        (CLEANUP_PLAYBOOK, CLEANUP_OPERATION),
    ):
        text = playbook.read_text()
        assert "infra/scripts/coding-hosted-guarded-run.py" in text
        assert operation in text
        assert "-e '" not in text


def test_docs_describe_every_guard() -> None:
    docs = _flat(
        (ROOT / "infra/docs/coding-hosted-worker-credentials-v2.md").read_text()
    )
    for phrase in (
        "`coding_hosted_worker_credentials_*`",
        CONFIRMATION,
        CLEANUP_CONFIRMATION,
        "DITTO_CODING_WORKER_",
        "curator secret key",
        "include_tasks",
        "--start-at-task",
        "finalization",
        "Removal is not revocation",
        "`refreshing`",
        "An empty listing",
        f"`{REHEARSAL_GATE}=1`",
        "dedicated image-reader identity and HMAC key",
        "dedicated, hard-capped OpenRouter key",
        "O_NOFOLLOW",
        "worker UID",
        "ANSIBLE_CONFIG",
        "no_log",
        "library",
        "ansible_play_batch == ['ditto-coding-hosted-v2']",
        "ansible_play_hosts_all == ['ditto-coding-hosted-v2']",
        "ansible_ssh_pipelining",
        "pipelining",
        "--limit ditto-coding-hosted-v2",
        "--step",
        "private_dir_mode",
        "not_attempted",
        "infra/scripts/coding-hosted-guarded-run.py",
        OPERATION,
        CLEANUP_OPERATION,
        "worker-credentials-journal-check",
        "accident guard only",
        "Direct `ansible-playbook` invocation is unsupported",
        "`#jinja2`",
        "two-hour",
        "request.time",
        "platform-storage-hmac-secret",
        "validator-openrouter-key",
        "hard credit limit",
        "renew",
    ):
        assert phrase in docs, phrase


# ─── Rehearsal ──────────────────────────────────────────────────────────────

STANDINS = {
    ENV_NAMES[0]: 'hip_reader"acc\\ess1',
    ENV_NAMES[1]: 'reader"sec\\ret2',
    ENV_NAMES[2]: 'hip_curator"acc\\ess3',
    ENV_NAMES[3]: 'hip_evidence"acc\\ess4',
    ENV_NAMES[4]: 'evidence"sec\\ret5',
    ENV_NAMES[5]: 'GOOG"image\\access6',
    ENV_NAMES[6]: 'image"sec\\ret7',
    ENV_NAMES[7]: 'sk-or"prov\\ider8',
}
LOOKUP_STANDIN = "REHEARSAL_LOOKUP_STANDIN"
LOOKUP_VALUE = 'lookup"lea\\k9'
# A positive control: this benign token is always in the output, so the leak
# search is proven to be scanning a real haystack.
CANARY = "PLAY RECAP"
STOPPED_UNITS = (
    "ditto-coding-hosted-worker.service loaded failed failed Worker\n"
    "ditto-coding-custody@0.service loaded inactive dead Custody\n"
)
LIVE_UNITS = {
    "active": "ditto-coding-custody@0.service loaded active running Custody",
    "activating": "ditto-coding-hosted-worker.service loaded activating start Worker",
    "reloading": "ditto-coding-custody@0.service loaded reloading reload Custody",
    "refreshing": "ditto-coding-custody@1.service loaded refreshing refresh-extensions C",  # noqa: E501
    "maintenance": "ditto-coding-custody@2.service loaded maintenance cleaning Custody",
    "unknown_state": "ditto-coding-hosted-worker.service loaded quiescent idle Worker",
    "unparseable": "● ditto-coding-custody@0.service loaded inactive dead Custody",
}
PROBED_IDENTITY = {
    "hostname": "ditto-coding-hosted-v2",
    "architecture": "x86_64",
    "distribution": "Debian",
    "distribution_major_version": "13",
}
# uid != gid so a guard that reads the GID column [2] instead of the UID [1] is
# caught by the rehearsal.
UID = str(os.getuid())
GID = str(os.getgid())
ACCOUNTS_FACT = {
    "ditto-coding-hosted": [
        "x",
        UID,
        "60002",
        "",
        "/var/lib/ditto-coding-hosted",
        "/usr/sbin/nologin",
    ],
    "ditto-coding-custody": [
        "x",
        "60003",
        "60003",
        "",
        "/var/lib/ditto-coding-custody",
        "/usr/sbin/nologin",
    ],
}

rehearsal = pytest.mark.skipif(
    os.environ.get(REHEARSAL_GATE) != "1",
    reason=f"set {REHEARSAL_GATE}=1 to run the ansible-core rehearsal",
)


def _rewrite(value):
    if isinstance(value, dict):
        return {
            k: v if k == "ansible.builtin.assert" else _rewrite(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_rewrite(v) for v in value]
    if isinstance(value, str) and value == OWNER:
        return "{{ rehearsal_owner }}"
    if isinstance(value, str):
        return value.replace("/var/lib/", "{{ rehearsal_root }}/var/lib/")
    return value


def _rehearse_identity(tasks, host_task) -> None:
    host = _task(host_task, tasks)["ansible.builtin.assert"]
    probed = host["that"][1].split(".ansible_facts.")[0]
    rewritten = []
    for line in host["that"]:
        m = re.fullmatch(
            rf"{re.escape(probed)}\.ansible_facts\.(ansible_\w+) == '[^']+'", line
        )
        rewritten.append(
            f"{probed}.ansible_facts.{m[1]} == rehearsal_local_identity.ansible_facts.{m[1]}"  # noqa: E501
            if m
            else line
        )
    host["that"] = rewritten


def _rehearse_common(tasks, accounts_task, listing, relist, worker_procs) -> None:
    accounts = _task(accounts_task, tasks)
    accounts.pop("ansible.builtin.getent")
    accounts["ansible.builtin.set_fact"] = {"getent_passwd": "{{ rehearsal_accounts }}"}
    _task(listing, tasks)["ansible.builtin.command"]["argv"] = [
        "/usr/bin/printf",
        "%s",
        "{{ rehearsal_units }}",
    ]
    _task(relist, tasks)["ansible.builtin.command"]["argv"] = [
        "/usr/bin/printf",
        "%s",
        "{{ rehearsal_units_after | default(rehearsal_units) }}",
    ]
    _task(worker_procs, tasks)["ansible.builtin.command"]["argv"] = [
        "/usr/bin/printf",
        "%s",
        "{{ rehearsal_worker_procs | default('') }}",
    ]


def _rehearsal_materialize(
    *,
    production_identity: bool = False,
    real_batch: bool = False,
    prepend_main: bool = True,
) -> list[dict]:
    # Prepend main.yml's preset refusal and gate freeze so the inlined run
    # mirrors main.yml -> materialize.yml, then rewrite paths and owner. When
    # prepend_main is False the caller supplies main.yml itself (the gate cases),
    # so the file is exactly the dynamically included materialize.yml and its
    # own first-task guard is the operative one under --start-at-task.
    main = _docs(MAIN)
    tasks = [
        *([copy.deepcopy(main[0]), copy.deepcopy(main[1])] if prepend_main else []),
        *copy.deepcopy(_docs(MATERIALIZE)),
    ]
    if not production_identity:
        _rehearse_identity(tasks, HOST)
    if not real_batch:
        # The bulk multi-host run cannot be a single dedicated host, so make the
        # batch check tautological there; dedicated cases keep the real literal.
        _task(BATCH, tasks)["ansible.builtin.assert"]["that"] = [
            "ansible_play_batch == ansible_play_batch"
        ]
    _rehearse_common(tasks, ACCOUNTS, LISTING, RELIST, WORKER_PROCS)
    _task(REPORT, tasks)["register"] = "rehearsal_report"
    tasks = _rewrite(tasks)
    unrooted = [
        t["name"]
        for t in _walk(tasks)
        if "block" not in t and re.search(r"(?<!\}\})/var/lib/", json.dumps(t))
    ]
    assert unrooted == [ACCOUNT_CHECK], unrooted
    return tasks


def _play(tasks, rp, hosts="all", report_var="rehearsal_report") -> dict:
    outcome = "{{ rehearsal_root }}/outcome-{{ rehearsal_pass }}.json"
    return {
        "name": f"Rehearse ({rp})",
        "hosts": hosts,
        "strategy": "free",
        "connection": "local",
        "gather_facts": False,
        "become": False,
        "vars": {
            "ansible_python_interpreter": "{{ ansible_playbook_python }}",
            "rehearsal_pass": rp,
            # Mirror the playbook's own play var (not a host var), so the guard
            # runs against the playbook's setting; -e can still override it.
            "ansible_pipelining": True,
        },
        "tasks": [
            {
                "name": "Probe this machine independently of the role",
                "ansible.builtin.setup": {
                    "gather_subset": ["!all", "!min", "platform", "distribution"]
                },
                "register": "rehearsal_local_identity",
            },
            {
                "name": "Rehearse",
                "block": [
                    *tasks,
                    {
                        "name": "Record completion",
                        "ansible.builtin.copy": {
                            "dest": outcome,
                            "content": "{{ {'report': "
                            + report_var
                            + ".msg} | to_json }}",
                        },
                    },
                ],
                "rescue": [
                    {
                        "name": "Record refusal",
                        "ansible.builtin.copy": {
                            "dest": outcome,
                            "content": (
                                "{{ {'task': ansible_failed_task.name, "
                                "'messages': [ansible_failed_result.msg | default('')] + "  # noqa: E501
                                "(ansible_failed_result.results | default([]) "
                                "| selectattr('failed', 'defined') | selectattr('failed') "  # noqa: E501
                                "| map(attribute='msg') | list)} | to_json }}"
                            ),
                        },
                    }
                ],
            },
        ],
    }


def _digest_forms(value: str) -> list[str]:
    raw = value.encode()
    return [
        hashlib.new(a, raw).hexdigest() for a in ("md5", "sha1", "sha256", "sha512")
    ]


def _text_forms(value: str) -> list[str]:
    return [
        value,
        json.dumps(value)[1:-1],
        yaml.safe_dump(value).strip(),
        repr(value),  # the doubled-backslash repr form ansible prints on some paths
        value.replace("\\", "\\\\"),
    ]


def _leak_free(output: str) -> None:
    assert CANARY in output, "leak search ran against an empty haystack"
    secrets = [*STANDINS.values(), LOOKUP_VALUE]
    for value in secrets:
        for form in _text_forms(value):
            assert form not in output, f"plaintext leak: {form!r}"
        for digest in _digest_forms(value):
            assert digest not in output, f"digest leak: {digest}"
    for document in _expected_documents().values():
        for digest in _digest_forms(document):
            assert digest not in output, f"document digest leak: {digest}"


def _expected_documents() -> dict[str, str]:
    hippius = {
        "DITTO_CODING_HIPPIUS_ENDPOINT_URL": "https://s3.hippius.com",
        "DITTO_CODING_HIPPIUS_REGION": "decentralized",
        "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_BUCKET": "ditto-subnet-coding-private-input",  # noqa: E501
        "DITTO_CODING_HIPPIUS_SEALED_EVIDENCE_BUCKET": "ditto-subnet-coding-sealed-evidence",  # noqa: E501
        "DITTO_CODING_HIPPIUS_TIMEOUT_SECONDS": "20",
        "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_READER_ACCESS_KEY": STANDINS[ENV_NAMES[0]],
        "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_READER_SECRET_KEY": STANDINS[ENV_NAMES[1]],
        "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_CURATOR_ACCESS_KEY": STANDINS[ENV_NAMES[2]],
        "DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_ACCESS_KEY": STANDINS[ENV_NAMES[3]],
        "DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_SECRET_KEY": STANDINS[ENV_NAMES[4]],
    }
    image = {
        "endpoint_url": "https://storage.googleapis.com",
        "bucket": "ditto-platform-agents-prod",
        "region": "auto",
        "access_key": STANDINS[ENV_NAMES[5]],
        "secret_key": STANDINS[ENV_NAMES[6]],
    }
    return {
        "hippius-environment.json": json.dumps(hippius, sort_keys=True),
        "image-storage.json": json.dumps(image, sort_keys=True),
        "provider-key": STANDINS[ENV_NAMES[7]],
    }


def _make_home(root: Path) -> Path:
    home = root / "var/lib/ditto-coding-hosted"
    (home / "private").mkdir(parents=True)
    home.chmod(0o755)
    (home / "private").chmod(0o700)
    return home / "private"


def _base_vars(root: Path) -> dict:
    return {
        "rehearsal_root": str(root),
        "rehearsal_units": STOPPED_UNITS,
        "rehearsal_owner": pwd.getpwuid(os.getuid()).pw_name,
        "rehearsal_group": grp.getgrgid(os.getgid()).gr_name,
        "rehearsal_accounts": ACCOUNTS_FACT,
    }


def _mat_host(root: Path) -> dict:
    _make_home(root)
    return {
        f"{PREFIX}enabled": True,
        f"{PREFIX}confirmation": CONFIRMATION,
        f"{PREFIX}source_revision": REVISION,
        **_base_vars(root),
    }


_MARKER_LITERAL = re.compile(
    re.escape(f"lookup('ansible.builtin.env', '{MARKER_ENV}') == '") + r"([a-z-]+)'"
)
_UNSET = object()


def _marker_for(plays: object) -> str | None:
    # The guarded entry point sets exactly one operation's marker per ansible
    # run; the rehearsal sets the one the rehearsed tasks require.
    found = set(_MARKER_LITERAL.findall(json.dumps(plays)))
    assert len(found) <= 1, found
    return next(iter(found), None)


def _run(tmp_path, name, hosts, plays, *flags, extra_env=None, marker=_UNSET) -> str:
    work = tmp_path / name
    work.mkdir()
    inventory = {"all": {"children": {"role_coding_hosted": {"hosts": hosts}}}}
    (work / "inventory.yml").write_text(yaml.safe_dump(inventory))
    (work / "rehearsal.yml").write_text(yaml.safe_dump(plays, sort_keys=False))
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("ANSIBLE_") and k not in ENV_NAMES
    }
    env |= {
        "ANSIBLE_HOME": str(work / "h"),
        "ANSIBLE_LOCAL_TEMP": str(work / "t"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_RETRY_FILES_ENABLED": "0",
        "ANSIBLE_CALLBACK_RESULT_FORMAT": "yaml",
        "ANSIBLE_LIBRARY": LIBRARY_PATH,
        "PYTHONDONTWRITEBYTECODE": "1",
        LOOKUP_STANDIN: LOOKUP_VALUE,
        **(extra_env if extra_env is not None else STANDINS),
    }
    chosen = _marker_for(plays) if marker is _UNSET else marker
    if chosen is not None:
        env[MARKER_ENV] = chosen
    completed = subprocess.run(
        [
            "uvx",
            "--from",
            "ansible-core==2.21.2",
            "ansible-playbook",
            "-f",
            "8",
            "-i",
            "inventory.yml",
            "--diff",
            "-vvv",
            *flags,
            "rehearsal.yml",
        ],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    return completed.stdout + completed.stderr


def _outcome(root: Path, rp: str) -> dict:
    return json.loads((root / f"outcome-{rp}.json").read_text())


def _private(root: Path) -> Path:
    return root / "var/lib/ditto-coding-hosted/private"


def _assert_refused(root, task, rp, *, match_msg=True, files_before=0) -> None:
    outcome = _outcome(root, rp)
    assert outcome.get("task") == task, (root.name, outcome)
    if match_msg:
        source = next(
            doc
            for doc in (_docs(MAIN), _docs(MATERIALIZE))
            if any(t.get("name") == task for t in _walk(doc))
        )
        expected = _flat(_task(task, source)["ansible.builtin.assert"]["fail_msg"])
        assert expected in [_flat(m) for m in outcome["messages"]], (root.name, outcome)
    assert len(list(_private(root).iterdir())) == files_before, root.name


def _assert_materialized(root, rp) -> None:
    assert _outcome(root, rp)["report"].startswith(
        "Native Coding worker credentials materialized"
    )
    for name, document in _expected_documents().items():
        path = _private(root) / name
        assert path.stat().st_mode & 0o777 == 0o600 and path.stat().st_nlink == 1
        assert path.read_text() == document


@rehearsal
def test_rehearsal_materializes_only_when_every_guard_passes(tmp_path) -> None:
    dest_cases = ("dest_symlink", "dest_hardlink", "dest_wrongmode")
    env_cases = (
        "missing_credential",
        "duplicate_credential",
        "bad_prefix",
        "curator_secret",
    )
    names = [
        "materialized",
        "no_units",
        *LIVE_UNITS,
        "confirmation_wrong",
        "revision_newline",
        "lookup_confirmation",
        "erroring_confirmation",
        "preset_documents",
        "preset_gate",
        "undocumented_input",
        "credential_as_variable",
        "idle_override",
        "worker_uid_busy",
        "unit_went_live",
        *dest_cases,
        *env_cases,
    ]
    roots = {n: tmp_path / "hosts" / n for n in names}
    hosts = {n: _mat_host(r) for n, r in roots.items()}
    hosts["no_units"]["rehearsal_units"] = ""
    for n, line in LIVE_UNITS.items():
        hosts[n]["rehearsal_units"] = STOPPED_UNITS + line + "\n"
    hosts["confirmation_wrong"][f"{PREFIX}confirmation"] = CONFIRMATION.lower()
    hosts["revision_newline"][f"{PREFIX}source_revision"] = REVISION + "\n"
    hosts["lookup_confirmation"][f"{PREFIX}confirmation"] = (
        "{{ lookup('env', '" + LOOKUP_STANDIN + "') }}"
    )
    hosts["erroring_confirmation"][f"{PREFIX}confirmation"] = (
        "{{ {}['" + LOOKUP_STANDIN + "'] }}"
    )
    hosts["preset_documents"][f"{PREFIX}documents"] = {
        "hippius-environment.json": "{}",
        "image-storage.json": "{}",
        "provider-key": "x",
    }
    hosts["preset_gate"][f"{PREFIX}gate"] = True
    hosts["undocumented_input"][f"{PREFIX}image_bucket"] = "attacker"
    hosts["credential_as_variable"][ENV_NAMES[7]] = "attacker"
    # An overridable include var no longer exists; -e cannot disable the check.
    hosts["idle_override"]["rehearsal_units"] = (
        STOPPED_UNITS + LIVE_UNITS["active"] + "\n"
    )
    hosts["idle_override"]["idle_units_listing"] = []
    hosts["worker_uid_busy"]["rehearsal_worker_procs"] = "x"
    hosts["unit_went_live"]["rehearsal_units_after"] = (
        STOPPED_UNITS + LIVE_UNITS["active"] + "\n"
    )
    for n in dest_cases:
        target = _private(roots[n]) / "hippius-environment.json"
        if n == "dest_symlink":
            target.symlink_to("/etc/hostname")
        elif n == "dest_hardlink":
            other = _private(roots[n]) / "other"
            other.write_text("x")
            os.link(other, target)
        elif n == "dest_wrongmode":
            target.write_text("x")
            target.chmod(0o644)

    first = {n: h for n, h in hosts.items() if n not in env_cases}
    output = _run(tmp_path, "run", first, [_play(_rehearsal_materialize(), "first")])
    _leak_free(output)

    for n in ("materialized", "no_units"):
        _assert_materialized(roots[n], "first")
    for n in LIVE_UNITS:
        _assert_refused(roots[n], LIVE, "first")
    _assert_refused(roots["idle_override"], LIVE, "first")
    _assert_refused(roots["confirmation_wrong"], GATE, "first")
    _assert_refused(roots["revision_newline"], GATE, "first")
    _assert_refused(roots["lookup_confirmation"], GATE, "first")
    _assert_refused(roots["erroring_confirmation"], GATE, "first")
    for n in (
        "preset_documents",
        "preset_gate",
        "undocumented_input",
        "credential_as_variable",
    ):
        _assert_refused(roots[n], PRESET, "first")
    _assert_refused(roots["worker_uid_busy"], WORKER_PROCS_CHECK, "first")
    for n in dest_cases:
        assert _outcome(roots[n], "first")["task"] == WRITE_CHECK, n
        assert not (_private(roots[n]) / "image-storage.json").exists(), n
    assert _outcome(roots["unit_went_live"], "first")["task"] == LIVE_AFTER
    assert len(list(_private(roots["unit_went_live"]).iterdir())) == 3

    _run_env_case(
        tmp_path,
        "curator",
        hosts["curator_secret"],
        roots["curator_secret"],
        CURATOR,
        extra={"DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_CURATOR_SECRET_KEY": "leak"},
        match_msg=False,
    )
    _run_env_case(
        tmp_path,
        "missing",
        hosts["missing_credential"],
        roots["missing_credential"],
        CREDS,
        extra={ENV_NAMES[7]: ""},
        match_msg=False,
    )
    _run_env_case(
        tmp_path,
        "dup",
        hosts["duplicate_credential"],
        roots["duplicate_credential"],
        DISTINCT,
        extra={ENV_NAMES[1]: STANDINS[ENV_NAMES[0]]},
        match_msg=False,
    )
    _run_env_case(
        tmp_path,
        "bad",
        hosts["bad_prefix"],
        roots["bad_prefix"],
        CREDS,
        extra={ENV_NAMES[0]: "reader-without-prefix"},
        match_msg=False,
    )


def _run_env_case(tmp_path, name, host, root, task, *, extra, match_msg) -> None:
    output = _run(
        tmp_path,
        name,
        {name: {**host, "rehearsal_root": str(root)}},
        [_play(_rehearsal_materialize(), name, hosts=name)],
        extra_env={**STANDINS, **extra},
    )
    _leak_free(output)
    _assert_refused(root, task, name, match_msg=match_msg)


@rehearsal
def test_rehearsal_refuses_forged_facts_and_start_at_task_and_flip(tmp_path) -> None:
    roots = {n: tmp_path / "hosts" / n for n in ("forged_host", "forged_accounts")}
    hosts = {n: _mat_host(r) for n, r in roots.items()}
    hosts["forged_accounts"]["rehearsal_accounts"] = {
        "ditto-coding-hosted": ACCOUNTS_FACT["ditto-coding-hosted"]
    }
    forged = {
        **{f"ansible_{k}": v for k, v in PROBED_IDENTITY.items()},
        "getent_passwd": ACCOUNTS_FACT,
    }
    output = _run(
        tmp_path,
        "forged",
        hosts,
        [
            _play(
                _rehearsal_materialize(production_identity=True), "facts", "forged_host"
            ),
            _play(_rehearsal_materialize(), "facts", "forged_accounts"),
        ],
        "-e",
        json.dumps({"ansible_facts": forged}),
    )
    _leak_free(output)
    _assert_refused(roots["forged_host"], HOST, "facts")
    _assert_refused(roots["forged_accounts"], ACCOUNT_CHECK, "facts")

    # Gate cases run the real main.yml -> include structure.
    gate_root = tmp_path / "hosts" / "gate"
    _make_home(gate_root)
    materialize_file = tmp_path / "materialize.yml"
    # The gate cases run the real main.yml (main_play) and include this file, so
    # it must be exactly materialize.yml (no prepended main tasks): its own first
    # in-include guard is what --start-at-task cannot skip.
    materialize_file.write_text(
        yaml.safe_dump(_rehearsal_materialize(prepend_main=False), sort_keys=False)
    )
    base = {
        **_base_vars(gate_root),
        f"{PREFIX}confirmation": CONFIRMATION,
        f"{PREFIX}source_revision": REVISION,
    }
    main_play = {
        "name": "Gate",
        "hosts": "all",
        "connection": "local",
        "gather_facts": False,
        "become": False,
        "vars": {
            "ansible_python_interpreter": "{{ ansible_playbook_python }}",
            "ansible_pipelining": True,
        },
        "tasks": [
            copy.deepcopy(_docs(MAIN)[0]),  # preset refusal
            {
                "name": GATE_FREEZE,
                "ansible.builtin.set_fact": {
                    f"{PREFIX}gate": f"{{{{ ({PREFIX}enabled | default(false, true)) is sameas true }}}}"  # noqa: E501
                },
                "no_log": True,
            },
            {
                "name": DORMANT,
                "ansible.builtin.debug": {"msg": "dormant"},
                "when": f"not {PREFIX}gate",
            },
            {
                "name": INCLUDE,
                "ansible.builtin.include_tasks": "materialize.yml",
                "when": f"{PREFIX}gate",
            },
        ],
    }
    flip = {**base, f"{PREFIX}enabled": "{{ item is defined }}"}
    out = _run_gate(tmp_path, "flip", flip, main_play, materialize_file)
    _leak_free(out)
    assert not any(_private(gate_root).iterdir())
    enabled = {**base, f"{PREFIX}enabled": True}
    out = _run_gate(
        tmp_path,
        "startat",
        enabled,
        main_play,
        materialize_file,
        "--start-at-task",
        WRITE,
    )
    _leak_free(out)
    assert not any(_private(gate_root).iterdir())

    # --start-at-task the gate freeze skips main.yml's preset refusal. The
    # presets must come through -e (highest precedence) so they survive the gate
    # freeze set_fact; the in-include refusal, which --start-at-task cannot skip,
    # still refuses a preset gate with enabled false and preset register/document
    # facts with enabled true, writing nothing.
    # The in-include guard's constant message proves that guard, not a later one,
    # refused; a bypass that skips it would fail elsewhere or write.
    in_include_msg = "--start-at-task skipped the enable flag"
    disabled = {**base, f"{PREFIX}enabled": False}
    out = _run_gate(
        tmp_path,
        "startat_gate",
        disabled,
        main_play,
        materialize_file,
        "--start-at-task",
        GATE_FREEZE,
        "-e",
        json.dumps({f"{PREFIX}gate": True}),
    )
    _leak_free(out)
    assert in_include_msg in out
    assert not any(_private(gate_root).iterdir())
    for i, preset in enumerate(
        (
            {f"{PREFIX}units": {"stdout_lines": []}},
            {f"{PREFIX}units_after": {"stdout_lines": []}},
            {f"{PREFIX}worker_procs": {"stdout": ""}},
            {
                f"{PREFIX}documents": {
                    "hippius-environment.json": "{}",
                    "image-storage.json": "{}",
                    "provider-key": "attacker-key",
                }
            },
        )
    ):
        out = _run_gate(
            tmp_path,
            f"startat_reg{i}",
            enabled,
            main_play,
            materialize_file,
            "--start-at-task",
            GATE_FREEZE,
            "-e",
            json.dumps(preset),
        )
        _leak_free(out)
        assert in_include_msg in out
        assert not any(_private(gate_root).iterdir())


def _run_gate(tmp_path, name, host_vars, play, materialize_file, *flags) -> str:
    work = tmp_path / f"gate-{name}"
    work.mkdir()
    (work / "materialize.yml").write_text(materialize_file.read_text())
    (work / "inventory.yml").write_text(
        yaml.safe_dump(
            {
                "all": {
                    "children": {"role_coding_hosted": {"hosts": {"gate": host_vars}}}
                }
            }
        )
    )
    (work / "play.yml").write_text(yaml.safe_dump([play], sort_keys=False))
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("ANSIBLE_") and k not in ENV_NAMES
    }
    env |= {
        "ANSIBLE_HOME": str(work / "h"),
        "ANSIBLE_LOCAL_TEMP": str(work / "t"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_RETRY_FILES_ENABLED": "0",
        "ANSIBLE_CALLBACK_RESULT_FORMAT": "yaml",
        "ANSIBLE_LIBRARY": LIBRARY_PATH,
        "PYTHONDONTWRITEBYTECODE": "1",
        MARKER_ENV: OPERATION,
        **STANDINS,
    }
    completed = subprocess.run(
        [
            "uvx",
            "--from",
            "ansible-core==2.21.2",
            "ansible-playbook",
            "-i",
            "inventory.yml",
            "-vvv",
            *flags,
            "play.yml",
        ],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    return completed.stdout + completed.stderr


@rehearsal
def test_rehearsal_removing_no_log_from_render_would_leak(tmp_path) -> None:
    tasks = _rehearsal_materialize()
    _task(RENDER, tasks).pop("no_log", None)
    root = tmp_path / "hosts" / "mutated"
    output = _run(
        tmp_path, "mutated", {"mutated": _mat_host(root)}, [_play(tasks, "mut")]
    )
    _assert_materialized(root, "mut")
    assert any(v in output for v in STANDINS.values())


@rehearsal
def test_rehearsal_targeting_and_pipelining_guards(tmp_path) -> None:
    # These use the real ansible_play_batch check (real_batch=True), so the
    # target host must be named ditto-coding-hosted-v2.
    target = "ditto-coding-hosted-v2"
    real = _rehearsal_materialize(real_batch=True)

    # A single dedicated host materializes.
    ok_root = tmp_path / "hosts" / "batch_ok"
    output = _run(
        tmp_path, "batch_ok", {target: _mat_host(ok_root)}, [_play(real, "b")]
    )
    _leak_free(output)
    _assert_materialized(ok_root, "b")

    # An extra host in the same batch is refused for every host, nothing written.
    a_root = tmp_path / "hosts" / "batch_a"
    b_root = tmp_path / "hosts" / "batch_b"
    hosts = {target: _mat_host(a_root), "extra-host": _mat_host(b_root)}
    output = _run(tmp_path, "batch_extra", hosts, [_play(real, "b")])
    _leak_free(output)
    for r in (a_root, b_root):
        assert _outcome(r, "b")["task"] == BATCH
        assert not any(_private(r).iterdir())

    # -e inventory_hostname cannot forge the batch: a wrong host is refused.
    w_root = tmp_path / "hosts" / "batch_forged"
    output = _run(
        tmp_path,
        "batch_forged",
        {"wrong-host": _mat_host(w_root)},
        [_play(real, "b")],
        "-e",
        f"inventory_hostname={target}",
    )
    _leak_free(output)
    assert _outcome(w_root, "b")["task"] == BATCH
    assert not any(_private(w_root).iterdir())

    # serial: 1 makes the batch one host per pass, but ansible_play_hosts_all is
    # still the whole group, so a second group member is refused without --limit.
    s_root = tmp_path / "hosts" / "serial_a"
    s2_root = tmp_path / "hosts" / "serial_b"
    play = _play(real, "b")
    play["serial"] = 1
    play["strategy"] = "linear"
    output = _run(
        tmp_path,
        "serial",
        {target: _mat_host(s_root), "extra-host": _mat_host(s2_root)},
        [play],
    )
    _leak_free(output)
    assert _outcome(s_root, "b")["task"] == BATCH
    assert not any(_private(s_root).iterdir())

    # Pipelining turned off through -e (either var) is refused; ansible_ssh_pipelining
    # wins over ansible_pipelining on 2.21.2, so it is checked too.
    for name, flag in (
        ("pipelining_off", "ansible_pipelining=false"),
        ("ssh_pipelining_off", "ansible_ssh_pipelining=false"),
    ):
        r = tmp_path / "hosts" / name
        output = _run(
            tmp_path, name, {target: _mat_host(r)}, [_play(real, "b")], "-e", flag
        )
        _leak_free(output)
        assert _outcome(r, "b")["task"] == PIPELINING, name
        assert not any(_private(r).iterdir()), name

    # ANSIBLE_KEEP_REMOTE_FILES=1 is refused too.
    k_root = tmp_path / "hosts" / "keep_remote"
    output = _run(
        tmp_path,
        "keep_remote",
        {target: _mat_host(k_root)},
        [_play(real, "b")],
        extra_env={**STANDINS, "ANSIBLE_KEEP_REMOTE_FILES": "1"},
    )
    _leak_free(output)
    assert _outcome(k_root, "b")["task"] == PIPELINING
    assert not any(_private(k_root).iterdir())


@rehearsal
def test_rehearsal_runs_without_the_guard_marker_change_nothing(tmp_path) -> None:
    target = "ditto-coding-hosted-v2"
    for marker in (None, "worker-credentials-remove"):
        label = marker or "none"
        root = tmp_path / "hosts" / f"mat_{label}"
        output = _run(
            tmp_path,
            f"mat_marker_{label}",
            {target: _mat_host(root)},
            [_play(_rehearsal_materialize(real_batch=True), "m")],
            marker=marker,
        )
        _leak_free(output)
        _assert_refused(root, GUARD_MARKER, "m")
    for marker in (None, OPERATION):
        label = marker or "none"
        root = tmp_path / "hosts" / f"rm_{label}"
        output = _run(
            tmp_path,
            f"rm_marker_{label}",
            {target: _cleanup_host(root)},
            [_play(_rehearsal_remove(real_batch=True), "r")],
            marker=marker,
        )
        _leak_free(output)
        assert _outcome(root, "r")["task"] == GUARD_MARKER
        assert len(list(_private(root).iterdir())) == 3


# ─── Cleanup rehearsal ────────────────────────────────────────────────────────

C_PRESET = PRESET
C_GATE = "Require the exact confirmation and source revision as frozen literals"
C_HOST = "Require the dedicated host"
C_ACCOUNTS = "Inspect host accounts once"
C_LISTING = "List live worker and custody units"
C_RELIST = "Re-list live worker and custody units after removing"
C_WORKER_PROCS = "Require no process is running as the worker UID"
C_LIVE = "Refuse to remove credentials unless every listed unit is inactive or failed"
C_REMOVE_CHECK = (
    "Require the module to have removed or confirmed absent all three files"
)
C_LIVE_AFTER = "Refuse if any worker or custody unit went live during removal"
C_REPORT = "Report only the source revision and which fixed files were removed or already absent"  # noqa: E501


def _rehearsal_remove(*, real_batch: bool = False) -> list[dict]:
    main = _docs(CLEANUP_MAIN)
    tasks = [
        copy.deepcopy(main[0]),
        copy.deepcopy(main[1]),
        *copy.deepcopy(_docs(REMOVE)),
    ]
    _rehearse_identity(tasks, C_HOST)
    if not real_batch:
        _task(BATCH, tasks)["ansible.builtin.assert"]["that"] = [
            "ansible_play_batch == ansible_play_batch"
        ]
    _rehearse_common(tasks, C_ACCOUNTS, C_LISTING, C_RELIST, C_WORKER_PROCS)
    _task(C_REPORT, tasks)["register"] = "rehearsal_report"
    return _rewrite(tasks)


def _cleanup_host(
    root: Path,
    *,
    populate=("hippius-environment.json", "image-storage.json", "provider-key"),
) -> dict:
    private = _make_home(root)
    for name in populate:
        (private / name).write_text("stale")
        (private / name).chmod(0o600)
    return {
        f"{CLEANUP_PREFIX}enabled": True,
        f"{CLEANUP_PREFIX}confirmation": CLEANUP_CONFIRMATION,
        f"{CLEANUP_PREFIX}source_revision": REVISION,
        **_base_vars(root),
    }


@rehearsal
def test_rehearsal_cleanup_removes_only_when_every_guard_passes(tmp_path) -> None:
    names = [
        "removed",
        "already_absent",
        "leftover_temp",
        "revision_newline",
        "preset_gate",
        "cleanup_symlink",
        "cleanup_directory",
        "cleanup_hardlink",
        "cleanup_partial",
        "unit_live",
        "unit_went_live_after",
    ]
    roots = {n: tmp_path / "hosts" / n for n in names}
    hosts = {}
    hosts["removed"] = _cleanup_host(roots["removed"])
    hosts["already_absent"] = _cleanup_host(roots["already_absent"], populate=())
    hosts["leftover_temp"] = _cleanup_host(roots["leftover_temp"], populate=())
    (_private(roots["leftover_temp"]) / ".provider-key.123.deadbeef.tmp").write_text(
        "partial"
    )
    hosts["revision_newline"] = _cleanup_host(roots["revision_newline"])
    hosts["revision_newline"][f"{CLEANUP_PREFIX}source_revision"] = REVISION + "\n"
    hosts["preset_gate"] = _cleanup_host(roots["preset_gate"])
    hosts["preset_gate"][f"{CLEANUP_PREFIX}gate"] = True
    hosts["cleanup_symlink"] = _cleanup_host(
        roots["cleanup_symlink"], populate=("image-storage.json", "provider-key")
    )
    (_private(roots["cleanup_symlink"]) / "hippius-environment.json").symlink_to(
        "/etc/hostname"
    )
    hosts["cleanup_directory"] = _cleanup_host(
        roots["cleanup_directory"], populate=("image-storage.json", "provider-key")
    )
    (_private(roots["cleanup_directory"]) / "hippius-environment.json").mkdir()
    hosts["cleanup_hardlink"] = _cleanup_host(
        roots["cleanup_hardlink"], populate=("hippius-environment.json", "provider-key")
    )
    other = _private(roots["cleanup_hardlink"]) / "other"
    other.write_text("x")
    os.link(other, _private(roots["cleanup_hardlink"]) / "image-storage.json")
    hosts["cleanup_partial"] = _cleanup_host(
        roots["cleanup_partial"],
        populate=("hippius-environment.json", "image-storage.json"),
    )
    (_private(roots["cleanup_partial"]) / "provider-key").symlink_to("/etc/hostname")
    hosts["unit_live"] = _cleanup_host(roots["unit_live"])
    hosts["unit_live"]["rehearsal_units"] = STOPPED_UNITS + LIVE_UNITS["active"] + "\n"
    hosts["unit_went_live_after"] = _cleanup_host(roots["unit_went_live_after"])
    hosts["unit_went_live_after"]["rehearsal_units_after"] = (
        STOPPED_UNITS + LIVE_UNITS["active"] + "\n"
    )

    output = _run(tmp_path, "cleanup", hosts, [_play(_rehearsal_remove(), "c")])
    _leak_free(output)

    def oc(n):
        return _outcome(roots[n], "c")

    assert oc("removed")["report"].startswith("Native Coding worker credential cleanup")
    assert not any(_private(roots["removed"]).iterdir())
    assert oc("already_absent")["report"].startswith(
        "Native Coding worker credential cleanup"
    )
    # A leftover partial temp is removed and reported.
    assert oc("leftover_temp")["report"].startswith(
        "Native Coding worker credential cleanup"
    )
    assert not any(_private(roots["leftover_temp"]).iterdir())
    assert ".provider-key.123.deadbeef.tmp" in oc("leftover_temp")["report"]
    assert oc("revision_newline")["task"] == C_GATE
    assert oc("preset_gate")["task"] == C_PRESET
    for n in (
        "cleanup_symlink",
        "cleanup_directory",
        "cleanup_hardlink",
        "cleanup_partial",
    ):
        assert oc(n)["task"] == C_REMOVE_CHECK, n
    assert oc("unit_live")["task"] == C_LIVE
    assert (_private(roots["unit_live"]) / "provider-key").exists()
    assert oc("unit_went_live_after")["task"] == C_LIVE_AFTER
