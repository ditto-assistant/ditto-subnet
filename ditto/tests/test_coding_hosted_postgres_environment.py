"""Native PostgreSQL environment materialization is default-off, unforgeable, silent."""

import copy
import grp
import hashlib
import importlib.util
import json
import os
import pwd
import re
import shutil
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/coding_hosted_postgres_environment"
MAIN = (ROLE / "tasks/main.yml").read_text()
MATERIALIZE_TEXT = (ROLE / "tasks/materialize.yml").read_text()
# Parsed materialize tasks without YAML comments, for token scans.
PARSED = yaml.safe_dump(yaml.safe_load(MATERIALIZE_TEXT), width=10_000)
PLAYBOOK = ROOT / "infra/ansible/playbooks/gcp-coding-hosted-postgres-environment.yml"
FIXTURE = ROOT / "infra/ansible/tests/coding-hosted-postgres-environment.yml"
# The stacked removal role (#1897). Its guards are duplicated, not shared; the
# two roles have deliberately diverged (this one is hardened further), so only
# the parts that must stay identical are compared, when both are on the tree.
CLEANUP_ROLE = ROOT / "infra/ansible/roles/coding_hosted_postgres_environment_cleanup"

PASSWORD_LOOKUP = "lookup('env', 'DITTO_CODING_PG_PASSWORD')"
PFX = "coding_hosted_postgres_environment_"
INPUTS = {
    f"{PFX}enabled": False,
    f"{PFX}confirmation": "",
    f"{PFX}source_revision": "",
    f"{PFX}host": "",
}
CONFIRMATION = "MATERIALIZE NATIVE CODING POSTGRES ENVIRONMENT"
REVISION = "0123456789abcdef0123456789abcdef01234567"
DATABASE_HOST = "10.30.0.5"
CAPTURED_GATE = f"{PFX}captured_enabled"
GATE = f"{CAPTURED_GATE} is sameas true"
REHEARSAL_GATE = "DITTO_ANSIBLE_REHEARSAL"
# inventory_hostname and group_names are host variables extra vars override;
# groups and ansible_play_hosts_all are magic variables they cannot.
GROUP_CHECK = (
    "ansible_play_hosts_all | difference(groups.get('role_coding_hosted', [])) "
    "| length == 0"
)
NON_OVERRIDABLE_MAGIC = {
    "groups",
    "ansible_play_hosts_all",
    "ansible_play_batch",
    "ansible_check_mode",
}
# Connection variables extra vars can override, but the role only ever compares
# them with sameas true. On 2.21.2 the ssh plugin reads ansible_pipelining then
# ansible_ssh_pipelining (the later wins) and both outrank the environment and
# ini, so an override can only make the guard refuse or genuinely enable
# pipelining.
SAMEAS_TRUE_CONNECTION_VARS = {"ansible_pipelining", "ansible_ssh_pipelining"}
PIPELINING_CHECKS = [
    "ansible_pipelining is sameas true",
    "ansible_ssh_pipelining | default(true) is sameas true",
    "lookup('ansible.builtin.config', 'DEFAULT_KEEP_REMOTE_FILES') is sameas false",
]
HOSTS_ALL_PIN = "ansible_play_hosts_all == ['ditto-coding-hosted-v2']"
BATCH_PIN = "ansible_play_batch == ['ditto-coding-hosted-v2']"

CUSTODY = "/var/lib/ditto-coding-custody/private/postgres-environment.json"
HOSTED = "/var/lib/ditto-coding-hosted/private/postgres-environment.json"
COPIES = [
    {"path": CUSTODY, "owner": "ditto-coding-custody"},
    {"path": HOSTED, "owner": "ditto-coding-hosted"},
]
OWNERS = ("ditto-coding-custody", "ditto-coding-hosted")
ENTRIES = [
    "POSTGRES_HOST={{ coding_hosted_postgres_environment_captured_host }}",
    "POSTGRES_PORT=5432",
    "POSTGRES_USER=ditto",
    "POSTGRES_DB=ditto_platform_prod",
    "POSTGRES_COMMAND_TIMEOUT=30",
    "POSTGRES_POOL_MIN_SIZE=1",
    "POSTGRES_POOL_MAX_SIZE=4",
]

CAPTURE_GATE = "Capture the materialization gate once, neutralising templates and loops"
INCLUDE = "Materialize the native PostgreSQL environment only when explicitly enabled"
EXPLAIN = "Explain dormant native PostgreSQL environment materialization"
PASSWORD_VARIABLE = "Refuse a password supplied as an Ansible variable"
PRESET = "Refuse preset registered results and undocumented role inputs"
IDENTITY = "Probe this machine's identity into a result extra vars cannot preset"
ACCOUNTS = "Inspect host accounts once"
CAPTURE = "Capture the operator inputs once, neutralising templates and loops"
HOST = "Require the exact host, source, database address and confirmation"
PASSWORD = "Require one bounded single-line password from the controller environment"
ACCOUNT_CHECK = "Require the distinct worker and custodian identities"
LISTING = "List live worker and custody units"
LIVE = "Refuse to replace credentials unless every listed unit is inactive or failed"
ASSEMBLE = "Assemble the fixed environment entries from the captured host"
RENDER = "Render the environment document once with the controller-only password"
WRITE = "Write each reader's own PostgreSQL environment copy through pinned directories"
RELIST_WRITE = "Re-list live worker and custody units after writing"
LIVE_WRITE = "Refuse if any unit became active during the write"
DIGEST = "Require both copies to hold exactly the rendered document"
RELIST_VERIFY = "Re-list live worker and custody units after verifying"
LIVE_VERIFY = "Refuse if any unit became active during verification"
REPORT = "Report only that the copies exist"
RAW_GATE = "Require the raw enabled flag to be a boolean true inside the include"
CHECK_MODE = "Refuse check mode, which cannot verify the write"
GUARD_MARKER = "Require the guarded entry point marker, an accident guard only"
OPERATION = "postgres-environment-materialize"
MARKER_ENV = "DITTO_CODING_HOSTED_GUARDED_RUN"
SPEC_PATH = ROOT / f"infra/ansible/guarded-runs/{OPERATION}.json"
PIPELINING = (
    "Require pipelining on and remote files not kept before reading the password"
)
WRITE_MODULE = "coding_hosted_postgres_environment_write"
WRITE_MODULE_PATH = ROLE / f"library/{WRITE_MODULE}.py"

# The one allow-list regex, shared with the initial, post-write and post-verify
# live-unit checks and with the cleanup role.
UNIT_PATTERN = (
    "^(ditto-coding-hosted-worker[.]service|ditto-coding-custody@\\\\S+[.]service)"
    "\\\\s+\\\\S+\\\\s+(inactive|failed)(\\\\s|$)"
)
LISTING_ARGV = [
    "/usr/bin/systemctl",
    "list-units",
    "--all",
    "--plain",
    "--no-legend",
    "--full",
    "ditto-coding-hosted-worker.service",
    "ditto-coding-custody@*.service",
]


def _materialize() -> list[dict]:
    return yaml.safe_load(MATERIALIZE_TEXT)


def _main() -> list[dict]:
    return yaml.safe_load(MAIN)


def _walk(tasks: list[dict]) -> Iterator[dict]:
    for task in tasks:
        yield task
        for section in ("block", "rescue", "always"):
            yield from _walk(task.get(section, []))


def _task(name: str, tasks: list[dict] | None = None) -> dict:
    (task,) = [t for t in _walk(tasks or _materialize()) if t.get("name") == name]
    return task


def _flat(text: object) -> str:
    return " ".join(str(text).split())


def _live_check(task: dict) -> str:
    (that,) = task["ansible.builtin.assert"]["that"]
    return _flat(that)


def _expected_live_check(register: str) -> str:
    return _flat(
        f"{register}.stdout_lines | reject('match', '{UNIT_PATTERN}') "
        "| list | length == 0"
    )


# --------------------------------------------------------------------------- #
# Structural tests                                                            #
# --------------------------------------------------------------------------- #


def test_default_off_gate_is_decided_once_behind_a_dynamic_include() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults == INPUTS
    main = _main()
    # A no_log capture of the flag, a dynamic include gated on the captured fact,
    # and the dormant explanation. No block, no per-task gate to re-evaluate.
    assert [t["name"] for t in main] == [CAPTURE_GATE, INCLUDE, EXPLAIN]
    capture, include, explain = main
    assert capture["no_log"] is True
    assert capture["ansible.builtin.set_fact"] == {
        CAPTURED_GATE: f"{{{{ ({PFX}enabled | default(false, true)) is sameas true }}}}"
    }
    assert include["ansible.builtin.include_tasks"] == "materialize.yml"
    assert _flat(include["when"]) == GATE
    assert _flat(explain["when"]) == f"not ({GATE})"
    parsed_main = yaml.safe_dump(main)
    assert "import_tasks" not in parsed_main  # static would defeat --start-at-task
    assert "block" not in parsed_main
    # The raw flag is rendered only by the capture.
    assert [t["name"] for t in main if f"{PFX}enabled" in json.dumps(t)] == [
        CAPTURE_GATE
    ]
    (play,) = yaml.safe_load(PLAYBOOK.read_text())
    assert play["gather_facts"] is False


