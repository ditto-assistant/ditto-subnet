"""Native worker unit and custody template uninstall: default-off and surgical.

Structural tests parse the role and prove that its only removal targets are the
exact unit files coding_hosted_connectivity and coding_hosted_custody_service
install. Module tests drive the pinned-descriptor unlink module directly. The
rehearsal, gated by DITTO_ANSIBLE_REHEARSAL=1, runs the real role (main.yml
verbatim, remove.yml with only paths, owner, identity literals and systemctl
rewritten) through ansible-core 2.21.2 under the repo's ansible.cfg against
temporary trees, and mutates each guard to prove it is load-bearing.
"""

import copy
import functools
import importlib.util
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parents[2]
ANSIBLE = ROOT / "infra/ansible"
ROLE = ANSIBLE / "roles/coding_hosted_unit_uninstall"
MAIN = (ROLE / "tasks/main.yml").read_text()
REMOVE = (ROLE / "tasks/remove.yml").read_text()
MODULE_NAME = "coding_hosted_unit_uninstall_unlink"
MODULE_PATH = ROLE / f"library/{MODULE_NAME}.py"
PLAYBOOK = ANSIBLE / "playbooks/gcp-coding-hosted-unit-uninstall.yml"
FIXTURE = ANSIBLE / "tests/coding-hosted-unit-uninstall.yml"
DOC = ROOT / "infra/docs/coding-hosted-unit-uninstall-v2.md"
REPO_ANSIBLE_CFG = ANSIBLE / "ansible.cfg"

# The install roles and the exact unit files they template.
INSTALLERS = {
    "coding_hosted_connectivity": (
        "/etc/systemd/system/ditto-coding-hosted-worker.service"
    ),
    "coding_hosted_custody_service": (
        "/etc/systemd/system/ditto-coding-custody@.service"
    ),
}
UNIT_DIR = "/etc/systemd/system"
WORKER = INSTALLERS["coding_hosted_connectivity"]
CUSTODY = INSTALLERS["coding_hosted_custody_service"]
UNIT_PATHS = [WORKER, CUSTODY]

PREFIX = "coding_hosted_unit_uninstall_"
INPUTS = {
    f"{PREFIX}enabled": False,
    f"{PREFIX}confirmation": "",
    f"{PREFIX}source_revision": "",
}
GATE_NAME = f"{PREFIX}gate"
CONFIRMATION = "UNINSTALL NATIVE CODING WORKER AND CUSTODY UNITS"
REVISION = "0123456789abcdef0123456789abcdef01234567"
REHEARSAL_GATE = "DITTO_ANSIBLE_REHEARSAL"
REVIEWED_HOST = "ditto-coding-hosted-v2"

# main.yml
PRESET = "Refuse a preset gate, capture or registered result"
GATE_FREEZE = "Freeze the enabled gate once"
DORMANT = "Explain the dormant native unit uninstall"
INCLUDE = "Uninstall the worker unit and custody template behind the enabled gate"
# remove.yml
PRESET_INCLUDE = "Refuse a preset internal name or gate, or a non-boolean enabled flag"
BATCH = "Require the run to target exactly the one dedicated host"
CHECK_MODE = "Refuse check mode for an enabled uninstall"
FREEZE_INPUTS = "Freeze the confirmation and source revision once"
GATE = "Require the exact confirmation and source revision as frozen literals"
IDENTITY = "Probe this machine's identity into a result extra vars cannot preset"
HOST = "Require the dedicated host"
LISTING = "List live worker and custody units"
JOBS = "List queued jobs for the worker and custody units"
LIVE = (
    "Refuse to uninstall unless every listed unit is inactive or failed with no "
    "queued job"
)
UNLINK = "Remove the two unit files and reload through the symlink-safe module"
UNLINK_CHECK = (
    "Require the module to have removed or confirmed absent both unit files and "
    "reloaded"
)
RELIST = "Re-list live worker and custody units after removal"
JOBS_AFTER = "Re-list queued jobs for the worker and custody units after removal"
LIVE_AFTER = (
    "Refuse if any worker or custody unit went live or queued a job during removal"
)
LOAD_STATES = "Show the load state systemd reports for both units after the reload"
LOAD_STATES_CHECK = "Require systemd to report both units not-found after the reload"
REPORT = "Report only the source revision and which fixed unit paths were removed or already absent"  # noqa: E501

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
JOBS_ARGV = [
    "/usr/bin/systemctl",
    "list-jobs",
    "--no-legend",
    "--plain",
    "--full",
    "ditto-coding-hosted-worker.service",
    "ditto-coding-custody@*.service",
]
ZERO_INSTANCE = "ditto-coding-custody@00000000-0000-0000-0000-000000000000.service"
RELOAD_ARGV = ["/usr/bin/systemctl", "daemon-reload"]
# Manual cleanup an orphaned live unit needs once its file is gone.
MANUAL_CLEANUP = (
    "`sudo systemctl stop ditto-coding-hosted-worker.service`",
    "`sudo /usr/bin/python3 -I /usr/local/lib/ditto-coding-hosted/"
    "connectivity-policy.py revoke`",
    "`sudo systemctl stop ditto-coding-custody@<worker-uuid>.service`",
    "`sudo /usr/bin/python3 -I /usr/local/lib/ditto-coding-custody/custody-run.py "
    "release <worker-uuid>`",
)


def _docs(text: str) -> list[dict]:
    return yaml.safe_load(text)


def _walk(tasks: list[dict]) -> Iterator[dict]:
    for task in tasks:
        yield task
        for section in ("block", "rescue", "always"):
            yield from _walk(task.get(section, []))


def _task(name: str, tasks: list[dict] | None = None) -> dict:
    (task,) = [t for t in _walk(tasks or _docs(REMOVE)) if t.get("name") == name]
    return task


def _flat(text: object) -> str:
    return " ".join(str(text).split())


def _strings(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)
    elif isinstance(node, str):
        yield node


def _names(tasks: list[dict]) -> set[str]:
    """Every variable a task file creates by register, set_fact or loop_var."""
    names = set()
    for task in _walk(tasks):
        if "register" in task:
            names.add(task["register"])
        names.update(task.get("ansible.builtin.set_fact", {}))
        if "loop_control" in task:
            names.add(task["loop_control"].get("loop_var", "item"))
    return names


@pytest.fixture(autouse=True)
def _umask() -> Iterator[None]:
    # Trees must not be group-writable, which a 0002 login umask would make them.
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


# ─── Structure ────────────────────────────────────────────────────────────────


def test_defaults_are_exactly_the_three_inputs() -> None:
    assert yaml.safe_load((ROLE / "defaults/main.yml").read_text()) == INPUTS


def test_main_refuses_presets_then_freezes_the_gate_and_includes() -> None:
    tasks = _docs(MAIN)
    assert [t["name"] for t in tasks] == [PRESET, GATE_FREEZE, DORMANT, INCLUDE]
    (that,) = tasks[0]["ansible.builtin.assert"]["that"]
    assert _flat(that) == _flat(
        f"lookup('ansible.builtin.varnames', '^{PREFIX}', wantlist=True) | sort == ["
        + ", ".join(f"'{n}'" for n in sorted(INPUTS))
        + "]"
    )
    assert tasks[0]["ansible.builtin.assert"]["quiet"] is True
    freeze = tasks[1]
    assert freeze["no_log"] is True
    assert _flat(freeze["ansible.builtin.set_fact"][GATE_NAME]) == (
        f"{{{{ ({PREFIX}enabled | default(false, true)) is sameas true }}}}"
    )
    assert tasks[2]["when"] == f"not ({GATE_NAME} is sameas true)"
    assert tasks[3]["ansible.builtin.include_tasks"] == "remove.yml"
    assert tasks[3]["when"] == f"{GATE_NAME} is sameas true"
    assert "import_tasks" not in MAIN


def test_no_bool_filter_anywhere_in_the_role() -> None:
    for text in (MAIN, REMOVE):
        parsed = yaml.safe_dump(yaml.safe_load(text), width=10_000)
        assert not re.search(r"\|\s*bool\b", parsed)


def test_remove_task_order() -> None:
    assert [t["name"] for t in _docs(REMOVE)] == [
        PRESET_INCLUDE,
        BATCH,
        CHECK_MODE,
        FREEZE_INPUTS,
        GATE,
        IDENTITY,
        HOST,
        LISTING,
        JOBS,
        LIVE,
        UNLINK,
        RELIST,
        JOBS_AFTER,
        LIVE_AFTER,
        UNLINK_CHECK,
        LOAD_STATES,
        LOAD_STATES_CHECK,
        REPORT,
    ]


def test_in_include_guard_refuses_every_internal_name_and_the_raw_flag() -> None:
    that = _task(PRESET_INCLUDE)["ansible.builtin.assert"]["that"]
    assert that[0] == f"({PREFIX}enabled | default(false, true)) is sameas true"
    allowed = sorted([*INPUTS, GATE_NAME])
    assert _flat(that[1]) == _flat(
        f"lookup('ansible.builtin.varnames', '^{PREFIX}', wantlist=True) | sort == ["
        + ", ".join(f"'{n}'" for n in allowed)
        + "]"
    )
    # Every name the role creates carries the prefix, so both refusals cover it.
    created = _names(_docs(MAIN)) | _names(_docs(REMOVE))
    assert created and all(name.startswith(PREFIX) for name in created), created
    assert _names(_docs(MAIN)) == {GATE_NAME}
    # The rehearsal presets every one of them.
    assert created <= set(INTERNAL_PRESETS)


def test_targeting_is_the_whole_play_host_set_not_an_overridable_var() -> None:
    assert _task(BATCH)["ansible.builtin.assert"]["that"] == [
        f"ansible_play_batch == ['{REVIEWED_HOST}']",
        f"ansible_play_hosts_all == ['{REVIEWED_HOST}']",
    ]
    for text in (MAIN, REMOVE):
        assert "inventory_hostname" not in yaml.safe_dump(yaml.safe_load(text))
        assert "groups" not in yaml.safe_dump(yaml.safe_load(text))
    (play,) = yaml.safe_load(PLAYBOOK.read_text())
    assert play["hosts"] == "role_coding_hosted"
    assert play["gather_facts"] is False and play["become"] is True
    assert play["roles"] == ["coding_hosted_unit_uninstall"]
    assert set(play) == {"name", "hosts", "become", "gather_facts", "roles"}


def test_check_mode_is_refused_before_any_host_probe() -> None:
    names = [t["name"] for t in _docs(REMOVE)]
    assert _task(CHECK_MODE)["ansible.builtin.assert"]["that"] == [
        "not ansible_check_mode"
    ]
    assert names.index(CHECK_MODE) < names.index(IDENTITY)