def test_no_bool_filter_can_print_a_coerced_value() -> None:
    # ansible-core 2.21 prints any non-boolean string the bool filter coerces in
    # a deprecation warning, even under no_log. Every boolean check, including
    # the gate and the pipelining guard on -e overridable connection variables,
    # uses sameas instead, so neither task file uses the bool filter.
    for text in (yaml.safe_dump(_main()), PARSED):
        assert not re.search(r"\|\s*bool\b", text)


def test_preset_and_password_guards_run_first_as_the_single_source_of_truth() -> None:
    tasks = _materialize()
    # The raw-gate and check-mode refusals lead; the password and preset guards
    # follow, still before any register or set_fact.
    assert [t["name"] for t in tasks[:5]] == [
        RAW_GATE,
        GUARD_MARKER,
        CHECK_MODE,
        PASSWORD_VARIABLE,
        PRESET,
    ]
    # Nothing is registered or set before the guards, so at guard time only the
    # documented inputs carry the prefix.
    before = tasks[: tasks.index(_task(PRESET))]
    assert not any("register" in t or "ansible.builtin.set_fact" in t for t in before)

    # Names are tested with varnames, which never renders a value; 'is defined'
    # would render a raising template and print its error.
    assert _task(PASSWORD_VARIABLE)["ansible.builtin.assert"]["that"] == [
        f"lookup('ansible.builtin.varnames', '^{PFX}password$', wantlist=True) == []"
    ]
    assert "is defined" not in PARSED and "is not defined" not in PARSED
    that = _task(PRESET)["ansible.builtin.assert"]["that"]
    # The varnames equality is the one source of truth for the prefix, with no
    # per-name "is not defined" lines; the loop item is refused by name too.
    assert len(that) == 2
    assert (
        that[1] == "lookup('ansible.builtin.varnames', '^item$', wantlist=True) == []"
    )
    allowed = sorted([*INPUTS, CAPTURED_GATE])
    assert _flat(that[0]) == _flat(
        f"lookup('ansible.builtin.varnames', '^{PFX}', wantlist=True) "
        f"| reject('match', '^{PFX}cleanup_') "
        "| sort == [" + ", ".join(f"'{name}'" for name in allowed) + "]"
    )
    # main.yml captures only the gate before this guard.
    main_facts = [
        name for task in _main() for name in task.get("ansible.builtin.set_fact", {})
    ]
    assert main_facts == [CAPTURED_GATE]
    # The _cleanup_ exclusion keeps the removal role's variables from producing a
    # misleading refusal here.
    assert f"reject('match', '^{PFX}cleanup_')" in _flat(that[0])
    assert "Nothing was written" in _task(PRESET)["ansible.builtin.assert"]["fail_msg"]


_JINJA_WORDS = {"and", "or", "not", "in", "is", "if", "else", "true", "false", "none"}
_JINJA_WORDS |= {"True", "False", "None"}


def _jinja_roots(expression: str) -> set[str]:
    """Top-level variable names an expression reads: no attributes, filters,
    tests, keyword arguments, string literals or Jinja keywords."""
    code = re.sub(r"'[^']*'|\"[^\"]*\"", "''", expression)
    roots = set()
    for match in re.finditer(r"[A-Za-z_]\w*", code):
        before = code[: match.start()].rstrip()
        after = code[match.end() :].lstrip()
        name = match.group()
        if name in _JINJA_WORDS or before.endswith((".", "|")):
            continue
        if re.search(r"\bis(\s+not)?$", before) or re.match(r"=(?!=)", after):
            continue
        roots.add(name)
    return roots


def _task_roots(task: dict) -> set[str]:
    roots: set[str] = set()

    def visit(value: object, expression: bool) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, key in ("when", "that", "changed_when", "failed_when"))
        elif isinstance(value, list):
            for item in value:
                visit(item, expression)
        elif isinstance(value, str):
            if expression:
                roots.update(_jinja_roots(value))
            for template in re.findall(r"{{(.*?)}}", value, re.S):
                roots.update(_jinja_roots(template))

    visit({k: v for k, v in task.items() if k != "name"}, False)
    return roots


def test_every_variable_read_is_an_input_a_refused_name_or_unforgeable() -> None:
    # Extra vars override registered results, set_facts, include vars and host
    # variables such as inventory_hostname and group_names. Every variable these
    # tasks read must therefore be a documented input, a prefixed name the preset
    # guard refuses, the loop item it also refuses, or a magic variable extra vars
    # cannot override.
    created = {
        name
        for task in _walk(_main() + _materialize())
        for name in [task.get("register"), *task.get("ansible.builtin.set_fact", {})]
        if name
    }
    assert all(name.startswith(PFX) for name in created)
    for task in _walk(_main() + _materialize()):
        for root in _task_roots(task):
            assert (
                root in INPUTS
                or root in created
                or root in {"item", "lookup"}
                or root in NON_OVERRIDABLE_MAGIC
                or (root in SAMEAS_TRUE_CONNECTION_VARS and task["name"] == PIPELINING)
            ), (task["name"], root)
    assert "inventory_hostname" not in PARSED and "group_names" not in PARSED
    # Every message is fixed text: nothing a variable could replace is rendered.
    for task in _walk(_materialize()):
        for arguments in task.values():
            if isinstance(arguments, dict):
                for key in ("fail_msg", "msg"):
                    assert "{{" not in str(arguments.get(key, "")), task["name"]


def test_target_modules_that_touch_the_password_run_under_no_log() -> None:
    # A module invoked without no_log writes "Invoked with <params>" to the
    # target's journal and returns its invocation under ansible_inject_invocation.
    # Every target-side module whose arguments, loop or result carry the password,
    # the document, a captured input or a checksum therefore runs under no_log.
    controller = {
        "ansible.builtin.assert",
        "ansible.builtin.set_fact",
        "ansible.builtin.debug",
        "ansible.builtin.include_tasks",
    }
    sensitive = [
        "DITTO_CODING_PG_PASSWORD",
        f"{PFX}document",
        f"{PFX}entries",
        f"{PFX}captured_",
        f"{PFX}written",
        "checksum",
    ]
    for task in _walk(_materialize()):
        if controller & set(task):
            continue
        if any(marker in json.dumps(task) for marker in sensitive):
            assert task.get("no_log") is True, task["name"]
    write = _task(WRITE)
    assert write["no_log"] is True
    # The document is a no_log argument-spec param, so the module never logs its
    # invocation to the target's journal and returns nothing sensitive.
    source = WRITE_MODULE_PATH.read_text()
    assert '"content": {"type": "str", "required": True, "no_log": True}' in source


def test_identity_and_accounts_come_from_registered_probes_no_facts_gathered() -> None:
    identity = _task(IDENTITY)
    assert identity["ansible.builtin.setup"] == {
        "gather_subset": ["!all", "!min", "platform", "distribution"]
    }
    assert identity["register"] == f"{PFX}identity"
    accounts = _task(ACCOUNTS)
    assert accounts["ansible.builtin.getent"] == {"database": "passwd"}
    assert accounts["register"] == f"{PFX}accounts"

    that = _task(HOST)["ansible.builtin.assert"]["that"]
    assert that[:7] == [
        GROUP_CHECK,
        HOSTS_ALL_PIN,
        BATCH_PIN,
        *(
            f"{PFX}identity.ansible_facts.ansible_{key} == '{value}'"
            for key, value in PROBED_IDENTITY.items()
        ),
    ]
    account_lines = _task(ACCOUNT_CHECK)["ansible.builtin.assert"]["that"]
    assert all(
        line.startswith(f"{PFX}accounts.ansible_facts.getent_passwd")
        for line in account_lines
    )
    # No guard reads gathered facts: every ansible_facts reference is via a
    # registered probe.
    assert PARSED.count("ansible_facts") == (
        PARSED.count(f"{PFX}identity.ansible_facts")
        + PARSED.count(f"{PFX}accounts.ansible_facts")
    )