def test_inputs_are_frozen_once_under_no_log_and_checked_as_literals() -> None:
    freeze = _task(FREEZE_INPUTS)
    assert freeze["no_log"] is True
    facts = freeze["ansible.builtin.set_fact"]
    assert _flat(facts[f"{PREFIX}gate_confirmation"]) == (
        f"{{{{ {PREFIX}confirmation | default('', true) }}}}"
    )
    assert _flat(facts[f"{PREFIX}gate_source_revision"]) == (
        f"{{{{ {PREFIX}source_revision | default('', true) }}}}"
    )
    that = _task(GATE)["ansible.builtin.assert"]["that"]
    assert f"{PREFIX}gate_confirmation == '{CONFIRMATION}'" in that
    assert f"{PREFIX}gate_source_revision is match('^[0-9a-f]{{40}}$')" in that
    # No task after the freeze reads the raw inputs again.
    after = _docs(REMOVE)[[t["name"] for t in _docs(REMOVE)].index(GATE) :]
    for task in after:
        dumped = json.dumps(task)
        for raw in ("confirmation", "source_revision"):
            assert f"{PREFIX}{raw}" not in dumped, task["name"]


def test_identity_comes_from_a_registered_probe() -> None:
    probe = _task(IDENTITY)
    assert probe["register"] == f"{PREFIX}identity"
    assert "ansible.builtin.setup" in probe
    assert _task(HOST)["ansible.builtin.assert"]["that"] == [
        f"{PREFIX}identity.ansible_facts.ansible_hostname == '{REVIEWED_HOST}'",
        f"{PREFIX}identity.ansible_facts.ansible_architecture == 'x86_64'",
        f"{PREFIX}identity.ansible_facts.ansible_distribution == 'Debian'",
        f"{PREFIX}identity.ansible_facts.ansible_distribution_major_version == '13'",
    ]
    assert "ansible_facts[" not in REMOVE


def test_live_units_and_jobs_are_refused_before_and_rechecked_after_removal() -> None:
    for listing, jobs, check in (
        (LISTING, JOBS, LIVE),
        (RELIST, JOBS_AFTER, LIVE_AFTER),
    ):
        units = _task(listing)
        queued = _task(jobs)
        assert units["ansible.builtin.command"]["argv"] == LISTING_ARGV
        assert queued["ansible.builtin.command"]["argv"] == JOBS_ARGV
        for task in (units, queued):
            assert task["check_mode"] is False and task["changed_when"] is False
            assert "failed_when" not in task
        live, idle_jobs = _task(check)["ansible.builtin.assert"]["that"]
        assert _flat(live) == _flat(
            f"{units['register']}.stdout_lines | reject('match', '{UNIT_PATTERN}') "
            "| list | length == 0"
        )
        assert idle_jobs == f"{queued['register']}.stdout | trim | length == 0"
    names = [t["name"] for t in _docs(REMOVE)]
    assert names.index(LIVE) < names.index(UNLINK) < names.index(RELIST)
    # An orphaned live unit is reported before the module receipt is enforced.
    assert names.index(LIVE_AFTER) < names.index(UNLINK_CHECK)
    message = _flat(_task(LIVE_AFTER)["ansible.builtin.assert"]["fail_msg"])
    for command in MANUAL_CLEANUP:
        assert command in message, command


def test_absence_is_confirmed_positively_after_the_reload() -> None:
    show = _task(LOAD_STATES)
    assert show["ansible.builtin.command"]["argv"] == [
        "/usr/bin/systemctl",
        "show",
        "--property=LoadState",
        "--value",
        Path(WORKER).name,
        ZERO_INSTANCE,
    ]
    assert "failed_when" not in show and show["check_mode"] is False
    assert _task(LOAD_STATES_CHECK)["ansible.builtin.assert"]["that"] == [
        f"{show['register']}.rc == 0",
        f"{show['register']}.stdout_lines | reject('equalto', '') | list"
        " == ['not-found', 'not-found']",
    ]
    # The zero instance can never be a real run: custody-run.py refuses it.
    helper = (
        ANSIBLE / "roles/coding_hosted_custody_service/files/custody-run.py"
    ).read_text()
    assert 'ZERO = "00000000-0000-0000-0000-000000000000"' in helper
    assert "list-unit-files" not in REMOVE


def test_role_stops_and_starts_nothing_and_touches_only_unit_files() -> None:
    systemctl = [
        task["ansible.builtin.command"]["argv"][1]
        for task in _walk(_docs(REMOVE))
        if "ansible.builtin.command" in task
    ]
    assert systemctl == ["list-units", "list-jobs", "list-units", "list-jobs", "show"]
    assert all(
        task["ansible.builtin.command"]["argv"][0] == "/usr/bin/systemctl"
        for task in _walk(_docs(REMOVE))
        if "ansible.builtin.command" in task
    )
    modules = {
        key
        for task in _walk(_docs(REMOVE))
        for key in task
        if "." in key or key == MODULE_NAME
    }
    assert modules == {
        "ansible.builtin.assert",
        "ansible.builtin.set_fact",
        "ansible.builtin.setup",
        "ansible.builtin.command",
        "ansible.builtin.debug",
        MODULE_NAME,
    }
    # Every operative string outside the constant messages and the report.
    values = [
        value
        for task in _walk(_docs(REMOVE))
        for key, body in task.items()
        if key != "name"
        for value in _strings(
            {k: v for k, v in body.items() if k not in ("fail_msg", "msg")}
            if isinstance(body, dict)
            else body
        )
    ]
    for forbidden in (
        "/var/lib",
        "/etc/ditto",
        "/opt/",
        "/usr/local",
        "/run/",
        "private",
        "postgres",
        "credential",
        "getent",
        "docker",
        "slurp",
        "shell",
        ".service.d",
    ):
        assert not any(forbidden in value.lower() for value in values), forbidden
    unlink = _task(UNLINK)
    assert unlink[MODULE_NAME] == {
        "unit_dir": UNIT_DIR,
        "owner": "root",
        "daemon_reload": RELOAD_ARGV,
    }
    assert unlink["failed_when"] is False
    assert _flat(unlink["changed_when"]) == (
        f"{PREFIX}removal.removed | default([]) | length > 0"
    )


def test_constant_fail_messages_except_the_module_receipt() -> None:
    receipt = rf" {PREFIX}removal\.\w+ \| default\((\[\]|false)\) \| to_json "
    for task in _walk(_docs(MAIN) + _docs(REMOVE)):
        assertion = task.get("ansible.builtin.assert")
        if not assertion:
            continue
        message = assertion["fail_msg"]
        if task["name"] in (UNLINK_CHECK, LIVE_AFTER):
            # Only the module's own fixed-path lists and flags, each defaulted.
            refs = re.findall(r"\{\{(.*?)\}\}", message)
            assert refs and all(re.fullmatch(receipt, ref) for ref in refs), refs
        else:
            assert "{{" not in message, task["name"]
        assert assertion["quiet"] is True
    that = _task(UNLINK_CHECK)["ansible.builtin.assert"]["that"]
    assert f"{PREFIX}removal.daemon_reloaded | default(false) is sameas true" in that


def test_report_interpolates_only_the_frozen_revision_and_module_state() -> None:
    message = _task(REPORT)["ansible.builtin.debug"]["msg"]
    assert re.findall(r"\{\{(.*?)\}\}", message) == [
        f" {PREFIX}gate_source_revision ",
        f" {PREFIX}removal.removed | to_json ",
        f" {PREFIX}removal.already_absent | to_json ",
        f" {PREFIX}removal.daemon_reloaded | to_json ",
    ]


def test_reload_runs_inside_the_unlink_module_not_a_separate_task() -> None:
    assert "daemon-reload" not in json.dumps(
        [t for t in _walk(_docs(REMOVE)) if MODULE_NAME not in t]
    )
    src = MODULE_PATH.read_text()
    assert (
        '"daemon_reload": {"type": "list", "elements": "str", "required": True}' in src
    )
    assert "module.run_command(argv, check_rc=False)" in src


# ─── Parity with the install roles ────────────────────────────────────────────

UNIT_REFERENCE = re.compile(r"ditto-coding-hosted-worker|ditto-coding-custody@")
SYSTEMD_PATH = re.compile(r"/etc/systemd/(?:\{\{.*?\}\}|[^\s\"'])*")
ENABLING_WORDS = re.compile(
    r"\b(enable|reenable|link|ln|mask|preset|preset-all|add-wants|add-requires|"
    r"set-property|edit|revert)\b"
)


def _module_key(task: dict) -> str | None:
    control = {
        "name", "when", "loop", "loop_control", "register", "changed_when",
        "failed_when", "check_mode", "no_log", "become", "become_user", "vars",
        "tags", "notify", "delegate_to", "run_once", "environment", "args",
        "ignore_errors", "with_items", "with_dict", "until", "retries", "delay",
        "diff", "listen", "block", "rescue", "always",
    }  # fmt: skip
    keys = [key for key in task if key not in control]
    return keys[0].rsplit(".", 1)[-1] if len(keys) == 1 else None


def _variables() -> dict[str, str]:
    """Scalar defaults, vars, group_vars and host_vars across the tree."""
    found: dict[str, str] = {}
    sources = [
        *ANSIBLE.glob("roles/*/defaults/*.yml"),
        *ANSIBLE.glob("roles/*/vars/*.yml"),
        *ANSIBLE.glob("group_vars/*.yml"),
        *ANSIBLE.glob("host_vars/*.yml"),
    ]
    for source in sources:
        data = yaml.safe_load(source.read_text()) or {}
        if isinstance(data, dict):
            found.update(
                {k: str(v) for k, v in data.items() if isinstance(v, str | int)}
            )
    return found


def _expand(value: str, task: dict, variables: dict[str, str]) -> list[str]:
    """Resolve simple {{ var }} and literal-loop {{ item }} references."""
    items = task.get("loop", task.get("with_items"))
    candidates = [value]
    if isinstance(items, list) and "item" in value:
        candidates = []
        for item in items:
            text = value
            if isinstance(item, dict):
                for key, sub in item.items():
                    text = re.sub(rf"\{{\{{\s*item\.{key}\s*\}}\}}", str(sub), text)
            else:
                text = re.sub(r"\{\{\s*item\s*\}\}", str(item), text)
            candidates.append(text)
    resolved = []
    for text in candidates:
        for _ in range(4):
            text = re.sub(
                r"\{\{\s*([A-Za-z_]\w*)\s*\}\}",
                lambda m: variables.get(m[1], m[0]),
                text,
            )
        resolved.append(text)
    return resolved