def test_inputs_are_captured_once_with_a_template_error_guard() -> None:
    capture = _task(CAPTURE)
    assert capture["no_log"] is True
    assert capture["ansible.builtin.set_fact"] == {
        f"{PFX}captured_host": f"{{{{ {PFX}host | default('', true) }}}}",
        f"{PFX}captured_confirmation": (
            f"{{{{ {PFX}confirmation | default('', true) }}}}"
        ),
        f"{PFX}captured_revision": (
            f"{{{{ {PFX}source_revision | default('', true) }}}}"
        ),
    }
    # After the capture, no task references a raw operator input: every later
    # task reads the captured, validated value, so a lazily templated value
    # cannot render differently inside a loop and no later task can render an
    # operator template into an error message. (The two guards above name the
    # inputs only as string literals in the varnames allow-list.)
    tasks = _materialize()
    after_capture = tasks[tasks.index(_task(CAPTURE)) + 1 :]
    for raw in (f"{PFX}host", f"{PFX}confirmation", f"{PFX}source_revision"):
        offenders = [
            t["name"]
            for t in _walk(after_capture)
            if re.search(rf"\b{raw}\b", json.dumps(t))
        ]
        assert offenders == [], (raw, offenders)
    # The document is built from the captured host, not the raw input.
    assert _set_fact(ASSEMBLE)[f"{PFX}entries"] == ENTRIES
    assert _flat(_set_fact(RENDER)[f"{PFX}document"]) == _flat(
        f"{{{{ ({PFX}entries + ['POSTGRES_PASSWORD=' ~ {PASSWORD_LOOKUP}]) "
        "| to_json }}"
    )


PROBED_IDENTITY = {
    "hostname": "ditto-coding-hosted-v2",
    "architecture": "x86_64",
    "distribution": "Debian",
    "distribution_major_version": "13",
}


def _set_fact(name: str) -> dict:
    return _task(name)["ansible.builtin.set_fact"]


def test_revision_and_database_host_refuse_a_trailing_newline() -> None:
    that = _task(HOST)["ansible.builtin.assert"]["that"]
    host_pattern = "^10[.]30[.]0[.]([2-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-3])$"
    assert that[7:] == [
        f"{PFX}captured_revision is string",
        f"{PFX}captured_revision is match('^[0-9a-f]{{40}}$')",
        f"{PFX}captured_revision | length == 40",
        f"{PFX}captured_confirmation == '{CONFIRMATION}'",
        f"{PFX}captured_host is string",
        f"{PFX}captured_host == {PFX}captured_host | trim",
        f"{PFX}captured_host is match('{host_pattern}')",
    ]
    # A '$' anchor alone accepts one trailing newline; the exact length and the
    # trim comparison are what refuse it.
    assert re.match("^[0-9a-f]{40}$", REVISION + "\n")
    assert len(REVISION + "\n") != 40
    assert re.match(host_pattern, DATABASE_HOST + "\n")
    assert (DATABASE_HOST + "\n").strip() != DATABASE_HOST + "\n"


ALLOWED_UNIT_LINES = [
    "ditto-coding-hosted-worker.service loaded inactive dead Worker",
    "ditto-coding-hosted-worker.service   loaded    failed   failed   Worker",
    "ditto-coding-custody@0.service not-found inactive dead ditto-coding-custody@0",
    "ditto-coding-custody@abc-1.service loaded inactive dead",
]
REFUSED_UNIT_LINES = [
    "ditto-coding-hosted-worker.service loaded active running Worker",
    "ditto-coding-hosted-worker.service loaded activating start Worker",
    "ditto-coding-custody@0.service loaded deactivating stop-sigterm Custody",
    "ditto-coding-custody@0.service loaded reloading reload Custody",
    "ditto-coding-custody@0.service loaded refreshing refresh-extensions Custody",
    "ditto-coding-custody@0.service loaded maintenance cleaning Custody",
    "ditto-coding-custody@0.service loaded quiescent future Custody",
    "ditto-coding-custody@0.service loaded inactivefuture dead Custody",
    "● ditto-coding-custody@0.service loaded inactive dead Custody",
    "other.service loaded inactive dead Other",
    "ditto-coding-custody@.service loaded inactive dead Custody",
    "ditto-coding-custody@0.service inactive",
    " ",
]


def test_live_unit_refusal_is_an_allow_list() -> None:
    listing = _task(LISTING)
    assert listing["ansible.builtin.command"]["argv"] == LISTING_ARGV
    assert listing["register"] == f"{PFX}units"
    assert listing["check_mode"] is False and listing["changed_when"] is False
    assert _live_check(_task(LIVE)) == _expected_live_check(f"{PFX}units")
    regex = re.compile(UNIT_PATTERN.replace("\\\\", "\\"))
    for line in ALLOWED_UNIT_LINES:
        assert regex.match(line), line
    for line in REFUSED_UNIT_LINES:
        assert not regex.match(line), line
    # The listing is the only systemctl use; nothing is stopped or started.
    assert PARSED.count("systemctl") == 3  # initial + after write + after verify
    for forbidden in ("systemd:", "service:", "state: stopped", "state: started"):
        assert forbidden not in PARSED, forbidden


def test_unit_state_is_rechecked_after_write_and_after_verify() -> None:
    names = [t["name"] for t in _materialize()]
    # Three listings, each with the same argv, and each write-window listing is
    # immediately followed by its allow-list refusal.
    for listing, register, check in (
        (LISTING, f"{PFX}units", LIVE),
        (RELIST_WRITE, f"{PFX}units_after_write", LIVE_WRITE),
        (RELIST_VERIFY, f"{PFX}units_after_verify", LIVE_VERIFY),
    ):
        assert _task(listing)["ansible.builtin.command"]["argv"] == LISTING_ARGV
        assert _task(listing)["register"] == register
        assert names.index(check) == names.index(listing) + 1
        assert _live_check(_task(check)) == _expected_live_check(register)
    # The re-checks straddle the write and the verify, and their refusals warn
    # that a copy may have been read mid-rotation.
    assert names.index(RELIST_WRITE) == names.index(WRITE) + 1
    assert names.index(RELIST_VERIFY) == names.index(DIGEST) + 1
    for check in (LIVE_WRITE, LIVE_VERIFY):
        assert "mid-rotation" in _task(check)["ansible.builtin.assert"]["fail_msg"]


def test_credentials_are_never_logged_read_back_or_diffed() -> None:
    write = _task(WRITE)
    assert write["no_log"] is True
    for forbidden in ("slurp", "fetch", "set -x", "gcloud secrets", "extra_vars"):
        assert forbidden not in MATERIALIZE_TEXT
    report = _task(REPORT)["ansible.builtin.debug"]["msg"]
    assert "password" not in report.lower()
    assert "{{" not in report
    # Every task that touches the password, the document or the captured inputs
    # is no_log; the write result carries the digest, so it is too.
    for name in (CAPTURE, PASSWORD, ASSEMBLE, RENDER, WRITE, DIGEST):
        assert _task(name).get("no_log") is True, name


def test_copies_are_written_and_verified_through_the_pinned_write_module() -> None:
    # The copy is written by the role-local module, never by copy/file/stat, so
    # a swapped symlink cannot be followed and no follow=false stat can be fooled.
    write = _task(WRITE)
    assert set(write) == {
        "name",
        WRITE_MODULE,
        "loop",
        "loop_control",
        "register",
        "no_log",
    }
    assert write[WRITE_MODULE] == {
        "path": "{{ item.path }}",
        "owner": "{{ item.owner }}",
        "content": f"{{{{ {PFX}document }}}}",
    }
    assert write["loop"] == COPIES
    assert write["register"] == f"{PFX}written"
    for forbidden in (
        "ansible.builtin.copy",
        "ansible.builtin.file",
        "ansible.builtin.stat",
        "ansible.builtin.template",
    ):
        assert forbidden not in MATERIALIZE_TEXT, forbidden
    # The digest check reads the module's returned state and checksum only.
    digest = _task(DIGEST)
    assert digest["ansible.builtin.assert"]["that"] == [
        "item.state == 'written'",
        f"item.checksum == {PFX}document | hash('sha256')",
    ]
    assert digest["loop"] == f"{{{{ {PFX}written.results }}}}"
    # The module opens every component with O_NOFOLLOW and writes atomically.
    source = _flat(WRITE_MODULE_PATH.read_text())
    assert "os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC" in source
    assert "os.open(component, _DIRECTORY_FLAGS, dir_fd=fd)" in source
    assert "os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW" in source
    assert (
        "os.rename(temp, NAME, src_dir_fd=private_fd, dst_dir_fd=private_fd)" in source
    )
    assert "def recheck_parents" in source


def test_write_module_refuses_swapped_parents_and_writes_atomically(tmp_path) -> None:
    tmp_path = tmp_path.resolve()
    module = _write_module()
    uid = os.getuid()
    home = tmp_path / "var/lib/ditto-coding-hosted"
    home.mkdir(parents=True, mode=0o700)
    path = str(home / "private/postgres-environment.json")
    state, checksum = module.write_copy(path, uid, '["POSTGRES_PASSWORD=x"]')
    written = Path(home / "private/postgres-environment.json")
    assert state == "written"
    assert written.read_text() == '["POSTGRES_PASSWORD=x"]'
    assert written.stat().st_mode & 0o777 == 0o600 and written.stat().st_nlink == 1
    assert written.parent.stat().st_mode & 0o777 == 0o700
    # A private swapped for a symlink to a root-ish victim dir is refused, and the
    # victim stays empty: the write never follows the link.
    # The victim is owned by the account at 0700, so only O_NOFOLLOW refuses.
    victim = tmp_path / "victim"
    victim.mkdir(mode=0o700)
    victim.chmod(0o700)
    home2 = tmp_path / "var/lib/ditto-coding-custody"
    home2.mkdir(parents=True, mode=0o700)
    (home2 / "private").symlink_to(victim)
    with pytest.raises(module.UnsafeCopy):
        module.write_copy(str(home2 / "private/postgres-environment.json"), uid, "z")
    assert list(victim.iterdir()) == []
    # A group-writable home is refused.
    home.chmod(0o770)
    with pytest.raises(module.UnsafeCopy):
        module.write_copy(path, uid, "y")
    home.chmod(0o700)
    # check mode writes nothing.
    home3 = tmp_path / "var/lib/ditto-coding-hosted-c"
    home3.mkdir(parents=True, mode=0o700)
    assert (
        module.write_copy(
            str(home3 / "private/postgres-environment.json"), uid, "c", check_mode=True
        )[0]
        == "would_write"
    )
    assert not (home3 / "private/postgres-environment.json").exists()


def test_check_mode_and_raw_gate_and_pipelining_are_refused_first() -> None:
    names = [t["name"] for t in _materialize()]
    # The raw-gate, marker and check-mode refusals run before any probe or capture.
    assert names[:3] == [RAW_GATE, GUARD_MARKER, CHECK_MODE]
    raw = _task(RAW_GATE)
    assert raw["no_log"] is True
    assert raw["ansible.builtin.assert"]["that"] == [
        f"({PFX}enabled | default(false, true)) is sameas true"
    ]
    assert _task(CHECK_MODE)["ansible.builtin.assert"]["that"] == [
        "not ansible_check_mode"
    ]
    # The pipelining guard runs before the password is read.
    assert names.index(PIPELINING) < names.index(PASSWORD)
    assert _task(PIPELINING)["ansible.builtin.assert"]["that"] == PIPELINING_CHECKS
    # The playbook enables pipelining through the ssh plugin's own variable; the
    # repo ansible.cfg's [ssh_connection] setting does not populate it.
    (play,) = yaml.safe_load(PLAYBOOK.read_text())
    assert play["vars"] == {"ansible_pipelining": True}


def test_guarded_entry_point_spec_and_marker() -> None:
    # The supported entry point is infra/scripts/coding-hosted-guarded-run.py
    # with this spec; the marker is an accident guard inside the dynamic include,
    # reading the controller environment rather than an operator template.
    marker = _task(GUARD_MARKER)
    assert marker["ansible.builtin.assert"]["that"] == [
        f"lookup('ansible.builtin.env', '{MARKER_ENV}') == '{OPERATION}'"
    ]
    assert marker["ansible.builtin.assert"]["quiet"] is True
    assert "Nothing was written" in marker["ansible.builtin.assert"]["fail_msg"]
    assert "{{" not in marker["ansible.builtin.assert"]["fail_msg"]
    assert MARKER_ENV not in MAIN
    spec = json.loads(SPEC_PATH.read_text())
    host_pattern = spec["nonsecret_env_vars"][0]["pattern"]
    assert spec == {
        "schema": "ditto-coding-hosted-guarded-run/v1",
        "operation": OPERATION,
        "playbook": "playbooks/gcp-coding-hosted-postgres-environment.yml",
        "limit": "ditto-coding-hosted-v2",
        "enabled_var": f"{PFX}enabled",
        "confirmation_var": f"{PFX}confirmation",
        "confirmation": CONFIRMATION,
        "revision_var": f"{PFX}source_revision",
        "nonsecret_env_vars": [
            {
                "env": "DITTO_CODING_PG_HOST",
                "var": f"{PFX}host",
                "pattern": host_pattern,
                "max_length": 15,
            }
        ],
        "secret_env": [
            {
                "name": "DITTO_CODING_PG_PASSWORD",
                "charset": "single_line",
                "prefix": "",
                "min_length": 1,
                "max_length": 1024,
            }
        ],
        "distinct_secret_values": False,
        "forbidden_env": [],
        "forbidden_env_prefixes": [],
    }
    # The guard's host pattern accepts exactly what the role's own check does.
    (role_regex,) = [
        re.search(r"is match\('(.*)'\)", line).group(1)  # type: ignore[union-attr]
        for line in _task(HOST)["ansible.builtin.assert"]["that"]
        if line.startswith(f"{PFX}captured_host is match(")
    ]
    candidates = [f"10.30.0.{n}" for n in range(0, 300)]
    candidates += ["10.30.0.05", "10.30.1.5", "10.30.0.5\n", " 10.30.0.5", "10.30.0."]
    for candidate in candidates:
        role_accepts = bool(re.match(role_regex, candidate)) and candidate == (
            candidate.strip()
        )
        assert bool(re.fullmatch(host_pattern, candidate)) == role_accepts, candidate
    # The spec's secret bounds mirror the role's password assert.
    password_that = _task(PASSWORD)["ansible.builtin.assert"]["that"]
    assert f"{PASSWORD_LOOKUP} | length <= 1024" in password_that
    playbook = PLAYBOOK.read_text()
    assert "infra/scripts/coding-hosted-guarded-run.py" in playbook
    assert OPERATION in playbook
    assert "-e '" not in playbook


def test_identity_check_pins_the_reviewed_host_by_inventory_name() -> None:
    that = _task(HOST)["ansible.builtin.assert"]["that"]
    # ansible_play_hosts_all and ansible_play_batch carry real inventory names, so
    # the reviewed host must be the only target; a labelled rogue VM without
    # --limit cannot receive it. Both are pinned: with serial: 1 the batch alone
    # is the reviewed host while the rogue host is still in the play.
    assert GROUP_CHECK in that
    assert HOSTS_ALL_PIN in that and BATCH_PIN in that


def test_playbook_group_connection_and_ci_registration() -> None:
    (play,) = yaml.safe_load(PLAYBOOK.read_text())
    assert play["hosts"] == "role_coding_hosted"
    assert play["become"] is True and play["gather_facts"] is False
    assert play["roles"] == ["coding_hosted_postgres_environment"]
    assert set(play) == {"name", "hosts", "become", "gather_facts", "vars", "roles"}
    (fixture,) = yaml.safe_load(FIXTURE.read_text())
    assert fixture["hosts"] == "localhost" and fixture["connection"] == "local"
    assert fixture["become"] is False and fixture["gather_facts"] is False
    assert fixture["roles"] == ["coding_hosted_postgres_environment"]
    group = yaml.safe_load(
        (ROOT / "infra/ansible/group_vars/role_coding_hosted.yml").read_text()
    )
    assert set(group) == {
        "gcp_project",
        "gcp_region",
        "gcp_zone",
        "ansible_user",
        "ansible_ssh_common_args",
    }
    assert "gcloud compute start-iap-tunnel" in group["ansible_ssh_common_args"]
    workflow = (ROOT / ".github/workflows/infra-ci.yml").read_text()
    assert "playbooks/gcp-coding-hosted-postgres-environment.yml" in workflow
    assert "tests/coding-hosted-postgres-environment.yml" in workflow


def test_rehearsal_runs_only_in_the_infra_ansible_job() -> None:
    workflows = ROOT / ".github/workflows"
    infra = yaml.safe_load((workflows / "infra-ci.yml").read_text())
    this_file = str(Path(__file__).relative_to(ROOT))
    for trigger in ("pull_request", "push"):
        paths = infra[True][trigger]["paths"]
        assert this_file in paths
        # A lockfile or dependency-group change alters what the gate-free run
        # resolves, so both trigger the job.
        assert "pyproject.toml" in paths and "uv.lock" in paths
    (step,) = [
        step
        for job in infra["jobs"].values()
        for step in job["steps"]
        if REHEARSAL_GATE in step.get("env", {}) and this_file in step["run"]
    ]
    assert step in infra["jobs"]["ansible"]["steps"]
    assert step["env"] == {REHEARSAL_GATE: "1"}
    assert step["working-directory"] == "${{ github.workspace }}"
    # The locked dev group already brings PyYAML 6.0.3 (via pre-commit), so no
    # --with is needed.
    assert step["run"].split() == [
        "uv",
        "run",
        "--locked",
        "--only-group",
        "dev",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-rs",
        this_file,
    ]
    uses = [s.get("uses", "") for s in infra["jobs"]["ansible"]["steps"]]
    assert any(action.startswith("astral-sh/setup-uv@") for action in uses)
    for other in workflows.glob("*.yml"):
        if other.name != "infra-ci.yml":
            assert REHEARSAL_GATE not in other.read_text(), other.name