def _task_files() -> Iterator[tuple[str, list[dict]]]:
    for path in sorted(ANSIBLE.glob("roles/*/tasks/*.yml")) + sorted(
        ANSIBLE.glob("roles/*/handlers/*.yml")
    ):
        yield str(path.relative_to(ANSIBLE)), _docs(path.read_text()) or []
    for path in sorted(ANSIBLE.glob("playbooks/*.yml")):
        for play in _docs(path.read_text()) or []:
            for section in ("pre_tasks", "tasks", "post_tasks", "handlers"):
                yield f"{path.relative_to(ANSIBLE)}:{section}", play.get(section, [])


def _operative(task: dict) -> dict:
    """Task arguments that act on the host (not names, messages or asserts)."""
    key = _module_key(task)
    if key in (None, "assert", "debug", "fail", "set_fact", "include_tasks",
               "import_tasks", "include_role", "import_role"):  # fmt: skip
        return {}
    return {k: v for k, v in task.items() if k not in ("name",)}


def test_removal_targets_equal_the_install_role_unit_destinations() -> None:
    module = _unlink_module()
    assert [f"{UNIT_DIR}/{name}" for name in module.UNIT_NAMES] == UNIT_PATHS
    assert tuple(UNIT_DIR.strip("/").split("/")) == module.UNIT_DIR_SUFFIX
    for role, path in INSTALLERS.items():
        tasks = [
            task
            for _, doc in _task_files()
            if _.startswith(f"roles/{role}/")
            for task in _walk(doc)
            if "block" not in task
        ]
        # Structural: the only task writing under /etc/systemd is the template.
        writers = [
            task
            for task in tasks
            if any("/etc/systemd" in value for value in _strings(_operative(task)))
        ]
        (install,) = writers
        template = install["ansible.builtin.template"]
        assert template["dest"] == path
        assert template["owner"] == "root" and template["mode"] == "0644"
        assert _task(UNLINK)[MODULE_NAME]["owner"] == template["owner"]
        body = (ANSIBLE / f"roles/{role}/templates/{template['src']}").read_text()
        # No [Install] section: nothing can create an enablement link from it.
        assert not re.search(r"^\s*\[Install\]", body, re.M), role
        for task in tasks:
            key = _module_key(task)
            args = task.get(next((k for k in task if k.endswith(str(key))), ""), {})
            if key == "file":
                assert args.get("state") not in ("link", "hard"), task["name"]
            if key in ("systemd", "systemd_service", "service"):
                assert set(args) <= {"daemon_reload"}, task["name"]
            if key in ("command", "shell", "raw", "script"):
                words = " ".join(
                    _strings({k: v for k, v in task.items() if k != "name"})
                )
                assert not ENABLING_WORDS.search(words), task["name"]
            references = [
                value
                for value in _strings(_operative(task))
                if UNIT_REFERENCE.search(value)
            ]
            if not references:
                continue
            if task is install:
                continue
            # Otherwise only read-only systemctl queries may name either unit.
            argv = task.get("ansible.builtin.command", {}).get("argv", [])
            assert argv[:2] in (
                ["systemctl", "is-active"],
                ["/usr/bin/systemctl", "list-units"],
            ), task["name"]


def test_no_other_role_or_playbook_touches_either_unit() -> None:
    variables = _variables()
    found: dict[str, set[str]] = {}
    unresolved: set[tuple[str, str]] = set()
    for source, doc in _task_files():
        if source.startswith(f"roles/{ROLE.name}/") or "unit-uninstall" in source:
            continue
        for task in _walk(doc):
            if "block" in task:
                continue
            for value in _strings(_operative(task)):
                for text in _expand(value, task, variables):
                    if UNIT_REFERENCE.search(text):
                        found.setdefault(source.split("/tasks/")[0], set()).add(
                            _task_label(task)
                        )
                    # A variable-built unit path under /etc/systemd must resolve,
                    # or its literal name prefix must rule out both units.
                    for match in SYSTEMD_PATH.finditer(text):
                        parts = re.split(r"/(?![^{]*\}\})", match[0])
                        if any("{{" in p and _could_name_a_unit(p) for p in parts):
                            unresolved.add((source, match[0]))
    assert found == {
        "roles/coding_hosted_connectivity": {
            "Refuse an active worker without interrupting it",
            "Install the manual one-attempt worker unit without starting it",
        },
        "roles/coding_hosted_custody_service": {
            "List custody instances that are still live",
            "Install the locked custody unit template without enabling or starting it",
        },
    }
    # The only paths a literal scan cannot resolve are screener_partition's
    # drop-ins over a loop expression. Its loop lists literal screener units
    # plus screener_partition_worker_units, and nothing that defines either
    # names a native Coding unit.
    assert unresolved == {
        ("roles/screener_partition/tasks/main.yml", "/etc/systemd/system/{{ item }}.d"),
        (
            "roles/screener_partition/tasks/main.yml",
            "/etc/systemd/system/{{ item }}.d/drain-safety.conf",
        ),
        (
            "roles/screener_partition/tasks/main.yml",
            "/etc/systemd/system/{{ item }}.d/partition.conf",
        ),
    }, sorted(unresolved)
    partition = [
        path
        for path in ANSIBLE.rglob("*")
        if path.is_file()
        and (
            "screener_partition" in path.parts
            or "screener_partition_worker_units" in path.read_text(errors="replace")
        )
    ]
    assert partition
    for path in partition:
        assert not UNIT_REFERENCE.search(path.read_text(errors="replace")), path


def _task_label(task: dict) -> str:
    return task.get("name", json.dumps(task, sort_keys=True)[:80])


def _could_name_a_unit(tail: str) -> bool:
    literal = tail.split("{{", 1)[0]
    return any(
        unit.startswith(literal) or literal.startswith(unit)
        for unit in ("ditto-coding-hosted-worker.service", "ditto-coding-custody@")
    )


def test_parity_scan_detects_variable_built_and_enabling_forms() -> None:
    # Positive controls for the scanners above.
    variables = {"unit": "ditto-coding-custody@"}
    task = {"ansible.builtin.file": {"path": "/etc/systemd/system/{{ unit }}x"}}
    assert any(
        UNIT_REFERENCE.search(text)
        for value in _strings(_operative(task))
        for text in _expand(value, task, variables)
    )
    looped = {
        "ansible.builtin.copy": {"dest": "/etc/systemd/system/{{ item.dest }}"},
        "loop": [{"dest": "ditto-coding-hosted-worker.service.d/x.conf"}],
    }
    assert any(
        UNIT_REFERENCE.search(text)
        for value in _strings(_operative(looped))
        for text in _expand(value, looped, {})
    )
    assert _could_name_a_unit("{{ item }}.d") and not _could_name_a_unit(
        "user@{{ uid }}"
    )
    assert ENABLING_WORDS.search("systemctl enable x") and ENABLING_WORDS.search(
        " ".join(["/usr/bin/systemctl", "link", "/x"])
    )
    assert _module_key({"name": "n", "ansible.builtin.file": {}, "when": "x"}) == "file"


# ─── Module ──────────────────────────────────────────────────────────────────