def test_docs_describe_every_forgery_guard() -> None:
    docs = (ROOT / "infra/docs/coding-hosted-postgres-v2.md").read_text()
    section = _flat(
        docs.split("## Native environment files", 1)[1].split("\n## ", 1)[0]
    )
    for phrase in (
        "dynamic",
        "start-at-task",
        "captured once",
        "template",
        "gathers no facts",
        "`ansible_facts`",
        "exactly 40 lowercase hex characters",
        "trailing newline",
        "`inactive` or `failed`",
        "`refreshing`",
        "`maintenance`",
        "re-checked",
        "mid-rotation",
        "`is sameas true`",
        "deprecation warning",
        "`exception`",
        "imported statically",
        "`varnames`",
        "`ansible_play_hosts_all`",
        "`ansible_inject_invocation`",
        "ansible_play_batch",
        "check mode",
        "O_NOFOLLOW",
        "renameat",
        "keep_remote_files",
        "`ansible_ssh_pipelining`",
        "`ansible_play_hosts_all == ['ditto-coding-hosted-v2']`",
        "`serial: 1`",
        "SHA-1, MD5 and SHA-256",
        f"`{REHEARSAL_GATE}=1`",
        "infra/scripts/coding-hosted-guarded-run.py",
        OPERATION,
        "accident guard only",
        "`#jinja2`",
        "Direct `ansible-playbook`",
        "closes this for the",
    ):
        assert phrase in section, phrase


# --------------------------------------------------------------------------- #
# Guard parity with the stacked removal role                                  #
# --------------------------------------------------------------------------- #


def test_guard_logic_follows_the_role_template() -> None:
    # The probe, the source-revision shape and the live-unit allow-list are the
    # unforgeable core; pin them so drift is caught.
    assert _task(IDENTITY)["ansible.builtin.setup"]["gather_subset"] == [
        "!all",
        "!min",
        "platform",
        "distribution",
    ]
    that = _task(HOST)["ansible.builtin.assert"]["that"]
    assert f"{PFX}captured_revision is match('^[0-9a-f]{{40}}$')" in that
    assert f"{PFX}captured_revision | length == 40" in that
    assert _live_check(_task(LIVE)) == _expected_live_check(f"{PFX}units")


@pytest.mark.skipif(
    not CLEANUP_ROLE.exists(),
    reason="the coding_hosted_postgres_environment_cleanup role is not on this tree",
)
def test_unit_listing_and_allow_list_match_the_cleanup_role() -> None:
    # The two roles have diverged (this one adds a dynamic-include gate, capture
    # once and template-error guards the cleanup branch still lacks), so only the
    # parts that must stay byte-identical are compared: the unit listing and the
    # allow-list regex both roles use to decide a copy is safe to touch.
    cleanup = yaml.safe_load((CLEANUP_ROLE / "tasks/main.yml").read_text())
    cleanup_prefix = "coding_hosted_postgres_environment_cleanup_"
    (cleanup_listing,) = [
        t
        for t in _walk(cleanup)
        if t.get("ansible.builtin.command", {}).get("argv", [""])[0]
        == "/usr/bin/systemctl"
    ]
    assert cleanup_listing["ansible.builtin.command"]["argv"] == LISTING_ARGV
    (cleanup_live,) = [
        t
        for t in _walk(cleanup)
        if "ansible.builtin.assert" in t
        and f"{cleanup_prefix}units.stdout_lines"
        in json.dumps(t["ansible.builtin.assert"]["that"])
    ]
    (cleanup_that,) = cleanup_live["ansible.builtin.assert"]["that"]
    # Normalise the prefix and compare the allow-list logic verbatim.
    assert _flat(cleanup_that).replace(cleanup_prefix, PFX) == _expected_live_check(
        f"{PFX}units"
    )


# --------------------------------------------------------------------------- #
# Local rehearsal (gated): run the real, restructured role against a temporary #
# tree through ansible-core 2.21.2, under the repo's yaml callback and -v      #
# --diff, and prove no forged input writes a copy or leaks the password.       #
# --------------------------------------------------------------------------- #

# A stand-in, never a real credential. It holds characters JSON, YAML and repr
# escape, plus a canary no escaping changes, so every printed form is found.
CANARY = "Kq7vCanaryZ3w9"
REHEARSAL_PASSWORD = f'rehearsal-only "stand-in"=pass\\word/42 {CANARY}'
REPO_ANSIBLE_CFG = ROOT / "infra/ansible/ansible.cfg"
STOPPED_UNITS = (
    "ditto-coding-hosted-worker.service loaded failed failed Worker\n"
    "ditto-coding-custody@0.service loaded inactive dead Custody\n"
    "ditto-coding-custody@1.service not-found inactive dead ditto-coding-custody@1\n"
)
REHEARSAL_ACCOUNTS = {
    "ditto-coding-hosted": [
        "x",
        "2001",
        "2001",
        "",
        "/var/lib/ditto-coding-hosted",
        "/usr/sbin/nologin",
    ],
    "ditto-coding-custody": [
        "x",
        "2002",
        "2002",
        "",
        "/var/lib/ditto-coding-custody",
        "/usr/sbin/nologin",
    ],
}
LIVE_UNITS = {
    "active": "ditto-coding-custody@0.service loaded active running Custody",
    "activating": "ditto-coding-hosted-worker.service loaded activating start Worker",
    "deactivating": (
        "ditto-coding-hosted-worker.service loaded deactivating stop-sigterm Worker"
    ),
    "reloading": "ditto-coding-custody@0.service loaded reloading reload Custody",
    "refreshing": (
        "ditto-coding-custody@2.service loaded refreshing refresh-extensions Custody"
    ),
    "maintenance": "ditto-coding-custody@3.service loaded maintenance cleaning Custody",
    "unknown_state": "ditto-coding-hosted-worker.service loaded quiescent idle Worker",
    "unparseable": "● ditto-coding-custody@0.service loaded inactive dead Custody",
}

rehearsal = pytest.mark.skipif(
    os.environ.get(REHEARSAL_GATE) != "1",
    reason=f"set {REHEARSAL_GATE}=1 to run the ansible-core rehearsal",
)


def _rewrite(node: Any, *, local_identity: bool, mock_accounts: bool) -> Any:
    """Point the role at a temporary tree: rewrite paths, owners, the unit
    listing and (optionally) the identity comparison and the account probe."""
    if isinstance(node, dict):
        out = {
            key: (
                value
                if key == "ansible.builtin.assert"
                else _rewrite(
                    value, local_identity=local_identity, mock_accounts=mock_accounts
                )
            )
            for key, value in node.items()
        }
        command = out.get("ansible.builtin.command")
        if command and command.get("argv", [None])[0] == "/usr/bin/systemctl":
            out["ansible.builtin.command"] = {
                "argv": ["/usr/bin/printf", "%s", "{{ rehearsal_units }}"]
            }
        if mock_accounts and "ansible.builtin.getent" in out:
            del out["ansible.builtin.getent"]
            out["ansible.builtin.set_fact"] = {
                "getent_passwd": "{{ rehearsal_accounts }}"
            }
        if "ansible.builtin.assert" in out:
            that = out["ansible.builtin.assert"]["that"]
            new = []
            for line in that:
                match = re.fullmatch(
                    rf"{PFX}identity\.ansible_facts\.(ansible_\w+) == '[^']+'", line
                )
                if match and local_identity:
                    new.append(
                        f"{PFX}identity.ansible_facts.{match[1]} == "
                        f"rehearsal_probe.ansible_facts.{match[1]}"
                    )
                else:
                    new.append(line)
            out["ansible.builtin.assert"] = dict(
                out["ansible.builtin.assert"], that=new
            )
        return out
    if isinstance(node, list):
        return [
            _rewrite(x, local_identity=local_identity, mock_accounts=mock_accounts)
            for x in node
        ]
    if isinstance(node, str):
        if node in OWNERS:
            return "{{ rehearsal_owner }}"
        return node.replace("/var/lib/", "{{ rehearsal_root }}/var/lib/")
    return node


def _write_module() -> Any:
    spec = importlib.util.spec_from_file_location(WRITE_MODULE, WRITE_MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def _build_role(
    dst: Path, *, local_identity: bool, mock_accounts: bool, break_digest: bool = False
) -> None:
    role = dst / "roles/coding_hosted_postgres_environment"
    (role / "tasks").mkdir(parents=True)
    shutil.copytree(
        ROLE / "library",
        role / "library",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (role / "tasks/main.yml").write_text(MAIN)
    materialize = _rewrite(
        copy.deepcopy(_materialize()),
        local_identity=local_identity,
        mock_accounts=mock_accounts,
    )
    if break_digest:
        # Force the post-write digest check to fail after a real write, so the
        # test can prove the checksum is not printed when a verify assert fails.
        _task(DIGEST, materialize)["ansible.builtin.assert"]["that"] = [
            "item.checksum == 'deadbeef'"
        ]
    rendered = json.dumps(materialize)
    assert "systemctl" not in rendered
    assert not any(f'"{owner}"' in rendered for owner in OWNERS)
    (role / "tasks/materialize.yml").write_text(
        yaml.safe_dump(materialize, sort_keys=False)
    )


def _record(content: str) -> dict:
    # The harness never prints what it records, so the output searched for leaks
    # holds only what the role itself printed.
    return {
        "ansible.builtin.copy": {
            "dest": "{{ rehearsal_root }}/outcome.json",
            "content": content,
        },
        "check_mode": False,
        "no_log": True,
        "diff": False,
    }


def _play(serial: int | None = None) -> dict:
    # The play vars are the real playbook's, so the rehearsal runs with exactly
    # the pipelining setting the playbook provides and nothing exported.
    (playbook,) = yaml.safe_load(PLAYBOOK.read_text())
    play: dict[str, Any] = {
        "name": "Rehearse materialization",
        "hosts": "all",
        "strategy": "free",
        "gather_facts": False,
        "become": False,
        "vars": {
            **playbook.get("vars", {}),
            "ansible_python_interpreter": "{{ ansible_playbook_python }}",
        },
        "tasks": [
            {
                "name": "Probe this machine for the local-identity comparison",
                "ansible.builtin.setup": {
                    "gather_subset": ["!all", "!min", "platform", "distribution"]
                },
                "register": "rehearsal_probe",
            },
            {
                "name": "Rehearse the role",
                "block": [
                    # A static import, like the playbook's roles: list, so
                    # --start-at-task sees exactly the tasks it sees in production.
                    {
                        "name": "Import the role",
                        "ansible.builtin.import_role": {
                            "name": "coding_hosted_postgres_environment"
                        },
                    },
                    {
                        "name": "Record completion",
                        **_record("{{ {'ok': true} | to_json }}"),
                    },
                ],
                "rescue": [
                    {
                        "name": "Record refusal",
                        **_record(
                            "{{ {'task': ansible_failed_task.name, 'msg': "
                            "ansible_failed_result.msg | default('')} | to_json }}"
                        ),
                    }
                ],
            },
        ],
    }
    if serial is not None:
        play["serial"] = serial
    return play


def _hostvars(root: Path, **overrides: object) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    base = {
        "ansible_connection": "local",
        f"{PFX}enabled": True,
        f"{PFX}confirmation": CONFIRMATION,
        f"{PFX}source_revision": REVISION,
        f"{PFX}host": DATABASE_HOST,
        "rehearsal_root": str(root),
        "rehearsal_units": STOPPED_UNITS,
        "rehearsal_owner": pwd.getpwuid(os.getuid()).pw_name,
        "rehearsal_group": grp.getgrgid(os.getgid()).gr_name,
        "rehearsal_accounts": REHEARSAL_ACCOUNTS,
    }
    base.update(overrides)
    return base


REVIEWED_HOST = "ditto-coding-hosted-v2"


def _run(
    tmp_path: Path,
    name: str,
    hosts: dict[str, dict],
    *flags: str,
    local_identity: bool = True,
    mock_accounts: bool = True,
    break_digest: bool = False,
    residual: str | None = None,
    outside: dict[str, dict] | None = None,
    keep_remote_files: str | None = None,
    host_name: str | None = None,
    seed_homes: bool = False,
    serial: int | None = None,
    marker: str | None = OPERATION,
) -> str:
    work = tmp_path / name
    work.mkdir()
    if seed_homes:
        for hostvars in hosts.values():
            _seed_homes(Path(hostvars["rehearsal_root"]))
    _build_role(
        work,
        local_identity=local_identity,
        mock_accounts=mock_accounts,
        break_digest=break_digest,
    )
    # A single in-group host is named for the reviewed host, so the play targets
    # exactly it and ansible_play_batch == [REVIEWED_HOST]. Multi-host cases keep
    # their names to prove the batch check refuses them.
    if host_name is not None:
        hosts = {host_name: next(iter(hosts.values()))}
    elif len(hosts) == 1 and not outside:
        hosts = {REVIEWED_HOST: next(iter(hosts.values()))}
    inventory: dict[str, Any] = {
        "all": {"children": {"role_coding_hosted": {"hosts": hosts}}}
    }
    if outside:
        # Hosts outside role_coding_hosted that the rehearsal play still targets.
        inventory["all"]["hosts"] = outside
    (work / "inventory.yml").write_text(yaml.safe_dump(inventory))
    (work / "play.yml").write_text(yaml.safe_dump([_play(serial)], sort_keys=False))
    # The repo's own ansible.cfg, verbatim: its default callback with yaml results,
    # and roles_path=roles resolved beside it, reach the temporary role.
    shutil.copy(REPO_ANSIBLE_CFG, work / "ansible.cfg")
    config = REPO_ANSIBLE_CFG.read_text()
    assert re.search(r"^stdout_callback\s*=\s*default$", config, re.M)
    assert re.search(r"^callback_result_format\s*=\s*yaml$", config, re.M)
    assert re.search(r"^roles_path\s*=\s*roles$", config, re.M)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("ANSIBLE_") and key != "DITTO_CODING_PG_PASSWORD"
    }
    environment |= {
        "ANSIBLE_CONFIG": str(work / "ansible.cfg"),
        "ANSIBLE_HOME": str(work / "ansible-home"),
        "ANSIBLE_LOCAL_TEMP": str(work / "ansible-tmp"),
        "ANSIBLE_LOG_PATH": str(work / "ansible.log"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_RETRY_FILES_ENABLED": "0",
        # No ANSIBLE_PIPELINING is exported: pipelining comes only from the
        # playbook's play vars and the repo ansible.cfg, as in the documented run.
        "DITTO_CODING_PG_PASSWORD": REHEARSAL_PASSWORD,
    }
    if marker is not None:
        environment[MARKER_ENV] = marker
    if keep_remote_files is not None:
        environment["ANSIBLE_KEEP_REMOTE_FILES"] = keep_remote_files
    completed = subprocess.run(
        [
            "uvx",
            "--from",
            "ansible-core==2.21.2",
            "ansible-playbook",
            "-f",
            "10",
            "-v",
            "--diff",
            "-i",
            "inventory.yml",
            *flags,
            "play.yml",
        ],
        cwd=work,
        env=environment,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    log = work / "ansible.log"
    output = "\n".join(
        [completed.stdout, completed.stderr, log.read_text() if log.exists() else ""]
    )
    assert completed.returncode == 0, output[-6000:]
    # Multi-form leak search over the console and the log: the raw stand-in, its
    # JSON-, YAML- and repr-escaped forms, the canary, and the SHA-1, MD5 and
    # SHA-256 of the password and of the rendered document (a digest would enable
    # offline guessing) must never appear, even under -v --diff.
    forms = _leak_forms()
    leaked = [line for line in output.splitlines() if any(f in line for f in forms)]
    if residual is None:
        assert leaked == [], (name, [line[:160] for line in leaked])
    else:
        # The documented residual: ansible-core prints a raised templating error
        # through the task result's preserved exception field, even under no_log.
        assert leaked, name
        assert all(residual in line for line in leaked), (name, leaked)
    return output


def _document(host: str = DATABASE_HOST, password: str = REHEARSAL_PASSWORD) -> str:
    # to_json is json.dumps with default separators and ASCII escapes, matching
    # what the role renders and writes.
    values = [
        f"POSTGRES_HOST={host}",
        "POSTGRES_PORT=5432",
        "POSTGRES_USER=ditto",
        "POSTGRES_DB=ditto_platform_prod",
        "POSTGRES_COMMAND_TIMEOUT=30",
        "POSTGRES_POOL_MIN_SIZE=1",
        "POSTGRES_POOL_MAX_SIZE=4",
        f"POSTGRES_PASSWORD={password}",
    ]
    return json.dumps(values)


def _leak_forms() -> set[str]:
    forms = {
        REHEARSAL_PASSWORD,
        CANARY,
        json.dumps(REHEARSAL_PASSWORD)[1:-1],
        repr(REHEARSAL_PASSWORD)[1:-1],
        yaml.safe_dump(REHEARSAL_PASSWORD).splitlines()[0].strip("'"),
        yaml.safe_dump(REHEARSAL_PASSWORD, default_style='"').strip()[1:-1],
    }
    for payload in (REHEARSAL_PASSWORD, _document()):
        for algorithm in ("sha1", "md5", "sha256"):
            forms.add(hashlib.new(algorithm, payload.encode()).hexdigest())
    return forms


def _seed_homes(root: Path) -> None:
    # The reader homes are created 0700 by the daemon and custody bootstraps in
    # production; the write module requires them to exist. Seed them so the
    # write-reaching cases mimic a bootstrapped host.
    for item in COPIES:
        home = root / Path(item["path"]).parents[1].relative_to("/")
        home.mkdir(parents=True, exist_ok=True)
        home.chmod(0o700)


def _outcome(root: Path) -> dict | None:
    path = root / "outcome.json"
    return json.loads(path.read_text()) if path.exists() else None


def _written(root: Path, path: str) -> list | None:
    copy_path = root / path.lstrip("/")
    return json.loads(copy_path.read_text()) if copy_path.exists() else None


def _refused_at(root: Path, task: str) -> None:
    outcome = _outcome(root)
    assert outcome is not None and outcome.get("task") == task, (root.name, outcome)
    assert _written(root, CUSTODY) is None and _written(root, HOSTED) is None
    assert not (root / "var").exists(), root.name


def _materialized(root: Path, host: str = DATABASE_HOST) -> None:
    expected = json.loads(_document(host=host))
    for item in COPIES:
        assert _written(root, item["path"]) == expected, root.name
        copy_path = root / item["path"].lstrip("/")
        assert copy_path.stat().st_mode & 0o777 == 0o600
        assert copy_path.stat().st_nlink == 1
        assert copy_path.parent.stat().st_mode & 0o777 == 0o700


@rehearsal
def test_rehearsal_materializes_and_refuses_every_forged_inventory_input(
    tmp_path,
) -> None:
    # Each case runs as its own single-host play named for the reviewed host, so
    # the batch identity check holds and the module writes to a real tree.
    def host(name, **overrides):
        return _hostvars(tmp_path / "hosts" / name, **overrides)

    materialize = {
        "materialized": host("materialized"),
        "no_units": host("no_units", rehearsal_units=""),
    }
    # A lazily templated host renders 203.0.113.9 only inside a loop; captured
    # once with no item it must resolve to the safe address and write that.
    materialize["lazy_host"] = host(
        "lazy_host",
        **{f"{PFX}host": '{{ "203.0.113.9" if item is defined else "10.30.0.5" }}'},
    )
    for name, hostvars in materialize.items():
        _run(tmp_path, f"mat_{name}", {name: hostvars}, seed_homes=True)
        _materialized(tmp_path / "hosts" / name)

    # A lazy gate, a gate templated to the password, and a string "true" all
    # leave the include closed and write nothing.
    dormant = {
        "lazy_enabled": "{{ item is defined }}",
        "enabled_password": '{{ lookup("env", "DITTO_CODING_PG_PASSWORD") }}',
        "leak_enabled": '{{ {}[lookup("env", "DITTO_CODING_PG_PASSWORD")] }}',
        "enabled_string_true": "true",
    }
    for name, value in dormant.items():
        root = tmp_path / "hosts" / name
        _run(tmp_path, f"dorm_{name}", {name: host(name, **{f"{PFX}enabled": value})})
        assert _written(root, CUSTODY) is None
        assert not (root / "var").exists(), name

    live_units: dict[str, tuple[dict, str]] = {name: ({}, LIVE) for name in LIVE_UNITS}
    refusals: dict[str, tuple[dict, str]] = {
        **live_units,
        "leak_host_error": (
            {f"{PFX}host": '{{ {}[lookup("env", "DITTO_CODING_PG_PASSWORD")] }}'},
            HOST,
        ),
        "leak_host_value": (
            {f"{PFX}host": '{{ lookup("env", "DITTO_CODING_PG_PASSWORD") }}'},
            HOST,
        ),
        "revision_newline": ({f"{PFX}source_revision": REVISION + "\n"}, HOST),
        "host_newline": ({f"{PFX}host": DATABASE_HOST + "\n"}, HOST),
        "preset_result": ({f"{PFX}units": {"stdout": "", "stdout_lines": []}}, PRESET),
        "undocumented_input": ({f"{PFX}user": "postgres"}, PRESET),
        "password_variable": (
            {f"{PFX}password": "rehearsal-variable"},
            PASSWORD_VARIABLE,
        ),
        "password_raising": (
            {
                f"{PFX}password": (
                    '{{ lookup("file", lookup("env", "DITTO_CODING_PG_PASSWORD")) }}'
                )
            },
            PASSWORD_VARIABLE,
        ),
        "preset_item": (
            {"item": {"path": str(tmp_path / "x"), "owner": "root"}},
            PRESET,
        ),
    }
    # A run without the guarded entry point's marker, or with another
    # operation's marker, is refused inside the include before any probe.
    for name, marker in {"no_marker": None, "wrong_marker": "other-op"}.items():
        _run(tmp_path, f"ref_{name}", {name: host(name)}, marker=marker)
        _refused_at(tmp_path / "hosts" / name, GUARD_MARKER)

    for name, (payload, task) in refusals.items():
        root = tmp_path / "hosts" / name
        if name in LIVE_UNITS:
            hostvars = host(
                name, rehearsal_units=STOPPED_UNITS + LIVE_UNITS[name] + "\n"
            )
        else:
            hostvars = host(name, **payload)
        _run(tmp_path, f"ref_{name}", {name: hostvars})
        _refused_at(root, task)


@rehearsal
def test_rehearsal_extra_vars_cannot_forge_the_gate_facts_or_accounts(
    tmp_path,
) -> None:
    # Forged gathered facts describing the dedicated host and valid accounts must
    # not satisfy the checks, which read only registered probes.
    assert socket.gethostname().split(".")[0] != PROBED_IDENTITY["hostname"]
    forged_facts = {
        **{f"ansible_{key}": value for key, value in PROBED_IDENTITY.items()},
        "getent_passwd": REHEARSAL_ACCOUNTS,
    }

    forged_host = tmp_path / "hosts/forged_host"
    _run(
        tmp_path,
        "forged_identity",
        {"forged_host": _hostvars(forged_host)},
        "-e",
        json.dumps({"ansible_facts": forged_facts}),
        local_identity=False,
    )
    _refused_at(forged_host, HOST)

    forged_accounts = tmp_path / "hosts/forged_accounts"
    _run(
        tmp_path,
        "forged_accounts",
        {"forged_accounts": _hostvars(forged_accounts)},
        "-e",
        json.dumps({"ansible_facts": forged_facts}),
        mock_accounts=False,
    )
    _refused_at(forged_accounts, ACCOUNT_CHECK)


@rehearsal
def test_rehearsal_extra_vars_cannot_forge_group_membership(tmp_path) -> None:
    # inventory_hostname and group_names are host variables extra vars override.
    # Forging both for a host outside role_coding_hosted must not pass the group
    # check, which reads only groups and ansible_play_hosts_all.
    roots = {name: tmp_path / "hosts" / name for name in ("inside", "outside")}
    _run(
        tmp_path,
        "forged_group",
        {"inside": _hostvars(roots["inside"])},
        "-e",
        json.dumps(
            {"inventory_hostname": "inside", "group_names": ["role_coding_hosted"]}
        ),
        outside={"outside": _hostvars(roots["outside"])},
    )
    for root in roots.values():
        _refused_at(root, HOST)


@rehearsal
def test_rehearsal_injected_invocations_never_carry_the_password(tmp_path) -> None:
    # -e ansible_inject_invocation=true returns every module's arguments in its
    # result and -vvv prints them; no_log on every password-bearing module keeps
    # the password, the document and its checksums out of both.
    root = tmp_path / "hosts/invocation"
    _run(
        tmp_path,
        "invocation",
        {"invocation": _hostvars(root)},
        "-vvv",
        "-e",
        json.dumps({"ansible_inject_invocation": True}),
        seed_homes=True,
    )
    _materialized(root)


@rehearsal
def test_rehearsal_extra_vars_leak_payload_is_caught_at_capture(tmp_path) -> None:
    # The reviewer's own -e repro: a host input that errors while reading the
    # password. It is caught at capture, fails validation, and never leaks.
    root = tmp_path / "hosts/leak"
    _run(
        tmp_path,
        "leak_extra_vars",
        {"leak": _hostvars(root)},
        "-e",
        json.dumps(
            {f"{PFX}host": '{{ {}[lookup("env", "DITTO_CODING_PG_PASSWORD")] }}'}
        ),
    )
    _refused_at(root, HOST)


@rehearsal
def test_rehearsal_start_at_task_cannot_skip_guards_to_reach_the_write(
    tmp_path,
) -> None:
    # The play imports the role statically, as the playbook's roles: list does,
    # so every task in tasks/main.yml is a valid start point. Each host also lists
    # a live unit, so a start that skipped the guards would be observable.
    starts = {
        "write": WRITE,
        "render": RENDER,
        "preset": PRESET,
        "include": INCLUDE,
    }
    for name, task in starts.items():
        root = tmp_path / "hosts" / name
        hostvars = _hostvars(root)
        hostvars["rehearsal_units"] = STOPPED_UNITS + LIVE_UNITS["active"] + "\n"
        _run(tmp_path, name, {name: hostvars}, "--start-at-task", task)
        assert _written(root, CUSTODY) is None and _written(root, HOSTED) is None
        assert not (root / "var").exists(), name
        if task == INCLUDE:
            # Starting at the include skips the capture: the undefined captured
            # gate fails the host instead of writing anything.
            outcome = _outcome(root)
            assert outcome is not None and outcome.get("task") == INCLUDE, outcome
        else:
            # Tasks inside the dynamically included materialize.yml are invisible
            # to --start-at-task, so nothing in the play ran at all.
            assert _outcome(root) is None, (name, _outcome(root))


@rehearsal
def test_rehearsal_a_verify_failure_never_prints_the_checksum(tmp_path) -> None:
    # Break the post-write digest check so it fails after a real write. _run
    # asserts the document digest (the on-disk checksum) and every password form
    # are absent from the -v --diff output and the log, which is what no_log on
    # the write result and the digest assert guarantees.
    root = tmp_path / "hosts/verify"
    _run(
        tmp_path,
        "verify",
        {"verify": _hostvars(root)},
        break_digest=True,
        seed_homes=True,
    )
    outcome = _outcome(root)
    assert outcome is not None and outcome.get("task") == DIGEST, outcome


@rehearsal
def test_rehearsal_module_never_writes_through_a_swapped_parent(tmp_path) -> None:
    # The reader owns its home and can swap private for a symlink to a root-owned
    # directory. The module refuses at the write, following no link, and the
    # victim directory stays empty.
    root = tmp_path / "hosts/swap"
    hostvars = _hostvars(root)
    # A victim the account "owns" at 0700 (as root would leave it after chowning
    # the link target), so only O_NOFOLLOW, not the owner/mode checks, refuses.
    victim = tmp_path / "victim-account-owned"
    victim.mkdir(mode=0o700)
    victim.chmod(0o700)
    custody_home = root / "var/lib/ditto-coding-custody"
    custody_home.mkdir(parents=True, mode=0o700)
    (custody_home / "private").symlink_to(victim)
    _run(tmp_path, "swap", {"swap": hostvars})
    outcome = _outcome(root)
    assert outcome is not None and outcome.get("task") == WRITE, outcome
    assert list(victim.iterdir()) == []
    assert _written(root, CUSTODY) is None


@rehearsal
def test_rehearsal_module_replaces_a_symlinked_copy_with_a_real_file(tmp_path) -> None:
    # The copy path itself is a symlink to an outside file. renameat replaces the
    # link entry with a real regular file; the outside file is never written.
    root = tmp_path / "hosts/relink"
    hostvars = _hostvars(root)
    _seed_homes(root)
    outside = tmp_path / "outside.json"
    outside.write_text("outside")
    for home in ("ditto-coding-custody", "ditto-coding-hosted"):
        private = root / "var/lib" / home / "private"
        private.mkdir(mode=0o700)
        (private / "postgres-environment.json").symlink_to(outside)
    _run(tmp_path, "relink", {"relink": hostvars})
    _materialized(root)
    assert outside.read_text() == "outside"


@rehearsal
def test_rehearsal_check_mode_pipelining_and_kept_files_are_refused(tmp_path) -> None:
    check = tmp_path / "hosts/check"
    _run(tmp_path, "check", {"check": _hostvars(check)}, "--check")
    _refused_at(check, CHECK_MODE)
    # The playbook's ansible_pipelining: true, with nothing exported, passes; any
    # override that disables pipelining, or a string where a boolean is expected,
    # is refused before the password is read. On 2.21.2 ansible_ssh_pipelining
    # wins over ansible_pipelining, so disabling it alone must refuse too.
    overrides = {
        "pipe_false": json.dumps({"ansible_pipelining": False}),
        "pipe_string_true": "ansible_pipelining=true",
        "pipe_string_false": "ansible_pipelining=false",
        "ssh_pipe_false": json.dumps({"ansible_ssh_pipelining": False}),
    }
    for name, extra in overrides.items():
        root = tmp_path / "hosts" / name
        _run(tmp_path, name, {name: _hostvars(root)}, "-e", extra)
        _refused_at(root, PIPELINING)
    keep = tmp_path / "hosts/keep_files"
    _run(tmp_path, "keep_files", {"keep_files": _hostvars(keep)}, keep_remote_files="1")
    _refused_at(keep, PIPELINING)


@rehearsal
def test_rehearsal_a_rogue_inventory_host_is_refused_without_limit(tmp_path) -> None:
    # A labelled rogue VM that reports the reviewed identity but is a different
    # inventory host is refused by the batch check, which reads real names.
    root = tmp_path / "hosts/rogue"
    _run(tmp_path, "rogue", {"rogue-vm": _hostvars(root)}, host_name="rogue-vm")
    _refused_at(root, HOST)
    # With serial: 1 each batch holds one host, so the batch pin alone passes on
    # the reviewed host while a rogue host is still in the play; the
    # ansible_play_hosts_all pin refuses both.
    roots = {name: tmp_path / "hosts" / f"serial_{name}" for name in ("named", "rogue")}
    _run(
        tmp_path,
        "serial",
        {
            REVIEWED_HOST: _hostvars(roots["named"]),
            "rogue-vm": _hostvars(roots["rogue"]),
        },
        serial=1,
    )
    for root in roots.values():
        _refused_at(root, HOST)


@rehearsal
def test_rehearsal_start_at_a_main_task_cannot_open_the_gate_with_enabled_false(
    tmp_path,
) -> None:
    # --start-at-task at a main.yml task with the captured gate and registers
    # preset must not write: starting at the capture recomputes it false, and
    # starting at the include is caught by the raw-enabled assert.
    presets = json.dumps(
        {
            CAPTURED_GATE: True,
            f"{PFX}units": {"stdout": "", "stdout_lines": []},
            f"{PFX}written": [],
        }
    )
    for task in (CAPTURE_GATE, INCLUDE, EXPLAIN):
        root = tmp_path / "hosts" / f"start_{task[:8]}"
        hostvars = _hostvars(root, **{f"{PFX}enabled": False})
        _run(
            tmp_path,
            f"start_{task[:8]}",
            {"h": hostvars},
            "--start-at-task",
            task,
            "-e",
            presets,
        )
        assert _written(root, CUSTODY) is None and _written(root, HOSTED) is None
        assert not (root / "var").exists(), task
        if task == INCLUDE:
            outcome = _outcome(root)
            assert outcome is not None and outcome.get("task") == RAW_GATE, outcome


@rehearsal
def test_rehearsal_raising_templates_fail_closed_and_leak_only_through_core(
    tmp_path,
) -> None:
    # The documented residual. A template that raises, rather than rendering
    # undefined, is not neutralised by default(..., true), and ansible-core 2.21.2
    # prints the raised message through the result's preserved exception field
    # even under no_log. The role still fails closed at the capture, writes
    # nothing, and never prints the value itself.
    raising = '{{ lookup("file", lookup("env", "DITTO_CODING_PG_PASSWORD")) }}'
    roots = {name: tmp_path / "hosts" / name for name in ("enabled", "host")}
    hosts = {
        "enabled": _hostvars(roots["enabled"], **{f"{PFX}enabled": raising}),
        "host": _hostvars(roots["host"], **{f"{PFX}host": raising}),
    }
    _run(
        tmp_path,
        "residual",
        hosts,
        residual="The lookup plugin 'file' failed: Unable to access the file",
    )
    for name, task in (("enabled", CAPTURE_GATE), ("host", CAPTURE)):
        outcome = _outcome(roots[name])
        assert outcome is not None and outcome.get("task") == task, (name, outcome)
        assert _written(roots[name], CUSTODY) is None, name
        assert not (roots[name] / "var").exists(), name