def _unlink_module() -> Any:
    spec = importlib.util.spec_from_file_location(MODULE_NAME, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def _unit_tree(root: Path, *, worker: bool = True, custody: bool = True) -> Path:
    unit_dir = root / "etc/systemd/system"
    unit_dir.mkdir(parents=True)
    for directory in (root / "etc", root / "etc/systemd", unit_dir):
        directory.chmod(0o755)
    for present, path in ((worker, WORKER), (custody, CUSTODY)):
        if present:
            target = unit_dir / Path(path).name
            target.write_text("[Unit]\n")
            target.chmod(0o644)
    (unit_dir / "unrelated.service").write_text("[Unit]\n")
    return unit_dir


def _uid() -> int:
    return os.getuid()


def _paths(unit_dir: Path) -> list[str]:
    return [f"{unit_dir}/{Path(p).name}" for p in UNIT_PATHS]


class _Reload:
    """Records the unit directory listing at the moment daemon-reload runs."""

    def __init__(self, unit_dir: Path, *, fail: bool = False) -> None:
        self.unit_dir, self.fail = unit_dir, fail
        self.listings: list[list[str] | None] = []

    def __call__(self) -> None:
        self.listings.append(
            sorted(p.name for p in self.unit_dir.iterdir())
            if self.unit_dir.is_dir()
            else None
        )
        if self.fail:
            raise OSError(5, "reload")


def test_module_removes_only_the_two_unit_files(tmp_path) -> None:
    module = _unlink_module()
    unit_dir = _unit_tree(tmp_path.resolve())
    result = module.remove_units(str(unit_dir), _uid())
    assert result["removed"] == [f"{unit_dir}/{Path(p).name}" for p in UNIT_PATHS]
    assert result["already_absent"] == [] and result["refused"] == []
    assert result["changed"] is True
    assert sorted(p.name for p in unit_dir.iterdir()) == ["unrelated.service"]
    again = module.remove_units(str(unit_dir), _uid())
    assert again["removed"] == [] and again["changed"] is False
    assert again["already_absent"] == [f"{unit_dir}/{Path(p).name}" for p in UNIT_PATHS]


def test_module_reports_a_missing_unit_dir_as_already_absent(tmp_path) -> None:
    module = _unlink_module()
    unit_dir = tmp_path.resolve() / "etc/systemd/system"
    result = module.remove_units(str(unit_dir), _uid())
    assert result["unit_dir_present"] is False
    assert result["already_absent"] == [
        f"{unit_dir}/{Path(p).name}" for p in UNIT_PATHS
    ]


def test_module_check_mode_removes_nothing(tmp_path) -> None:
    module = _unlink_module()
    unit_dir = _unit_tree(tmp_path.resolve())
    result = module.remove_units(str(unit_dir), _uid(), check_mode=True)
    assert result["would_remove"] == [f"{unit_dir}/{Path(p).name}" for p in UNIT_PATHS]
    assert result["removed"] == []
    assert (unit_dir / Path(WORKER).name).exists()


@pytest.mark.parametrize("swap", ["symlink", "directory", "hardlink", "fifo"])
def test_module_refuses_a_swapped_worker_unit_and_attempts_nothing_after(
    tmp_path, swap
) -> None:
    module = _unlink_module()
    root = tmp_path.resolve()
    unit_dir = _unit_tree(root, worker=False)
    worker = unit_dir / Path(WORKER).name
    outside = root / "outside.service"
    outside.write_text("keep")
    if swap == "symlink":
        worker.symlink_to(outside)
    elif swap == "directory":
        worker.mkdir()
    elif swap == "hardlink":
        os.link(outside, worker)
    else:
        os.mkfifo(worker)
    result = module.remove_units(str(unit_dir), _uid())
    assert result["removed"] == []
    assert [r.split(": ")[0] for r in result["refused"]] == [str(worker)]
    assert result["not_attempted"] == [f"{unit_dir}/{Path(CUSTODY).name}"]
    assert outside.read_text() == "keep"
    assert (unit_dir / Path(CUSTODY).name).exists()
    assert os.path.lexists(worker)


def test_module_reports_a_partial_removal(tmp_path) -> None:
    module = _unlink_module()
    root = tmp_path.resolve()
    unit_dir = _unit_tree(root, custody=False)
    other = root / "other"
    other.write_text("x")
    os.link(other, unit_dir / Path(CUSTODY).name)
    result = module.remove_units(str(unit_dir), _uid())
    assert result["removed"] == [f"{unit_dir}/{Path(WORKER).name}"]
    assert result["refused"] == [
        f"{unit_dir}/{Path(CUSTODY).name}: has more than one hard link"
    ]
    assert result["not_attempted"] == []
    assert result["changed"] is True


def test_module_refuses_a_tree_not_owned_by_the_unit_owner(tmp_path) -> None:
    module = _unlink_module()
    unit_dir = _unit_tree(tmp_path.resolve())
    # Without root the files cannot be chowned; an owner the tree does not have
    # is refused at the first foreign directory, before any file.
    result = module.remove_units(str(unit_dir), _uid() + 1)
    (refusal,) = result["refused"]
    assert "owned by" in refusal
    assert result["removed"] == [] and result["not_attempted"] == _paths(unit_dir)
    assert (unit_dir / Path(WORKER).name).exists()


@pytest.mark.parametrize(
    "entry",
    [
        "ditto-coding-hosted-worker.service.d/override.conf",
        "ditto-coding-custody@.service.d/override.conf",
        "ditto-coding-custody@7c9e6679-7425-40de-944b-e07fc1f90ae7.service",
        "multi-user.target.wants/ditto-coding-hosted-worker.service",
        "default.target.requires/ditto-coding-custody@x.service",
    ],
)
def test_module_refuses_drop_ins_instances_and_links_before_removing(
    tmp_path, entry
) -> None:
    module = _unlink_module()
    unit_dir = _unit_tree(tmp_path.resolve())
    path = unit_dir / entry
    path.parent.mkdir(exist_ok=True)
    if ".wants" in entry or ".requires" in entry:
        path.symlink_to(unit_dir / Path(WORKER).name)
    else:
        path.write_text("[Service]\n")
    result = module.remove_units(str(unit_dir), _uid())
    assert result["foreign"] == [
        entry.split("/")[0] if ".service.d" in entry else entry
    ]
    assert result["removed"] == []
    assert result["not_attempted"] == [f"{unit_dir}/{Path(p).name}" for p in UNIT_PATHS]
    assert all((unit_dir / Path(p).name).exists() for p in UNIT_PATHS)


def test_module_refuses_a_symlinked_dependency_directory(tmp_path) -> None:
    module = _unlink_module()
    root = tmp_path.resolve()
    unit_dir = _unit_tree(root)
    hidden = root / "hidden"
    hidden.mkdir()
    (hidden / "ditto-coding-hosted-worker.service").symlink_to(
        unit_dir / Path(WORKER).name
    )
    (unit_dir / "multi-user.target.wants").symlink_to(hidden)
    result = module.remove_units(str(unit_dir), _uid())
    assert result["refused"] == [
        "dependency directory multi-user.target.wants is a symlink"
    ]
    assert result["removed"] == [] and result["not_attempted"] == _paths(unit_dir)
    assert all((unit_dir / Path(p).name).exists() for p in UNIT_PATHS)


def test_module_refuses_a_symlinked_or_writable_unit_directory(tmp_path) -> None:
    module = _unlink_module()
    root = tmp_path.resolve()
    real = _unit_tree(root / "real")
    linked = root / "linked/etc/systemd"
    linked.mkdir(parents=True)
    (linked / "system").symlink_to(real)
    result = module.remove_units(str(linked / "system"), _uid())
    assert result["refused"] == [
        "a unit directory component is a symlink or not a directory"
    ]
    assert result["removed"] == []
    assert (real / Path(WORKER).name).exists()

    writable = _unit_tree(root / "writable")
    writable.chmod(0o775)
    result = module.remove_units(str(writable), _uid())
    assert "writable by group or others" in result["refused"][0]
    writable.chmod(0o755)
    writable.parent.chmod(0o777)
    result = module.remove_units(str(writable), _uid())
    assert "writable by group or others" in result["refused"][0]
    assert result["removed"] == []
    writable.parent.chmod(0o755)
    assert (writable / Path(WORKER).name).exists()


@pytest.mark.parametrize(
    "path",
    [
        "etc/systemd/system",
        "/etc/systemd/system/",
        "/etc/systemd//system",
        "/etc/systemd/../systemd/system",
        "/etc/systemd/user",
        "/etc/systemd/system.control",
        "/run/systemd/transient",
    ],
)
def test_module_refuses_any_unit_dir_but_an_etc_systemd_system(path) -> None:
    module = _unlink_module()
    with pytest.raises(module.Unsafe):
        module.split_unit_dir(path)


def test_module_reloads_in_the_same_call_after_the_unlinks(tmp_path) -> None:
    module = _unlink_module()
    unit_dir = _unit_tree(tmp_path.resolve())
    reload = _Reload(unit_dir)
    result = module.remove_units(str(unit_dir), _uid(), reload=reload)
    # Exactly one reload, and it saw both files already gone.
    assert reload.listings == [["unrelated.service"]]
    assert result["daemon_reloaded"] is True and result["removed"] == _paths(unit_dir)


def test_module_reloads_after_a_refusal_but_never_in_check_mode(tmp_path) -> None:
    module = _unlink_module()
    unit_dir = _unit_tree(tmp_path.resolve())
    (unit_dir / "ditto-coding-custody@.service.d").mkdir()
    reload = _Reload(unit_dir)
    result = module.remove_units(str(unit_dir), _uid(), reload=reload)
    assert result["refused"] and result["daemon_reloaded"] is True
    assert len(reload.listings) == 1
    dry = _Reload(unit_dir)
    result = module.remove_units(str(unit_dir), _uid(), check_mode=True, reload=dry)
    assert dry.listings == [] and result["daemon_reloaded"] is False


def test_module_reports_a_failed_reload(tmp_path) -> None:
    module = _unlink_module()
    unit_dir = _unit_tree(tmp_path.resolve())
    result = module.remove_units(
        str(unit_dir), _uid(), reload=_Reload(unit_dir, fail=True)
    )
    assert result["removed"] == _paths(unit_dir)
    assert result["refused"] == ["daemon-reload failed"]
    assert result["daemon_reloaded"] is False and result["changed"] is True


def test_module_reports_the_true_state_when_the_second_unlink_fails(
    tmp_path, monkeypatch
) -> None:
    module = _unlink_module()
    unit_dir = _unit_tree(tmp_path.resolve())
    real_unlink = os.unlink
    calls = []

    def unlink(name, *, dir_fd=None):
        calls.append(name)
        if len(calls) == 2:
            raise PermissionError(1, "Operation not permitted")
        real_unlink(name, dir_fd=dir_fd)

    monkeypatch.setattr(module.os, "unlink", unlink)
    reload = _Reload(unit_dir)
    result = module.remove_units(str(unit_dir), _uid(), reload=reload)
    worker, custody = _paths(unit_dir)
    assert result["removed"] == [worker]
    assert result["refused"] == [f"{custody}: unlink failed: Operation not permitted"]
    assert result["not_attempted"] == [] and result["already_absent"] == []
    assert result["changed"] is True and result["daemon_reloaded"] is True
    assert not os.path.lexists(worker) and os.path.exists(custody)
    assert reload.listings == [sorted([Path(custody).name, "unrelated.service"])]


def test_module_reports_the_true_state_when_fsync_fails(tmp_path, monkeypatch) -> None:
    module = _unlink_module()
    unit_dir = _unit_tree(tmp_path.resolve())

    def fsync(_fd):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(module.os, "fsync", fsync)
    reload = _Reload(unit_dir)
    result = module.remove_units(str(unit_dir), _uid(), reload=reload)
    assert result["removed"] == _paths(unit_dir)
    assert result["refused"] == ["unexpected error: Input/output error"]
    assert result["not_attempted"] == [] and result["changed"] is True
    assert result["daemon_reloaded"] is True and len(reload.listings) == 1


def test_module_counts_a_name_replaced_after_its_unlink_as_removed(
    tmp_path, monkeypatch
) -> None:
    module = _unlink_module()
    unit_dir = _unit_tree(tmp_path.resolve())
    real_unlink = os.unlink

    def unlink(name, *, dir_fd=None):
        real_unlink(name, dir_fd=dir_fd)
        (unit_dir / name).write_text("racer")

    monkeypatch.setattr(module.os, "unlink", unlink)
    result = module.remove_units(str(unit_dir), _uid(), reload=_Reload(unit_dir))
    worker, custody = _paths(unit_dir)
    assert result["removed"] == [worker]
    assert result["refused"] == [
        f"{worker}: was replaced by a new entry during removal"
    ]
    assert result["not_attempted"] == [custody]
    assert result["changed"] is True and result["daemon_reloaded"] is True


def test_module_mutation_building_the_receipt_after_the_fact_hides_a_removal(
    tmp_path, monkeypatch
) -> None:
    # Mutation check: if outcomes were not recorded into the receipt as each
    # unlink happens (the pre-fix shape), an OSError on the second name would
    # report removed=[] while the worker file is already gone.
    module = _unlink_module()
    unit_dir = _unit_tree(tmp_path.resolve())
    real_unlink = os.unlink
    calls = []

    def unlink(name, *, dir_fd=None):
        calls.append(name)
        if len(calls) == 2:
            raise PermissionError(1, "Operation not permitted")
        real_unlink(name, dir_fd=dir_fd)

    def detached(unit_dir, owner_uid, check_mode, result, _handled):
        scratch = copy.deepcopy(result)
        original(unit_dir, owner_uid, check_mode, scratch, set())
        if scratch["refused"]:
            raise OSError(1, "Operation not permitted")

    original = module._unlink_all
    monkeypatch.setattr(module.os, "unlink", unlink)
    monkeypatch.setattr(module, "_unlink_all", detached)
    result = module.remove_units(str(unit_dir), _uid())
    worker, _custody = _paths(unit_dir)
    assert not os.path.lexists(worker)
    assert result["removed"] == [] and worker in result["not_attempted"]


def test_module_mutation_reloading_before_the_unlinks_sees_the_files(
    tmp_path, monkeypatch
) -> None:
    # Mutation check: a reload outside (before) the unlink phase observes both
    # unit files still present, which the in-call ordering test would reject.
    module = _unlink_module()
    unit_dir = _unit_tree(tmp_path.resolve())
    reload = _Reload(unit_dir)
    original = module._unlink_all

    def reload_first(*args):
        reload()
        original(*args)

    monkeypatch.setattr(module, "_unlink_all", reload_first)
    module.remove_units(str(unit_dir), _uid())
    assert reload.listings[0] != ["unrelated.service"]


def test_module_pins_directories_and_never_follows_links() -> None:
    src = MODULE_PATH.read_text()
    assert "O_NOFOLLOW" in src and "O_DIRECTORY" in src
    assert 'os.open("/"' in src
    assert "os.unlink(name, dir_fd=dir_fd)" in src
    for forbidden in ("os.remove(", "shutil", "rmdir", "os.path.realpath", "open(path"):
        assert forbidden not in src, forbidden


def test_module_mutation_without_nofollow_follows_a_symlinked_unit_dir(
    tmp_path,
) -> None:
    # Mutation check: the O_NOFOLLOW walk is what refuses a swapped parent.
    module = _unlink_module()
    root = tmp_path.resolve()
    real = _unit_tree(root / "real")
    linked = root / "linked/etc/systemd"
    linked.mkdir(parents=True)
    (linked / "system").symlink_to(real)
    module._DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    result = module.remove_units(str(linked / "system"), _uid())
    assert result["removed"]
    assert not (real / Path(WORKER).name).exists()


def test_module_mutation_without_foreign_scan_leaves_a_drop_in_orphaned(
    tmp_path,
) -> None:
    module = _unlink_module()
    unit_dir = _unit_tree(tmp_path.resolve())
    (unit_dir / "ditto-coding-hosted-worker.service.d").mkdir()
    module.foreign_entries = lambda _fd: []
    result = module.remove_units(str(unit_dir), _uid())
    assert (
        result["removed"]
        and (unit_dir / "ditto-coding-hosted-worker.service.d").exists()
    )


def test_module_mutation_without_type_checks_unlinks_a_symlink(tmp_path) -> None:
    module = _unlink_module()
    root = tmp_path.resolve()
    unit_dir = _unit_tree(root, worker=False)
    (unit_dir / Path(WORKER).name).symlink_to(root / "elsewhere")
    fake = {k: getattr(stat, k) for k in dir(stat) if not k.startswith("__")}
    module.stat = SimpleNamespace(**{**fake, "S_ISREG": lambda _mode: True})
    result = module.remove_units(str(unit_dir), _uid())
    assert result["removed"][0].endswith(Path(WORKER).name)


# ─── Playbook, CI and docs ───────────────────────────────────────────────────


def test_fixture_ci_and_docs_registration() -> None:
    (fixture,) = yaml.safe_load(FIXTURE.read_text())
    assert fixture["hosts"] == "localhost" and fixture["become"] is False
    assert fixture["roles"] == ["coding_hosted_unit_uninstall"]
    workflow = yaml.safe_load((ROOT / ".github/workflows/infra-ci.yml").read_text())
    this_file = str(Path(__file__).relative_to(ROOT))
    for trigger in ("pull_request", "push"):
        paths = workflow[True][trigger]["paths"]
        assert "infra/**" in paths
        assert this_file in paths and "pyproject.toml" in paths and "uv.lock" in paths
    steps = workflow["jobs"]["ansible"]["steps"]
    runs = "\n".join(step.get("run", "") for step in steps)
    assert "playbooks/gcp-coding-hosted-unit-uninstall.yml" in runs
    assert "tests/coding-hosted-unit-uninstall.yml" in runs
    (step,) = [
        step
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if REHEARSAL_GATE in step.get("env", {}) and this_file in step.get("run", "")
    ]
    assert step in steps
    assert step["env"] == {REHEARSAL_GATE: "1"}
    assert step["working-directory"] == "${{ github.workspace }}"
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
    uses = [s.get("uses", "") for s in steps]
    assert any(action.startswith("astral-sh/setup-uv@") for action in uses)


def test_docs_describe_every_guard_and_residual() -> None:
    doc = DOC.read_text()
    for phrase in (
        CONFIRMATION,
        *UNIT_PATHS,
        "coding_hosted_connectivity",
        "coding_hosted_custody_service",
        "--limit ditto-coding-hosted-v2",
        "ansible_play_batch == ['ditto-coding-hosted-v2']",
        "ansible_play_hosts_all == ['ditto-coding-hosted-v2']",
        "is sameas true",
        "include_tasks",
        "--start-at-task",
        "O_NOFOLLOW",
        "daemon-reload",
        "inactive or failed",
        "not_attempted",
        "already_absent",
        "check mode",
        f"`{REHEARSAL_GATE}=1`",
        "#1897",
        "#1925",
        "custody-run.py",
        "connectivity-policy.py",
        "Residual",
        "list-jobs",
        "LoadState",
        "ExecStopPost",
        "daemon_reloaded",
        *(command.strip("`") for command in MANUAL_CLEANUP),
    ):
        assert phrase in doc, phrase


# ─── Rehearsal ────────────────────────────────────────────────────────────────

rehearsal = pytest.mark.skipif(
    os.environ.get(REHEARSAL_GATE) != "1",
    reason=f"set {REHEARSAL_GATE}=1 to run the ansible-core rehearsal",
)

# A distinctive value no escaping changes, used as a wrong input; it must never
# appear in ansible output.
CANARY = "Kq7vUninstallCanaryZ3w9"
STOPPED_UNITS = (
    "ditto-coding-hosted-worker.service loaded failed failed Worker\n"
    "ditto-coding-custody@0.service loaded inactive dead Custody\n"
    "ditto-coding-custody@1.service not-found inactive dead ditto-coding-custody@1\n"
)
LIVE_UNITS = {
    "live_custody": "ditto-coding-custody@0.service loaded active running Custody",
    "live_worker": "ditto-coding-hosted-worker.service loaded deactivating stop-sigterm W",  # noqa: E501
    "activating": "ditto-coding-hosted-worker.service loaded activating start Worker",
    "reloading": "ditto-coding-custody@0.service loaded reloading reload Custody",
    "refreshing": "ditto-coding-custody@2.service loaded refreshing refresh-extensions C",  # noqa: E501
    "maintenance": "ditto-coding-custody@3.service loaded maintenance cleaning Custody",
    "unknown_state": "ditto-coding-hosted-worker.service loaded quiescent idle Worker",
    "unparseable": "● ditto-coding-custody@0.service loaded inactive dead Custody",
}
INTERNAL_PRESETS = {
    GATE_NAME: True,
    f"{PREFIX}gate_confirmation": CONFIRMATION,
    f"{PREFIX}gate_source_revision": REVISION,
    f"{PREFIX}identity": {"ansible_facts": {"ansible_hostname": REVIEWED_HOST}},
    f"{PREFIX}units": {"stdout_lines": []},
    f"{PREFIX}jobs": {"stdout": ""},
    f"{PREFIX}removal": {
        "refused": [],
        "removed": [],
        "already_absent": [],
        "daemon_reloaded": True,
    },
    f"{PREFIX}units_after": {"stdout_lines": []},
    f"{PREFIX}jobs_after": {"stdout": ""},
    f"{PREFIX}load_states": {"rc": 0, "stdout_lines": ["not-found", "not-found"]},
    f"{PREFIX}undocumented": "x",
}


def _rewrite(node: Any, *, local_identity: bool) -> Any:
    """Point remove.yml at a temporary tree: unit dir, owner, systemctl, identity."""
    if isinstance(node, dict):
        out = {
            key: value
            if key == "ansible.builtin.assert"
            else _rewrite(value, local_identity=local_identity)
            for key, value in node.items()
        }
        command = out.get("ansible.builtin.command")
        if command and command["argv"][0] == "/usr/bin/systemctl":
            stand_in = {
                f"{PREFIX}units": "{{ rehearsal_units }}",
                f"{PREFIX}jobs": "{{ rehearsal_jobs }}",
                f"{PREFIX}units_after": (
                    "{{ rehearsal_units_after | default(rehearsal_units) }}"
                ),
                f"{PREFIX}jobs_after": (
                    "{{ rehearsal_jobs_after | default(rehearsal_jobs) }}"
                ),
                f"{PREFIX}load_states": "{{ rehearsal_load_states }}",
            }[out["register"]]
            out["ansible.builtin.command"] = {
                "argv": ["/usr/bin/printf", "%s", stand_in]
            }
        if MODULE_NAME in out:
            out[MODULE_NAME] = {
                "unit_dir": "{{ rehearsal_root }}" + out[MODULE_NAME]["unit_dir"],
                "owner": "{{ rehearsal_owner }}",
                "daemon_reload": "{{ rehearsal_reload_argv }}",
            }
        if out.get("name") == REPORT:
            out["register"] = "rehearsal_report"
        if "ansible.builtin.assert" in out and local_identity:
            identity = _local_identity()
            that = [
                re.sub(
                    rf"^({PREFIX}identity\.ansible_facts\.(ansible_\w+)) == '[^']+'$",
                    lambda m: f"{m[1]} == {json.dumps(identity[m[2]])}",
                    line,
                )
                for line in out["ansible.builtin.assert"]["that"]
            ]
            out["ansible.builtin.assert"] = dict(
                out["ansible.builtin.assert"], that=that
            )
        return out
    if isinstance(node, list):
        return [_rewrite(item, local_identity=local_identity) for item in node]
    return node


@functools.cache
def _local_identity() -> dict[str, str]:
    """This machine's probed identity, as literals: --start-at-task skips any
    in-play probe, so the rewritten host check compares against constants."""
    completed = subprocess.run(
        [
            "uvx",
            "--from",
            "ansible-core==2.21.2",
            "ansible",
            "-i",
            "localhost,",
            "-c",
            "local",
            "-m",
            "ansible.builtin.setup",
            "-a",
            "gather_subset=!all,!min,platform,distribution",
            "localhost",
        ],
        env={k: v for k, v in os.environ.items() if not k.startswith("ANSIBLE_")}
        | {"ANSIBLE_NOCOLOR": "1", "ANSIBLE_PYTHON_INTERPRETER": sys.executable},
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
    )
    facts = json.loads(completed.stdout.split("=> ", 1)[1])["ansible_facts"]
    return {
        key: facts[key]
        for key in (
            "ansible_hostname",
            "ansible_architecture",
            "ansible_distribution",
            "ansible_distribution_major_version",
        )
    }


def _build_role(
    dst: Path,
    *,
    local_identity: bool,
    real_batch: bool,
    mutate_main=None,
    mutate_remove=None,
) -> None:
    role = dst / "roles/coding_hosted_unit_uninstall"
    (role / "tasks").mkdir(parents=True)
    shutil.copytree(ROLE / "defaults", role / "defaults")
    shutil.copytree(
        ROLE / "library", role / "library", ignore=shutil.ignore_patterns("__pycache__")
    )
    tasks = _rewrite(copy.deepcopy(_docs(REMOVE)), local_identity=local_identity)
    if not real_batch:
        # Multi-host bulk runs cannot be the single dedicated host; the batch
        # guard is rehearsed with its real literal in the targeting test.
        _task(BATCH, tasks)["ansible.builtin.assert"]["that"] = [
            "ansible_play_batch == ansible_play_batch"
        ]
    # No host-acting argument still reaches the real systemd.
    operative = json.dumps(
        [
            {k: v for k, v in t.items() if k not in ("name", "ansible.builtin.assert")}
            for t in _walk(tasks)
        ]
    )
    assert "systemctl" not in operative
    assert '"/etc/systemd' not in operative
    assert _task(PRESET_INCLUDE, tasks) == _task(PRESET_INCLUDE)
    assert _task(GATE, tasks) == _task(GATE)
    assert _task(LIVE, tasks) == _task(LIVE)
    main = _docs(MAIN)
    if mutate_main is not None:
        mutate_main(main)
    if mutate_remove is not None:
        mutate_remove(tasks)
    (role / "tasks/main.yml").write_text(
        MAIN if mutate_main is None else yaml.safe_dump(main, sort_keys=False)
    )
    (role / "tasks/remove.yml").write_text(yaml.safe_dump(tasks, sort_keys=False))


def _record(outcome: str, content: str) -> dict:
    return {
        "ansible.builtin.copy": {"dest": outcome, "content": content},
        "check_mode": False,
        "no_log": True,
        "diff": False,
    }


def _play(rehearsal_pass: str, serial: int | None = None) -> dict:
    outcome = "{{ rehearsal_root }}/outcome-" + rehearsal_pass + ".json"
    play: dict[str, Any] = {
        "name": f"Rehearse unit uninstall ({rehearsal_pass})",
        "hosts": "all",
        "strategy": "free" if serial is None else "linear",
        "gather_facts": False,
        "become": False,
        "vars": {"ansible_python_interpreter": "{{ ansible_playbook_python }}"},
        "tasks": [
            {
                "name": "Rehearse the role",
                "block": [
                    # A static import, like the playbook's roles: list, so
                    # --start-at-task sees exactly the production task list.
                    {
                        "name": "Import the role",
                        "ansible.builtin.import_role": {
                            "name": "coding_hosted_unit_uninstall"
                        },
                    },
                    {
                        "name": "Record completion",
                        **_record(
                            outcome,
                            "{{ {'report': rehearsal_report.msg | default(none)}"
                            " | to_json }}",
                        ),
                    },
                ],
                "rescue": [
                    {
                        "name": "Record refusal",
                        **_record(
                            outcome,
                            "{{ {'task': ansible_failed_task.name, 'messages': "
                            "[ansible_failed_result.msg | default('')]} | to_json }}",
                        ),
                    }
                ],
            },
        ],
    }
    if serial is not None:
        play["serial"] = serial
    return play


def _run(
    tmp_path: Path,
    name: str,
    hosts: dict[str, dict],
    *flags: str,
    rehearsal_pass: str = "first",
    local_identity: bool = True,
    real_batch: bool = False,
    serial: int | None = None,
    mutate_main=None,
    mutate_remove=None,
    allow_deprecation: bool = False,
) -> str:
    work = tmp_path / "runs" / name
    work.mkdir(parents=True)
    _build_role(
        work,
        local_identity=local_identity,
        real_batch=real_batch,
        mutate_main=mutate_main,
        mutate_remove=mutate_remove,
    )
    inventory = {"all": {"children": {"role_coding_hosted": {"hosts": hosts}}}}
    (work / "inventory.yml").write_text(yaml.safe_dump(inventory))
    (work / "play.yml").write_text(
        yaml.safe_dump([_play(rehearsal_pass, serial)], sort_keys=False)
    )
    shutil.copy(REPO_ANSIBLE_CFG, work / "ansible.cfg")
    config = REPO_ANSIBLE_CFG.read_text()
    assert re.search(r"^roles_path\s*=\s*roles$", config, re.M)
    environment = {k: v for k, v in os.environ.items() if not k.startswith("ANSIBLE_")}
    environment |= {
        "ANSIBLE_CONFIG": str(work / "ansible.cfg"),
        "ANSIBLE_HOME": str(work / "ansible-home"),
        "ANSIBLE_LOCAL_TEMP": str(work / "ansible-tmp"),
        "ANSIBLE_NOCOLOR": "1",
    }
    completed = subprocess.run(
        [
            "uvx",
            "--from",
            "ansible-core==2.21.2",
            "ansible-playbook",
            "-f",
            "8",
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
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output[-6000:]
    assert CANARY not in output, name
    if not allow_deprecation:
        assert "DEPRECATION" not in output, (name, output[-3000:])
    return output


def _host(root: Path, **overrides: Any) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    return {
        "ansible_connection": "local",
        f"{PREFIX}enabled": True,
        f"{PREFIX}confirmation": CONFIRMATION,
        f"{PREFIX}source_revision": REVISION,
        "rehearsal_root": str(root),
        "rehearsal_units": STOPPED_UNITS,
        "rehearsal_jobs": "",
        "rehearsal_load_states": "not-found\n\nnot-found\n",
        # Stands in for systemctl daemon-reload: records the unit directory as
        # the module's in-call reload sees it.
        "rehearsal_reload_argv": [
            "/usr/bin/sh",
            "-c",
            'ls -A "$1" > "$2" 2>/dev/null || : > "$2"',
            "sh",
            f"{root}/etc/systemd/system",
            f"{root}/daemon-reloaded",
        ],
        "rehearsal_owner": pwd.getpwuid(os.getuid()).pw_name,
        **overrides,
    }


def _outcome(root: Path, rehearsal_pass: str = "first") -> dict | None:
    path = root / f"outcome-{rehearsal_pass}.json"
    return json.loads(path.read_text()) if path.exists() else None


def _recorded(root: Path, rehearsal_pass: str = "first") -> dict:
    outcome = _outcome(root, rehearsal_pass)
    assert outcome is not None, root.name
    return outcome


def _units(root: Path) -> list[Path]:
    return [root / p.lstrip("/") for p in UNIT_PATHS]


def _kept(root: Path) -> bool:
    return all(path.read_text() == "[Unit]\n" for path in _units(root))


def _gone(root: Path) -> bool:
    return not any(os.path.lexists(path) for path in _units(root))


def _rooted(root: Path, paths: list[str]) -> str:
    return json.dumps([f"{root}{p}" for p in paths])


def _report(root: Path, removed: list[str], absent: list[str]) -> str:
    return (
        f"Native Coding worker and custody unit uninstall; source_revision={REVISION}; "
        f"removed={_rooted(root, removed)}; already_absent={_rooted(root, absent)}; "
        "daemon_reloaded=true; services_stopped=false; services_started=false; "
        "credentials_touched=false; data_dirs_touched=false."
    )


def _message(task: str) -> str:
    source = _docs(MAIN) if task in {t["name"] for t in _docs(MAIN)} else _docs(REMOVE)
    return _flat(_task(task, source)["ansible.builtin.assert"]["fail_msg"])


def _assert_refused(root: Path, task: str, rehearsal_pass: str = "first") -> None:
    outcome = _outcome(root, rehearsal_pass)
    assert outcome is not None and outcome.get("task") == task, (root.name, outcome)
    if "{{" not in _message(task):
        assert _message(task) in [_flat(m) for m in outcome["messages"]], outcome


def _assert_refused_untouched(root: Path, task: str) -> None:
    _assert_refused(root, task)
    assert _kept(root), root.name
    assert not (root / "daemon-reloaded").exists(), root.name


@rehearsal
def test_rehearsal_uninstalls_only_when_every_guard_passes(tmp_path) -> None:
    tmp_path = tmp_path.resolve()
    roots: dict[str, Path] = {}
    hosts: dict[str, dict] = {}

    def add(name: str, **overrides: Any) -> Path:
        tree = overrides.pop("tree", True)
        root = tmp_path / "hosts" / name
        hosts[name] = _host(root, **overrides)
        if tree:
            _unit_tree(root)
        roots[name] = root
        return root

    add("removed")
    add("disabled", **{f"{PREFIX}enabled": False})
    add("enabled_missing", **{f"{PREFIX}enabled": None})
    add("enabled_string", **{f"{PREFIX}enabled": CANARY})
    add("enabled_true_string", **{f"{PREFIX}enabled": "true"})
    add("enabled_flip", **{f"{PREFIX}enabled": "{{ item is defined }}"})
    _unit_tree(add("already_absent", tree=False), worker=False, custody=False)
    add("no_unit_dir", tree=False)
    _unit_tree(add("worker_only", tree=False), custody=False)
    add("confirmation_wrong", **{f"{PREFIX}confirmation": CANARY})
    add("confirmation_lower", **{f"{PREFIX}confirmation": CONFIRMATION.lower()})
    add("revision_upper", **{f"{PREFIX}source_revision": REVISION.upper()})
    add("revision_short", **{f"{PREFIX}source_revision": REVISION[:39]})
    add("revision_newline", **{f"{PREFIX}source_revision": REVISION + "\n"})
    add("revision_missing", **{f"{PREFIX}source_revision": None})
    for state, line in LIVE_UNITS.items():
        add(f"live_{state}", rehearsal_units=STOPPED_UNITS + line + "\n")
    for preset, value in INTERNAL_PRESETS.items():
        add(f"preset_{preset.removeprefix(PREFIX)}", **{preset: value})
    add("went_live_after", rehearsal_units_after=LIVE_UNITS["live_custody"] + "\n")
    # A queued start shows up in list-units only as an extra JOB column word.
    add(
        "queued_job",
        rehearsal_units=(
            "ditto-coding-hosted-worker.service loaded inactive dead start Worker\n"
        ),
        rehearsal_jobs="42 ditto-coding-hosted-worker.service start waiting\n",
    )
    add(
        "queued_job_after",
        rehearsal_jobs_after="43 ditto-coding-custody@0.service start waiting\n",
    )
    add("shadow_load_state", rehearsal_load_states="loaded\n\nnot-found\n")
    add("shadow_template", rehearsal_load_states="not-found\n\nloaded\n")
    add("load_state_empty", rehearsal_load_states="")
    add("reload_fails", rehearsal_reload_argv=["/usr/bin/false"])
    for swap in ("symlink", "hardlink", "directory"):
        root = add(f"swap_{swap}", tree=False)
        unit_dir = _unit_tree(root, worker=False)
        worker = unit_dir / Path(WORKER).name
        (root / "outside").write_text("keep")
        if swap == "symlink":
            worker.symlink_to(root / "outside")
        elif swap == "hardlink":
            os.link(root / "outside", worker)
        else:
            worker.mkdir()
    partial = add("partial", tree=False)
    unit_dir = _unit_tree(partial, custody=False)
    (unit_dir / Path(CUSTODY).name).symlink_to(partial / "outside")
    (partial / "outside").write_text("keep")
    parent_link = add("parent_symlink", tree=False)
    real = _unit_tree(parent_link / "real")
    (parent_link / "etc/systemd").mkdir(parents=True)
    (parent_link / "etc/systemd/system").symlink_to(real)
    add("dir_writable", tree=False)
    _unit_tree(roots["dir_writable"]).chmod(0o775)
    add("drop_in", tree=False)
    (_unit_tree(roots["drop_in"]) / "ditto-coding-hosted-worker.service.d").mkdir()
    add("wants_link", tree=False)
    wants = _unit_tree(roots["wants_link"]) / "multi-user.target.wants"
    wants.mkdir()
    (wants / "ditto-coding-hosted-worker.service").symlink_to(
        roots["wants_link"] / "etc/systemd/system/ditto-coding-hosted-worker.service"
    )

    output = _run(tmp_path, "bulk", hosts)
    assert "PLAY RECAP" in output

    # A full removal reloads and reports both paths; the unrelated unit stays.
    assert _outcome(roots["removed"]) == {
        "report": _report(roots["removed"], UNIT_PATHS, [])
    }
    assert _gone(roots["removed"])
    # The module's in-call reload ran after both unlinks.
    assert (roots["removed"] / "daemon-reloaded").read_text() == "unrelated.service\n"
    assert (roots["removed"] / "etc/systemd/system/unrelated.service").exists()
    assert _outcome(roots["already_absent"]) == {
        "report": _report(roots["already_absent"], [], UNIT_PATHS)
    }
    assert _outcome(roots["no_unit_dir"]) == {
        "report": _report(roots["no_unit_dir"], [], UNIT_PATHS)
    }
    assert _outcome(roots["worker_only"]) == {
        "report": _report(roots["worker_only"], [WORKER], [CUSTODY])
    }
    # Disabled, missing, non-boolean and lazily templated flags are a no-op.
    for name in (
        "disabled",
        "enabled_missing",
        "enabled_string",
        "enabled_true_string",
        "enabled_flip",
    ):
        assert _outcome(roots[name]) == {"report": None}, name
        assert _kept(roots[name]) and not (roots[name] / "daemon-reloaded").exists(), (
            name
        )
    for name in (
        "confirmation_wrong",
        "confirmation_lower",
        "revision_upper",
        "revision_short",
        "revision_newline",
        "revision_missing",
    ):
        _assert_refused_untouched(roots[name], GATE)
    for state in LIVE_UNITS:
        _assert_refused_untouched(roots[f"live_{state}"], LIVE)
    for preset in INTERNAL_PRESETS:
        _assert_refused_untouched(
            roots[f"preset_{preset.removeprefix(PREFIX)}"], PRESET
        )
    _assert_refused_untouched(roots["queued_job"], LIVE)
    # Removed, then refused on the post-removal checks, naming the manual cleanup
    # and the true receipt.
    for name in ("went_live_after", "queued_job_after"):
        _assert_refused(roots[name], LIVE_AFTER)
        assert _gone(roots[name])
        (message,) = _recorded(roots[name])["messages"]
        for command in MANUAL_CLEANUP:
            assert command in _flat(message), (name, command)
        assert f"removed={_rooted(roots[name], UNIT_PATHS)}; refused=[]" in _flat(
            message
        )
    for name in ("shadow_load_state", "shadow_template", "load_state_empty"):
        _assert_refused(roots[name], LOAD_STATES_CHECK)
        assert _gone(roots[name])
    _assert_refused(roots["reload_fails"], UNLINK_CHECK)
    (message,) = _recorded(roots["reload_fails"])["messages"]
    assert f"removed={_rooted(roots['reload_fails'], UNIT_PATHS)}" in _flat(message)
    assert 'refused=["daemon-reload failed"]' in _flat(message)
    assert "daemon_reloaded=false" in _flat(message)
    # Swapped entries: nothing after the refusal is attempted, the link target
    # survives, systemd is still reloaded and the receipt names every state.
    for swap in ("symlink", "hardlink", "directory"):
        root = roots[f"swap_{swap}"]
        _assert_refused(root, UNLINK_CHECK)
        (message,) = _recorded(root)["messages"]
        worker_path = f"{root}{WORKER}"
        assert f'refused=["{worker_path}: ' in _flat(message), message
        assert (
            f"removed=[]; already_absent=[]; not_attempted={_rooted(root, [CUSTODY])}"
            in _flat(message)
        )
        assert (root / "outside").read_text() == "keep"
        assert (root / CUSTODY.lstrip("/")).exists()
        assert (root / "daemon-reloaded").exists()
    root = roots["partial"]
    _assert_refused(root, UNLINK_CHECK)
    (message,) = _recorded(root)["messages"]
    assert f"removed={_rooted(root, [WORKER])}" in _flat(message)
    assert (
        f'refused=["{root}{CUSTODY}: is a symlink, directory or special file"]'
        in _flat(message)
    )
    assert not os.path.lexists(root / WORKER.lstrip("/"))
    assert (root / "outside").read_text() == "keep"
    assert (root / "daemon-reloaded").exists()
    # Directory-level refusals remove nothing.
    for name in ("parent_symlink", "dir_writable", "drop_in", "wants_link"):
        _assert_refused(roots[name], UNLINK_CHECK)
        (message,) = _recorded(roots[name])["messages"]
        assert "removed=[]" in _flat(message), (name, message)
    assert all(
        (roots["parent_symlink"] / "real" / p.lstrip("/")).exists() for p in UNIT_PATHS
    )
    assert (
        _kept(roots["dir_writable"])
        and _kept(roots["drop_in"])
        and _kept(roots["wants_link"])
    )
    assert 'foreign=["ditto-coding-hosted-worker.service.d"]' in _flat(
        _recorded(roots["drop_in"])["messages"][0]
    )
    assert (
        'foreign=["multi-user.target.wants/ditto-coding-hosted-worker.service"]'
        in _flat(_recorded(roots["wants_link"])["messages"][0])
    )

    # Idempotent second run: what the first run removed is now already absent.
    second = {n: hosts[n] for n in ("removed", "worker_only")}
    _run(tmp_path, "second", second, rehearsal_pass="second")
    assert _outcome(roots["removed"], "second") == {
        "report": _report(roots["removed"], [], UNIT_PATHS)
    }
    assert _outcome(roots["worker_only"], "second") == {
        "report": _report(roots["worker_only"], [], UNIT_PATHS)
    }


@rehearsal
def test_rehearsal_forged_identity_and_facts_are_refused(tmp_path) -> None:
    tmp_path = tmp_path.resolve()
    root = tmp_path / "hosts/forged"
    hosts = {"forged": _host(root)}
    _unit_tree(root)
    forged = {
        "ansible_facts": {
            "hostname": REVIEWED_HOST,
            "architecture": "x86_64",
            "distribution": "Debian",
            "distribution_major_version": "13",
        },
        "ansible_hostname": REVIEWED_HOST,
    }
    # Production identity literals: this machine is not the dedicated host, and
    # extra-vars facts cannot stand in for the registered probe.
    _run(tmp_path, "forged", hosts, "-e", json.dumps(forged), local_identity=False)
    _assert_refused_untouched(root, HOST)


@rehearsal
def test_rehearsal_targets_exactly_the_one_dedicated_host(tmp_path) -> None:
    tmp_path = tmp_path.resolve()
    ok = tmp_path / "hosts/ok"
    _unit_tree(ok)
    _run(tmp_path, "single", {REVIEWED_HOST: _host(ok)}, real_batch=True)
    assert _outcome(ok) == {"report": _report(ok, UNIT_PATHS, [])}

    for name, serial in (("extra", None), ("serial", 1)):
        roots = {
            h: tmp_path / "hosts" / f"{name}_{h}" for h in (REVIEWED_HOST, "rogue-vm")
        }
        for root in roots.values():
            _unit_tree(root)
        _run(
            tmp_path,
            name,
            {h: _host(r) for h, r in roots.items()},
            real_batch=True,
            serial=serial,
        )
        for root in roots.values():
            _assert_refused_untouched(root, BATCH)

    # -e inventory_hostname cannot forge the batch.
    forged = tmp_path / "hosts/forged_name"
    _unit_tree(forged)
    _run(
        tmp_path,
        "forged_name",
        {"wrong-host": _host(forged)},
        "-e",
        f"inventory_hostname={REVIEWED_HOST}",
        real_batch=True,
    )
    _assert_refused_untouched(forged, BATCH)


@rehearsal
def test_rehearsal_check_mode_is_refused_when_enabled_and_dormant_when_not(
    tmp_path,
) -> None:
    tmp_path = tmp_path.resolve()
    roots = {n: tmp_path / "hosts" / n for n in ("enabled", "disabled")}
    for root in roots.values():
        _unit_tree(root)
    hosts = {
        "enabled": _host(roots["enabled"]),
        "disabled": _host(roots["disabled"], **{f"{PREFIX}enabled": False}),
    }
    _run(tmp_path, "check", hosts, "--check")
    _assert_refused_untouched(roots["enabled"], CHECK_MODE)
    assert _outcome(roots["disabled"]) == {"report": None}
    assert _kept(roots["disabled"])


def _start_hosts(tmp_path: Path, label: str) -> dict[str, tuple[dict, Path]]:
    out = {}
    for name, enabled in (("on", True), ("off", False)):
        root = tmp_path / "hosts" / f"{label}_{name}"
        _unit_tree(root)
        out[name] = (
            _host(
                root,
                **{f"{PREFIX}enabled": enabled},
                rehearsal_units=STOPPED_UNITS + LIVE_UNITS["live_custody"] + "\n",
            ),
            root,
        )
    return out


@rehearsal
def test_rehearsal_start_at_every_task_cannot_skip_the_guards(tmp_path) -> None:
    tmp_path = tmp_path.resolve()
    # Presets that would bypass the live-unit guard and open the gate if any
    # start point could skip the in-include refusal. Both hosts list a live unit.
    presets = json.dumps({GATE_NAME: True, f"{PREFIX}units": {"stdout_lines": []}})
    main_names = [t["name"] for t in _docs(MAIN)]
    remove_names = [t["name"] for t in _docs(REMOVE)]
    for index, task in enumerate(main_names + remove_names):
        label = f"start{index:02d}"
        hosts = _start_hosts(tmp_path, label)
        _run(
            tmp_path,
            label,
            {n: h for n, (h, _) in hosts.items()},
            "--start-at-task",
            task,
            "-e",
            presets,
        )
        for name, (_, root) in hosts.items():
            assert _kept(root), (task, name)
            assert not (root / "daemon-reloaded").exists(), (task, name)
            outcome = _outcome(root)
            if task == PRESET:
                _assert_refused(root, PRESET)
            elif task in main_names:
                # Starting past main.yml's refusal reaches the include, whose
                # first task refuses the preset gate and registered result.
                assert outcome is not None and outcome["task"] == PRESET_INCLUDE, (
                    task,
                    outcome,
                )
            else:
                # Tasks of the dynamically included file are invisible to
                # --start-at-task, so nothing in the play ran at all.
                assert outcome is None, (task, outcome)


# ─── Mutations: each guard is load-bearing ────────────────────────────────────


def _drop(name: str):
    def mutate(tasks: list[dict]) -> None:
        tasks[:] = [t for t in tasks if t.get("name") != name]

    return mutate


def _drop_that(name: str, index: int):
    def mutate(tasks: list[dict]) -> None:
        del _task(name, tasks)["ansible.builtin.assert"]["that"][index]

    return mutate


def _mutant(tmp_path: Path, label: str, host: dict, *flags: str, **kwargs: Any) -> Path:
    root = Path(host["rehearsal_root"])
    _run(tmp_path, f"mutant_{label}", {label: host}, *flags, **kwargs)
    return root


def _tree_host(tmp_path: Path, label: str, **overrides: Any) -> dict:
    root = tmp_path / "hosts" / f"mutant_{label}"
    _unit_tree(root)
    return _host(root, **overrides)


@rehearsal
def test_rehearsal_mutations_prove_every_guard_is_load_bearing(tmp_path) -> None:
    tmp_path = tmp_path.resolve()
    live = STOPPED_UNITS + LIVE_UNITS["live_custody"] + "\n"

    # main.yml preset refusal and in-include refusal both removed: a -e preset
    # registered listing hides a live unit and the units are removed.
    both = _mutant(
        tmp_path,
        "presets",
        _tree_host(tmp_path, "presets", rehearsal_units=live),
        "-e",
        json.dumps({f"{PREFIX}units": {"stdout_lines": []}}),
        mutate_main=_drop(PRESET),
        mutate_remove=_drop(PRESET_INCLUDE),
    )
    assert _gone(both)

    # In-include refusal removed: --start-at-task past main.yml's refusal with
    # the same preset removes the units.
    start = _mutant(
        tmp_path,
        "in_include",
        _tree_host(tmp_path, "in_include", rehearsal_units=live),
        "--start-at-task",
        GATE_FREEZE,
        "-e",
        json.dumps({f"{PREFIX}units": {"stdout_lines": []}}),
        mutate_remove=_drop(PRESET_INCLUDE),
    )
    assert _gone(start)

    # Raw enabled re-assert removed: --start-at-task past main.yml's refusal
    # with -e gate=true opens the removal even though enabled is false.
    raw = _mutant(
        tmp_path,
        "raw_enabled",
        _tree_host(tmp_path, "raw_enabled", **{f"{PREFIX}enabled": False}),
        "--start-at-task",
        GATE_FREEZE,
        "-e",
        json.dumps({GATE_NAME: True}),
        mutate_remove=_drop_that(PRESET_INCLUDE, 0),
    )
    assert _gone(raw)

    # The gate filter replaced by bool: a non-boolean string opens the removal.
    def bool_gate(main: list[dict]) -> None:
        _task(GATE_FREEZE, main)["ansible.builtin.set_fact"][GATE_NAME] = (
            f"{{{{ {PREFIX}enabled | bool }}}}"
        )

    def bool_raw(tasks: list[dict]) -> None:
        _task(PRESET_INCLUDE, tasks)["ansible.builtin.assert"]["that"][0] = (
            f"{PREFIX}enabled | bool"
        )

    root = tmp_path / "hosts/mutant_bool"
    _unit_tree(root)
    _run(
        tmp_path,
        "mutant_bool",
        {"bool": _host(root, **{f"{PREFIX}enabled": "yes"})},
        mutate_main=bool_gate,
        mutate_remove=bool_raw,
        allow_deprecation=True,
    )
    assert _gone(root)

    # Batch guard removed: an extra host in the play is uninstalled too.
    roots = {
        h: tmp_path / "hosts" / f"mutant_batch_{h}" for h in (REVIEWED_HOST, "rogue-vm")
    }
    for r in roots.values():
        _unit_tree(r)
    _run(
        tmp_path,
        "mutant_batch",
        {h: _host(r) for h, r in roots.items()},
        real_batch=True,
        mutate_remove=_drop(BATCH),
    )
    assert all(_gone(r) for r in roots.values())

    # Check-mode guard removed: an enabled --check run probes the host and
    # drives the module in check mode instead of refusing up front; it then
    # fails only on the receipt because nothing was removed.
    check = _mutant(
        tmp_path,
        "check",
        _tree_host(tmp_path, "check"),
        "--check",
        mutate_remove=_drop(CHECK_MODE),
    )
    outcome = _outcome(check)
    assert outcome is not None and outcome["task"] == UNLINK_CHECK, outcome
    assert _kept(check) and not (check / "daemon-reloaded").exists()

    # Confirmation/revision guard removed: a wrong confirmation uninstalls.
    gate = _mutant(
        tmp_path,
        "gate",
        _tree_host(tmp_path, "gate", **{f"{PREFIX}confirmation": "no"}),
        mutate_remove=_drop(GATE),
    )
    assert _gone(gate)

    # Host guard removed: a forged-fact run on the wrong machine uninstalls.
    host = _mutant(
        tmp_path,
        "host",
        _tree_host(tmp_path, "host"),
        "-e",
        json.dumps({"ansible_hostname": REVIEWED_HOST}),
        local_identity=False,
        mutate_remove=_drop(HOST),
    )
    assert _gone(host)

    # Live-unit guard removed: a live custody instance loses its template.
    live_root = _mutant(
        tmp_path,
        "live",
        _tree_host(tmp_path, "live", rehearsal_units=live),
        mutate_remove=_drop(LIVE),
    )
    assert _gone(live_root)

    # Module receipt check removed: a refused symlink swap reports success.
    swap = tmp_path / "hosts/mutant_receipt"
    unit_dir = _unit_tree(swap, worker=False)
    (unit_dir / Path(WORKER).name).symlink_to(swap / "outside")
    _run(
        tmp_path,
        "mutant_receipt",
        {"receipt": _host(swap)},
        mutate_remove=_drop(UNLINK_CHECK),
    )
    outcome = _outcome(swap)
    assert outcome is not None and outcome.get("report", "").startswith(
        "Native Coding worker and custody unit uninstall"
    )

    # Receipt refusal and reload-flag lines removed: a failed in-call reload is
    # reported as a completed uninstall with daemon_reloaded=false.
    def no_reload_check(tasks: list[dict]) -> None:
        _drop_that(UNLINK_CHECK, 3)(tasks)
        _drop_that(UNLINK_CHECK, 0)(tasks)

    skipped = _mutant(
        tmp_path,
        "reload_check",
        _tree_host(tmp_path, "reload_check", rehearsal_reload_argv=["/usr/bin/false"]),
        mutate_remove=no_reload_check,
    )
    assert _gone(skipped)
    assert "daemon_reloaded=false" in _recorded(skipped)["report"]

    # Queued-job check removed before removal: a queued start loses its unit.
    queued = _mutant(
        tmp_path,
        "jobs",
        _tree_host(
            tmp_path,
            "jobs",
            rehearsal_jobs="42 ditto-coding-hosted-worker.service start waiting\n",
        ),
        mutate_remove=_drop_that(LIVE, 1),
    )
    assert _gone(queued)

    # Queued-job re-check removed: a job queued during removal is reported clean.
    queued_after = _mutant(
        tmp_path,
        "jobs_after",
        _tree_host(
            tmp_path,
            "jobs_after",
            rehearsal_jobs_after="43 ditto-coding-custody@0.service start waiting\n",
        ),
        mutate_remove=_drop_that(LIVE_AFTER, 1),
    )
    assert "report" in _recorded(queued_after)

    # Post-removal live check removed: a unit that went live is reported clean.
    after = _mutant(
        tmp_path,
        "live_after",
        _tree_host(tmp_path, "live_after", rehearsal_units_after=live),
        mutate_remove=_drop(LIVE_AFTER),
    )
    assert "report" in _recorded(after)

    # Positive load-state check weakened to the old empty-output test: an empty
    # answer (what a systemctl or D-Bus error can print) is reported clean.
    def empty_output_check(tasks: list[dict]) -> None:
        _task(LOAD_STATES_CHECK, tasks)["ansible.builtin.assert"]["that"] = [
            f"{PREFIX}load_states.stdout | trim | length == 0"
        ]

    empty = _mutant(
        tmp_path,
        "load_state_empty",
        _tree_host(tmp_path, "load_state_empty", rehearsal_load_states=""),
        mutate_remove=empty_output_check,
    )
    assert "report" in _recorded(empty)

    # Load-state check removed: a shadow definition is reported clean.
    shadow = _mutant(
        tmp_path,
        "shadow",
        _tree_host(tmp_path, "shadow", rehearsal_load_states="loaded\n\nnot-found\n"),
        mutate_remove=_drop(LOAD_STATES_CHECK),
    )
    assert "report" in _recorded(shadow)
