"""Native PostgreSQL environment removal stays default-off, surgical and silent."""

import copy
import hashlib
import importlib.util
import json
import os
import pwd
import re
import shutil
import socket
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/coding_hosted_postgres_environment_cleanup"
MAIN = (ROLE / "tasks/main.yml").read_text()
REMOVE = (ROLE / "tasks/remove.yml").read_text()
MODULE_NAME = "coding_hosted_postgres_environment_unlink"
MODULE_PATH = ROLE / f"library/{MODULE_NAME}.py"
# Parsed tasks without YAML comments, for forbidden-token scans.
PARSED = yaml.safe_dump(yaml.safe_load(REMOVE), width=10_000)
PARSED_MAIN = yaml.safe_dump(yaml.safe_load(MAIN), width=10_000)
MATERIALIZE_ROLE = ROOT / "infra/ansible/roles/coding_hosted_postgres_environment"
# The materialization role's guarded tasks live in its dynamically included file.
MATERIALIZE = yaml.safe_load((MATERIALIZE_ROLE / "tasks/materialize.yml").read_text())
MATERIALIZE_HOST = "Require the exact host, source, database address and confirmation"
MATERIALIZE_PRESET = "Refuse preset registered results and undocumented role inputs"
PLAYBOOK = (
    ROOT / "infra/ansible/playbooks/gcp-coding-hosted-postgres-environment-cleanup.yml"
)
REPO_ANSIBLE_CFG = ROOT / "infra/ansible/ansible.cfg"

CUSTODY = "/var/lib/ditto-coding-custody/private/postgres-environment.json"
HOSTED = "/var/lib/ditto-coding-hosted/private/postgres-environment.json"
COPIES = [CUSTODY, HOSTED]
OWNERS = ("ditto-coding-custody", "ditto-coding-hosted")
COPY_ITEMS = [
    {"path": CUSTODY, "owner": "ditto-coding-custody"},
    {"path": HOSTED, "owner": "ditto-coding-hosted"},
]
PARENT_ITEMS = [
    {"path": "/var/lib/ditto-coding-custody", "owner": "ditto-coding-custody"},
    {"path": "/var/lib/ditto-coding-custody/private", "owner": "ditto-coding-custody"},
    {"path": "/var/lib/ditto-coding-hosted", "owner": "ditto-coding-hosted"},
    {"path": "/var/lib/ditto-coding-hosted/private", "owner": "ditto-coding-hosted"},
]
PARENTS = [item["path"] for item in PARENT_ITEMS]
PREFIX = "coding_hosted_postgres_environment_cleanup_"
MATERIALIZE_PREFIX = "coding_hosted_postgres_environment_"
INPUTS = {
    f"{PREFIX}enabled": False,
    f"{PREFIX}confirmation": "",
    f"{PREFIX}source_revision": "",
}
FROZEN_GATE = f"{PREFIX}frozen_enabled"
FROZEN_REVISION = f"{PREFIX}frozen_revision"
FROZEN_CONFIRMATION = f"{PREFIX}frozen_confirmation"
ALLOWED_AT_GUARD = sorted([*INPUTS, FROZEN_GATE])
GATE = f"{FROZEN_GATE} is sameas true"
REVISION_MESSAGE = f"source_revision={{{{ {FROZEN_REVISION} }}}}"
CONFIRMATION = "REMOVE NATIVE CODING POSTGRES ENVIRONMENT"
REVISION = "0123456789abcdef0123456789abcdef01234567"
REHEARSAL_GATE = "DITTO_ANSIBLE_REHEARSAL"

FREEZE_GATE = "Freeze the removal gate once, neutralising templates and loops"
INCLUDE = "Remove the native PostgreSQL environment copies only when explicitly enabled"
EXPLAIN = "Explain dormant native PostgreSQL environment removal"
RAW_GATE = "Require the raw enabled flag to be a boolean true inside the include"
GUARD_MARKER = "Require the guarded entry point marker, an accident guard only"
OPERATION = "postgres-environment-remove"
MARKER_ENV = "DITTO_CODING_HOSTED_GUARDED_RUN"
SPEC_PATH = ROOT / f"infra/ansible/guarded-runs/{OPERATION}.json"
PRESET = "Refuse preset registered results and undocumented role inputs"
FREEZE_INPUTS = "Freeze the removal inputs once, neutralising templates and loops"
IDENTITY = "Probe this machine's identity into a result extra vars cannot preset"
HOST = "Require the exact host, source and removal confirmation"
LISTING = "List live worker and custody units"
LIVE = "Refuse to remove credentials unless every listed unit is inactive or failed"
PARENT_STAT = "Inspect the reader directories without following links"
PARENT_CHECK = "Refuse a linked, non-directory, foreign-owned or shared-writable parent"
COPY_STAT = "Inspect both exact copies as link metadata only"
COPY_CHECK = (
    "Refuse anything but an absent copy or a regular single-link copy owned by its "
    "reader"
)
REMOVAL = "Unlink the present copies and report any partial removal"
RECORD = "Record which copies the module removed and which vanished before removal"
VANISHED = "Refuse if a copy present before removal vanished instead of being removed"
UNLINK = (
    "Unlink each exact copy that exists through a pinned directory and nothing else"
)
PARTIAL = "Report the copies unlinked before the removal failure and stop"
RELIST = "Re-list live worker and custody units after removal"
LIVE_AFTER = "Refuse if any unit became active during removal"
AFTER_STAT = "Reinspect both exact paths without following links"
AFTER_CHECK = "Require both exact copies to be absent"
REPORT = "Report only the source revision and exact paths removed or already absent"

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


def _main() -> list[dict]:
    return yaml.safe_load(MAIN)


def _remove() -> list[dict]:
    return yaml.safe_load(REMOVE)


def _walk(tasks: list[dict]) -> Iterator[dict]:
    for task in tasks:
        yield task
        for section in ("block", "rescue", "always"):
            yield from _walk(task.get(section, []))


def _task(name: str, tasks: list[dict] | None = None) -> dict:
    (task,) = [task for task in _walk(tasks or _remove()) if task["name"] == name]
    return task


def _module(task: dict) -> str:
    (module,) = set(task) - {
        "name",
        "loop",
        "loop_control",
        "register",
        "when",
        "changed_when",
        "check_mode",
        "rescue",
        "no_log",
    }
    return module


def _flat(text: object) -> str:
    return " ".join(str(text).split())


def _messages(task: dict) -> list[str]:
    return [
        _flat(arguments[key])
        for arguments in task.values()
        if isinstance(arguments, dict)
        for key in ("fail_msg", "msg")
        if key in arguments
    ]


def _live_check(task: dict) -> str:
    (that,) = task["ansible.builtin.assert"]["that"]
    return _flat(that)


def _expected_live_check(register: str) -> str:
    return _flat(
        f"{register}.stdout_lines | reject('match', '{UNIT_PATTERN}') "
        "| list | length == 0"
    )


def _names(tasks: list[dict]) -> set[str]:
    """Every variable name a task file creates by register or set_fact."""
    names = set()
    for task in _walk(tasks):
        if "register" in task:
            names.add(task["register"])
        names.update(task.get("ansible.builtin.set_fact", {}))
    return names


# --------------------------------------------------------------------------- #
# Structural tests                                                            #
# --------------------------------------------------------------------------- #


def test_gate_is_frozen_once_behind_a_dynamic_include() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults == INPUTS
    main = _main()
    # A no_log freeze, a dynamic include gated on the frozen fact, and the dormant
    # explanation. No block and no per-task gate left to re-evaluate.
    assert [task["name"] for task in main] == [FREEZE_GATE, INCLUDE, EXPLAIN]
    freeze, include, explain = main
    assert freeze["no_log"] is True
    assert freeze["ansible.builtin.set_fact"] == {
        FROZEN_GATE: (
            f"{{{{ ({PREFIX}enabled | default(false, true)) is sameas true }}}}"
        )
    }
    assert include["ansible.builtin.include_tasks"] == "remove.yml"
    assert include["when"] == GATE
    assert explain["when"] == f"not ({GATE})"
    assert "no credential file is removed" in explain["ansible.builtin.debug"]["msg"]
    # A static import would defeat --start-at-task; a block gate is re-evaluated.
    assert "import_tasks" not in PARSED_MAIN and "block" not in PARSED_MAIN
    # The raw flag is rendered only by the freeze.
    assert [t["name"] for t in main if f"{PREFIX}enabled" in json.dumps(t)] == [
        FREEZE_GATE
    ]
    (play,) = yaml.safe_load(PLAYBOOK.read_text())
    assert play["gather_facts"] is False


def test_no_bool_filter_can_print_a_coerced_value() -> None:
    # ansible-core 2.21 prints any non-boolean string the bool filter coerces in
    # a deprecation warning, even under no_log, so neither file uses it.
    for text in (PARSED_MAIN, PARSED):
        assert not re.search(r"\|\s*bool\b", text)
    assert "sameas true" in PARSED_MAIN


def test_preset_refusal_runs_first_and_is_the_single_source_of_truth() -> None:
    tasks = _remove()
    # The raw-gate refusal leads; the preset guard is next, still before any
    # register or set_fact.
    assert [t["name"] for t in tasks[:3]] == [RAW_GATE, GUARD_MARKER, PRESET]
    raw = _task(RAW_GATE)
    assert raw["no_log"] is True
    assert raw["ansible.builtin.assert"]["that"] == [
        f"({PREFIX}enabled | default(false, true)) is sameas true"
    ]
    assert not any(
        "register" in task or "ansible.builtin.set_fact" in task
        for task in _walk(tasks[: tasks.index(_task(PRESET))])
    )
    that, item = _task(PRESET)["ansible.builtin.assert"]["that"]
    assert _flat(that) == _flat(
        f"lookup('ansible.builtin.varnames', '^{PREFIX}', wantlist=True) | sort == "
        "[" + ", ".join(f"'{name}'" for name in ALLOWED_AT_GUARD) + "]"
    )
    # The loop item is refused by name; varnames never renders a value.
    assert item == "lookup('ansible.builtin.varnames', '^item$', wantlist=True) == []"
    assert "is defined" not in PARSED + PARSED_MAIN
    # Main freezes only the gate before the guard; everything remove.yml creates
    # is prefixed and absent from the allow-list, so presetting it is refused.
    assert _names(_main()) == {FROZEN_GATE}
    created = _names(tasks)
    assert created == {
        FROZEN_CONFIRMATION,
        FROZEN_REVISION,
        f"{PREFIX}identity",
        f"{PREFIX}units",
        f"{PREFIX}directories",
        f"{PREFIX}copies",
        f"{PREFIX}unlinked",
        f"{PREFIX}removed",
        f"{PREFIX}vanished",
        f"{PREFIX}units_after_removal",
        f"{PREFIX}after",
    }
    assert all(name.startswith(PREFIX) for name in created)
    assert not created & set(ALLOWED_AT_GUARD)
    message = _task(PRESET)["ansible.builtin.assert"]["fail_msg"]
    assert "{{" not in message and "Nothing was removed" in message


def test_prefix_patterns_stay_distinct_from_the_materialization_role() -> None:
    cleanup_names = set(INPUTS) | _names(_main()) | _names(_remove())
    materialize_defaults = yaml.safe_load(
        (MATERIALIZE_ROLE / "defaults/main.yml").read_text()
    )
    materialize_main = yaml.safe_load((MATERIALIZE_ROLE / "tasks/main.yml").read_text())
    materialize_names = (
        set(materialize_defaults) | _names(materialize_main) | _names(MATERIALIZE)
    )
    assert materialize_names and cleanup_names
    # This role's guard pattern never matches a materialization name, and every
    # name this role creates is one the materialization guard excludes.
    cleanup_pattern = re.compile(f"^{PREFIX}")
    assert not any(cleanup_pattern.match(name) for name in materialize_names)
    assert all(cleanup_pattern.match(name) for name in cleanup_names)
    materialize_that, materialize_item = _task(MATERIALIZE_PRESET, MATERIALIZE)[
        "ansible.builtin.assert"
    ]["that"]
    # Both roles refuse a preset loop item by name.
    assert materialize_item == _task(PRESET)["ansible.builtin.assert"]["that"][1]
    lookup, exclusion = re.findall(r"'(\^[a-z_]+)'", _flat(materialize_that))[:2]
    assert lookup == f"^{MATERIALIZE_PREFIX}"
    assert exclusion == f"^{PREFIX}"
    assert all(re.match(lookup, name) for name in cleanup_names)
    assert all(re.match(exclusion, name) for name in cleanup_names)
    assert not any(re.match(exclusion, name) for name in materialize_names)


def test_inputs_are_frozen_once_and_never_rendered_into_messages() -> None:
    freeze = _task(FREEZE_INPUTS)
    assert freeze["no_log"] is True
    assert freeze["ansible.builtin.set_fact"] == {
        FROZEN_CONFIRMATION: f"{{{{ {PREFIX}confirmation | default('', true) }}}}",
        FROZEN_REVISION: f"{{{{ {PREFIX}source_revision | default('', true) }}}}",
    }
    tasks = _remove()
    assert [task["name"] for task in tasks[:6]] == [
        RAW_GATE,
        GUARD_MARKER,
        PRESET,
        FREEZE_INPUTS,
        IDENTITY,
        HOST,
    ]
    after_freeze = tasks[tasks.index(freeze) + 1 :]
    for raw in INPUTS:
        offenders = [
            task["name"]
            for task in _walk(after_freeze)
            if re.search(rf"\b{raw}\b", json.dumps(task))
        ]
        assert offenders == [], (raw, offenders)
    # Every refusal is fixed text. Only the partial-removal failure and the final
    # report render values, and only the frozen revision the host check validated,
    # registered results and check mode.
    host_index = tasks.index(_task(HOST))
    for task in _walk(tasks):
        if "ansible.builtin.assert" in task:
            message = task["ansible.builtin.assert"]["fail_msg"]
            assert "{{" not in message, task["name"]
    for task in _walk(tasks[: host_index + 1]):
        assert all("{{" not in message for message in _messages(task)), task["name"]
    assert [
        task["name"]
        for task in _walk(tasks)
        if any("{{" in message for message in _messages(task))
    ] == [PARTIAL, REPORT]
    allowed = {
        FROZEN_REVISION,
        f"{PREFIX}unlinked",
        f"{PREFIX}copies",
        f"{PREFIX}removed",
        "ansible_check_mode",
    }
    for task in _walk(tasks[host_index + 1 :]):
        for message in _messages(task):
            referenced = set(
                re.findall(r"\b(coding_hosted_\w+|ansible_\w+)\b", message)
            )
            assert referenced <= allowed, (task["name"], referenced - allowed)


def test_probed_host_check_matches_the_materialization_literals() -> None:
    identity = _task(IDENTITY)
    assert identity["ansible.builtin.setup"] == {
        "gather_subset": ["!all", "!min", "platform", "distribution"]
    }
    assert identity["register"] == f"{PREFIX}identity"
    # Same literals as the materialization host check, both read from a
    # registered probe: a -e ansible_facts value replaces gathered facts.
    materialize_host = _task(MATERIALIZE_HOST, MATERIALIZE)
    inventory, hosts_all, batch, *facts = materialize_host["ansible.builtin.assert"][
        "that"
    ][:7]
    # inventory_hostname and group_names are host variables extra vars override;
    # groups, ansible_play_hosts_all and ansible_play_batch are magic variables
    # they cannot, so both roles pin the group and the exact reviewed host.
    assert inventory == (
        "ansible_play_hosts_all | difference(groups.get('role_coding_hosted', [])) "
        "| length == 0"
    )
    # Both roles pin the play and the batch: with serial: 1 the batch alone is
    # the reviewed host while a rogue host is still in the play.
    assert hosts_all == "ansible_play_hosts_all == ['ditto-coding-hosted-v2']"
    assert batch == "ansible_play_batch == ['ditto-coding-hosted-v2']"
    probed = []
    for line in facts:
        match = re.fullmatch(
            r"coding_hosted_postgres_environment_identity\.ansible_facts\."
            r"ansible_(\w+) == '([^']+)'",
            line,
        )
        assert match, line
        probed.append(
            f"{PREFIX}identity.ansible_facts.ansible_{match[1]} == '{match[2]}'"
        )
    assert _task(HOST)["ansible.builtin.assert"]["that"] == [
        inventory,
        hosts_all,
        batch,
        *probed,
        f"{FROZEN_REVISION} is string",
        f"{FROZEN_REVISION} is match('^[0-9a-f]{{40}}$')",
        f"{FROZEN_REVISION} | length == 40",
        f"{FROZEN_CONFIRMATION} == '{CONFIRMATION}'",
    ]
    hostname = "ansible_hostname == 'ditto-coding-hosted-v2'"
    assert f"{PREFIX}identity.ansible_facts.{hostname}" in probed
    assert PARSED.count("ansible_facts") == PARSED.count(
        f"{PREFIX}identity.ansible_facts"
    )
    # A '$' anchor alone accepts one trailing newline; the exact length refuses it.
    assert re.match("^[0-9a-f]{40}$", REVISION + "\n")
    assert len(REVISION + "\n") != 40


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


def test_live_unit_refusal_is_an_allow_list_over_the_materialization_listing() -> None:
    names = [task["name"] for task in _remove()]
    assert names[6:8] == [LISTING, LIVE]
    listing = _task(LISTING)
    original = _task(LISTING, MATERIALIZE)
    assert listing["ansible.builtin.command"] == original["ansible.builtin.command"]
    assert listing["ansible.builtin.command"]["argv"] == LISTING_ARGV
    assert listing["register"] == f"{PREFIX}units"
    assert listing["check_mode"] is False and listing["changed_when"] is False
    assert _live_check(_task(LIVE)) == _expected_live_check(f"{PREFIX}units")
    # Jinja decodes the doubled backslashes; Ansible's match test is re.match.
    regex = re.compile(UNIT_PATTERN.replace("\\\\", "\\"))
    for line in ALLOWED_UNIT_LINES:
        assert regex.match(line), line
    for line in REFUSED_UNIT_LINES:
        assert not regex.match(line), line
    # The listings are the only systemctl use; nothing is stopped or killed.
    assert PARSED.count("systemctl") == 2  # before and after removal
    for forbidden in ("systemd", "service:", "state: stopped", "kill", "is-active"):
        assert forbidden not in PARSED, forbidden


def test_unit_state_is_rechecked_after_removal() -> None:
    names = [task["name"] for task in _remove()]
    relist = _task(RELIST)
    assert relist["ansible.builtin.command"]["argv"] == LISTING_ARGV
    assert relist["register"] == f"{PREFIX}units_after_removal"
    assert relist["check_mode"] is False and relist["changed_when"] is False
    assert names.index(RELIST) == names.index(VANISHED) + 1
    assert names.index(VANISHED) == names.index(REMOVAL) + 2
    assert names.index(LIVE_AFTER) == names.index(RELIST) + 1
    assert _live_check(_task(LIVE_AFTER)) == _expected_live_check(
        f"{PREFIX}units_after_removal"
    )
    assert "mid-removal" in _task(LIVE_AFTER)["ansible.builtin.assert"]["fail_msg"]


def test_only_the_two_exact_copies_are_ever_removed() -> None:
    assert {_module(task) for task in _main()} == {
        "ansible.builtin.set_fact",
        "ansible.builtin.include_tasks",
        "ansible.builtin.debug",
    }
    tasks = list(_walk(_remove()))
    # No file, shell, copy, find or other module that could create, recurse or read.
    assert {_module(task) for task in tasks} == {
        "block",
        "ansible.builtin.assert",
        "ansible.builtin.command",
        "ansible.builtin.debug",
        "ansible.builtin.fail",
        "ansible.builtin.set_fact",
        "ansible.builtin.setup",
        "ansible.builtin.stat",
        MODULE_NAME,
    }
    commands = [task["name"] for task in tasks if "ansible.builtin.command" in task]
    assert commands == [LISTING, RELIST]
    assert sorted(path.name for path in (ROLE / "library").iterdir()) == [
        f"{MODULE_NAME}.py"
    ]
    unlink = _task(UNLINK)
    assert unlink[MODULE_NAME] == {
        "path": "{{ item.path }}",
        "owner": "{{ item.owner }}",
    }
    assert unlink["loop"] == COPY_ITEMS
    literals = set(re.findall(r"/var/lib/[^\s'\",\]}]*", REMOVE))
    assert literals == {*COPIES, *PARENTS}
    for forbidden in (
        "state: absent",
        "ansible.builtin.file",
        "rmtree",
        "rm ",
        "rmdir",
        "recurse",
        "find",
        "fileglob",
        "with_",
        "removes:",
        "*.json",
        "/usr/bin/unlink",
    ):
        assert forbidden not in PARSED, forbidden
    source = MODULE_PATH.read_text()
    for forbidden in (
        "shutil",
        "rmtree",
        "rmdir",
        "os.remove(",
        "subprocess",
        "open(path",
    ):
        assert forbidden not in source, forbidden


def _only(tasks: list[dict], module: str) -> dict:
    (task,) = [task for task in tasks if module in task]
    return task


def test_targets_and_owners_equal_the_materialization_write_loop() -> None:
    # The cleanup literals are copied from coding_hosted_postgres_environment;
    # parse that role's write module loop so any drift in a path or owner fails
    # here. The materialization role writes through its own pinned module now, so
    # the private directories are implicit in each copy path rather than a
    # separate file task.
    written = _only(MATERIALIZE, "coding_hosted_postgres_environment_write")["loop"]
    asserted = " ".join(
        line
        for task in MATERIALIZE
        if "ansible.builtin.assert" in task
        for line in task["ansible.builtin.assert"]["that"]
    )
    homes = dict(re.findall(r"getent_passwd\['([^']+)'\]\[4\] == '([^']+)'", asserted))
    assert set(homes) == set(OWNERS)
    assert [item["owner"] for item in written] == list(OWNERS)
    assert [item["path"] for item in written] == COPIES
    assert written == COPY_ITEMS

    assert _task(COPY_STAT)["loop"] == written
    assert _task(UNLINK)["loop"] == written
    assert _task(AFTER_STAT)["loop"] == [item["path"] for item in written]

    parents: list[dict] = []
    for item in written:
        private = str(Path(item["path"]).parent)
        assert str(Path(private).parent) == homes[item["owner"]]
        parents += [
            {"path": homes[item["owner"]], "owner": item["owner"]},
            {"path": private, "owner": item["owner"]},
        ]
    assert _task(PARENT_STAT)["loop"] == parents == PARENT_ITEMS


def test_lstat_safety_checks_precede_removal_and_never_read_contents() -> None:
    assert [task["name"] for task in _remove()] == [
        RAW_GATE,
        GUARD_MARKER,
        PRESET,
        FREEZE_INPUTS,
        IDENTITY,
        HOST,
        LISTING,
        LIVE,
        PARENT_STAT,
        PARENT_CHECK,
        COPY_STAT,
        COPY_CHECK,
        REMOVAL,
        RECORD,
        VANISHED,
        RELIST,
        LIVE_AFTER,
        AFTER_STAT,
        AFTER_CHECK,
        REPORT,
    ]
    for name in (PARENT_STAT, COPY_STAT, AFTER_STAT):
        arguments = _task(name)["ansible.builtin.stat"]
        assert arguments["follow"] is False
        assert arguments["get_checksum"] is False
        assert arguments["get_mime"] is False
        assert arguments["get_attributes"] is False
    (parent,) = _task(PARENT_CHECK)["ansible.builtin.assert"]["that"]
    assert _flat(parent) == (
        "not item.stat.exists or (item.stat.isdir and not item.stat.islnk and "
        "item.stat.pw_name | default('') == item.item.owner and "
        "not item.stat.wgrp and not item.stat.woth)"
    )
    (sealed,) = _task(COPY_CHECK)["ansible.builtin.assert"]["that"]
    assert _flat(sealed) == (
        "not item.stat.exists or (item.stat.isreg and not item.stat.islnk and "
        "item.stat.nlink == 1 and item.stat.pw_name | default('') == item.item.owner)"
    )
    # Removal is limited to the copies whose lstat result proved them present.
    assert _flat(_task(UNLINK)["when"]) == (
        f"item.path in ({PREFIX}copies.results | "
        "selectattr('stat.exists') | map(attribute='item.path') | list)"
    )
    assert _task(AFTER_CHECK)["ansible.builtin.assert"]["that"] == [
        "not item.stat.exists"
    ]
    for forbidden in (
        "slurp",
        "fetch",
        "checksum: true",
        "content",
        "DITTO_CODING_PG_PASSWORD",
        "coding_hosted_postgres_environment_password",
        "gcloud",
        "set -x",
    ):
        assert forbidden not in PARSED + PARSED_MAIN, forbidden
    # The only lookups list variable names, plus the guard marker's single
    # environment read; nothing reads a secret, a file or a pipe.
    assert PARSED.count("lookup(") == 3
    assert PARSED.count("lookup('ansible.builtin.env', ") == 1
    assert f"lookup('ansible.builtin.env', '{MARKER_ENV}')" in PARSED
    assert PARSED.count("lookup('ansible.builtin.varnames'") == 2
    assert "lookup(" not in PARSED_MAIN


def test_removal_failure_reports_partial_progress_and_still_fails() -> None:
    removal = _task(REMOVAL)
    assert set(removal) == {"name", "block", "rescue"}
    assert [task["name"] for task in removal["block"]] == [UNLINK]
    assert [task["name"] for task in removal["rescue"]] == [PARTIAL]
    unlinked = f"{PREFIX}unlinked"
    assert _task(UNLINK)["register"] == unlinked
    succeeded = (
        f"{unlinked}.results | default([]) | selectattr('state', 'defined') | "
        "selectattr('state', 'equalto', 'removed') | map(attribute='item.path')"
    )
    # ansible.builtin.fail re-raises: a rescued removal failure still fails the host.
    assert _flat(_task(PARTIAL)["ansible.builtin.fail"]["msg"]) == _flat(
        "Native Coding PostgreSQL environment cleanup failed during removal; "
        f"{REVISION_MESSAGE}; "
        f"removed={{{{ {succeeded} | list | to_json }}}}; "
        f"not_removed={{{{ {PREFIX}copies.results | selectattr('stat.exists') | "
        f"map(attribute='item.path') | reject('in', {succeeded} | list) "
        "| list | to_json }}; "
        "reinspect both paths and reconcile by hand before re-running."
    )


_JINJA_WORDS = {"and", "or", "not", "in", "is", "if", "else", "true", "false", "none"}
_JINJA_WORDS |= {"True", "False", "None"}
# Magic variables extra vars cannot override on ansible-core 2.21.2, unlike host
# variables such as inventory_hostname and group_names.
NON_OVERRIDABLE_MAGIC = {
    "groups",
    "ansible_play_hosts_all",
    "ansible_play_batch",
    "ansible_check_mode",
}


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
    # variables such as inventory_hostname. Every variable these tasks read must
    # be a documented input, a prefixed name the preset guard refuses, the loop
    # item it also refuses, or a magic variable extra vars cannot override.
    tasks = _main() + _remove()
    created = _names(_main()) | _names(_remove())
    assert all(name.startswith(PREFIX) for name in created)
    for task in _walk(tasks):
        for root in _task_roots(task):
            assert (
                root in INPUTS
                or root in created
                or root in {"item", "lookup"}
                or root in NON_OVERRIDABLE_MAGIC
            ), (task["name"], root)
    assert _jinja_roots("inventory_hostname in groups.get('x', [])") == {
        "inventory_hostname",
        "groups",
    }
    for forbidden in ("inventory_hostname", "group_names"):
        assert forbidden not in PARSED + PARSED_MAIN, forbidden
    # No task handles the password, so no module invocation can log it.
    assert "DITTO_CODING_PG_PASSWORD" not in PARSED + PARSED_MAIN
    assert "password" not in _flat(MODULE_PATH.read_text()).lower()


def test_validated_revision_is_reported_after_the_host_check_only() -> None:
    for name in (PRESET, HOST):
        message = _flat(_task(name)["ansible.builtin.assert"]["fail_msg"])
        assert "source_revision=" not in message and "{{" not in message, name
    for name in (LIVE, PARENT_CHECK, COPY_CHECK, LIVE_AFTER, AFTER_CHECK):
        message = _flat(_task(name)["ansible.builtin.assert"]["fail_msg"])
        assert "{{" not in message and message.endswith("."), name
    assert REVISION_MESSAGE in _flat(_task(PARTIAL)["ansible.builtin.fail"]["msg"])
    report = _flat(_task(REPORT)["ansible.builtin.debug"]["msg"])
    # removed=/would_remove= come from the module's returned state, not the
    # pre-unlink stat, so a copy that survived under a renamed parent is never
    # claimed removed.
    expected = (
        "Native Coding PostgreSQL environment cleanup; "
        + REVISION_MESSAGE
        + "; {{ 'would_remove' if ansible_check_mode else 'removed' }}={{ "
        + PREFIX
        + "removed | sort | to_json }}; already_absent={{ "
        + PREFIX
        + "copies.results | map(attribute='item.path') | reject('in', "
        + PREFIX
        + "removed) | list | to_json }}; "
        "directories_kept=true; services_stopped=false; password_read=false."
    )
    assert report == _flat(expected)
    # removed= comes from the module's 'removed' state in a real run and from the
    # pre-unlink stat only in check mode; vanished from the module's 'absent'
    # state, and is empty in check mode.
    facts = _task(RECORD)["ansible.builtin.set_fact"]
    removed_expr = _flat(facts[f"{PREFIX}removed"])
    assert "if ansible_check_mode" in removed_expr
    assert "selectattr('state', 'equalto', 'removed')" in removed_expr
    assert "selectattr('state', 'equalto', 'absent')" in _flat(
        facts[f"{PREFIX}vanished"]
    )
    (vanished,) = _task(VANISHED)["ansible.builtin.assert"]["that"]
    assert vanished == f"{PREFIX}vanished | length == 0"


def test_playbook_fixture_ci_and_docs_registration() -> None:
    (play,) = yaml.safe_load(PLAYBOOK.read_text())
    assert play["hosts"] == "role_coding_hosted"
    assert play["become"] is True and play["gather_facts"] is False
    assert play["roles"] == ["coding_hosted_postgres_environment_cleanup"]
    assert set(play) == {"name", "hosts", "become", "gather_facts", "roles"}
    (fixture,) = yaml.safe_load(
        (
            ROOT / "infra/ansible/tests/coding-hosted-postgres-environment-cleanup.yml"
        ).read_text()
    )
    assert fixture["hosts"] == "localhost" and fixture["connection"] == "local"
    assert fixture["become"] is False and fixture["gather_facts"] is False
    assert fixture["roles"] == ["coding_hosted_postgres_environment_cleanup"]
    workflow = (ROOT / ".github/workflows/infra-ci.yml").read_text()
    assert "playbooks/gcp-coding-hosted-postgres-environment-cleanup.yml" in workflow
    assert "tests/coding-hosted-postgres-environment-cleanup.yml" in workflow
    section = _removal_docs()
    assert CONFIRMATION in section
    assert "platform-db-password" in section
    assert "does not rotate" in section
    assert "`inactive` or `failed`" in section
    assert "`refreshing`" in section
    assert "extra vars" in section


def test_guarded_entry_point_spec_and_marker() -> None:
    # The supported entry point is infra/scripts/coding-hosted-guarded-run.py
    # with this spec; the marker is an accident guard inside the dynamic include.
    marker = _task(GUARD_MARKER)
    assert marker["ansible.builtin.assert"]["that"] == [
        f"lookup('ansible.builtin.env', '{MARKER_ENV}') == '{OPERATION}'"
    ]
    assert marker["ansible.builtin.assert"]["quiet"] is True
    assert "Nothing was removed" in marker["ansible.builtin.assert"]["fail_msg"]
    assert "{{" not in marker["ansible.builtin.assert"]["fail_msg"]
    assert MARKER_ENV not in MAIN
    assert json.loads(SPEC_PATH.read_text()) == {
        "schema": "ditto-coding-hosted-guarded-run/v1",
        "operation": OPERATION,
        "playbook": "playbooks/gcp-coding-hosted-postgres-environment-cleanup.yml",
        "limit": "ditto-coding-hosted-v2",
        "enabled_var": f"{PREFIX}enabled",
        "confirmation_var": f"{PREFIX}confirmation",
        "confirmation": CONFIRMATION,
        "revision_var": f"{PREFIX}source_revision",
        "nonsecret_env_vars": [],
        "secret_env": [],
        "distinct_secret_values": False,
        # Removal needs no secret: an exported password or host is refused.
        "forbidden_env": ["DITTO_CODING_PG_PASSWORD", "DITTO_CODING_PG_HOST"],
        "forbidden_env_prefixes": [],
    }
    playbook = PLAYBOOK.read_text()
    assert "infra/scripts/coding-hosted-guarded-run.py" in playbook
    assert OPERATION in playbook
    assert "-e '" not in playbook


def _removal_docs() -> str:
    docs = (ROOT / "infra/docs/coding-hosted-postgres-v2.md").read_text()
    return _flat(docs.split("## Removal and rotation", 1)[1])


def test_docs_describe_every_removal_bypass_guard_and_residual() -> None:
    section = _removal_docs()
    for phrase in (
        "frozen once",
        "dynamic `include_tasks`",
        "`--start-at-task`",
        "`is sameas true`",
        "deprecation warning",
        "`default(..., true)`",
        "never renders an input",
        "every refusal is fixed text",
        "`ansible_play_hosts_all`",
        "`ansible_inject_invocation`",
        "ansible_play_batch",
        "`serial: 1`",
        "~/.ansible/tmp",
        "raw inside the include",
        "returned state, never the pre-unlink stat",
        "`O_NOFOLLOW`",
        "`unlinkat`",
        "owned by its reader",
        "re-checked after the unlink",
        "mid-removal",
        "`exception`",
        "lookup('file', lookup('env', 'DITTO_CODING_PG_PASSWORD'))",
        "`DITTO_ANSIBLE_REHEARSAL=1`",
        "SHA-1, MD5 and SHA-256",
        "infra/scripts/coding-hosted-guarded-run.py",
        OPERATION,
        "accident guard only",
        "unsupported",
    ):
        assert phrase in section, phrase


def test_docs_name_every_password_holder_and_order_rotation() -> None:
    section = _removal_docs()
    assert "does not revoke any credential" in section
    runtime = (
        ROOT / "apps/platform/ditto/api_server/coding_hosted_runtime.py"
    ).read_text()
    # The per-run copy the docs name is still written with the full entry list.
    assert '"postgres.json",' in runtime and "config.postgres_entries" in runtime
    plan_apply = (ROOT / ".github/workflows/infra-plan-apply.yml").read_text()
    assert "TF_VAR_db_password: ${{ secrets.PLATFORM_DB_PASSWORD }}" in plan_apply
    for holder in (
        *COPIES,
        "`<runtime_root>/postgres.json`",
        "`write_worker_config`",
        "retained evidence",
        "`PLATFORM_DB_PASSWORD`",
        "`infra-plan`",
        "`TF_VAR_db_password`",
        "`platform-db-password`",
        "gs://ditto-app-dev-tfstate/gcp-platform",
        "gs://ditto-app-dev-tfstate/ci-plans/gcp-platform/",
        "/opt/ditto/secrets/postgres-ditto.password",
        "`apps/platform/.env`",
        "`DITTO_PG_PASSWORD`",
        "`DITTO_CODING_PG_PASSWORD`",
    ):
        assert holder in section, holder
    steps = [
        "1. **Stop.**",
        "2. **Rotate the `ditto` password.**",
        "3. **Clean up.**",
        "4. **Re-materialize.**",
        "5. **Verify.**",
    ]
    positions = [section.index(step) for step in steps]
    assert positions == sorted(positions)
    assert "it is not complete without step 2" in section
    assert "must not be extended to" in section


def test_rehearsal_runs_only_in_the_infra_ansible_job() -> None:
    workflows = ROOT / ".github/workflows"
    infra = yaml.safe_load((workflows / "infra-ci.yml").read_text())
    this_file = str(Path(__file__).relative_to(ROOT))
    for trigger in ("pull_request", "push"):
        paths = infra[True][trigger]["paths"]
        assert this_file in paths
        assert "pyproject.toml" in paths and "uv.lock" in paths
    # The materialization rehearsal shares the gate, so select this file's step.
    (step,) = [
        step
        for job in infra["jobs"].values()
        for step in job["steps"]
        if REHEARSAL_GATE in step.get("env", {}) and this_file in step["run"]
    ]
    assert step in infra["jobs"]["ansible"]["steps"]
    assert step["env"] == {REHEARSAL_GATE: "1"}
    assert step["working-directory"] == "${{ github.workspace }}"
    # Lock-pinned pytest plugins; the locked dev group already brings PyYAML
    # 6.0.3, so no --with and no project install.
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
    # The job installs uv before any step that uses it.
    uses = [step.get("uses", "") for step in infra["jobs"]["ansible"]["steps"]]
    assert any(action.startswith("astral-sh/setup-uv@") for action in uses)
    for other in workflows.glob("*.yml"):
        if other.name != "infra-ci.yml":
            assert REHEARSAL_GATE not in other.read_text(), other.name


# --------------------------------------------------------------------------- #
# Directory-fd unlink module: exercised directly, without ansible-core        #
# --------------------------------------------------------------------------- #


def _unlink_module() -> Any:
    spec = importlib.util.spec_from_file_location(MODULE_NAME, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Never leave a __pycache__ beside the role's module.
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def _reader_tree(root: Path, home: str = "ditto-coding-custody") -> Path:
    """Create <root>/var/lib/<home>/private as 0700 directories; return private."""
    private = root / "var/lib" / home / "private"
    private.mkdir(parents=True)
    for directory in (private.parent, private):
        directory.chmod(0o700)
    return private


def _plant(private: Path, body: str = "[]") -> Path:
    target = private / "postgres-environment.json"
    target.write_text(body)
    target.chmod(0o600)
    return target


def test_unlink_module_removes_only_a_regular_owned_single_link_copy(tmp_path) -> None:
    module = _unlink_module()
    uid = os.getuid()
    private = _reader_tree(tmp_path.resolve())
    target = _plant(private)
    sibling = private / "custody-key.pem"
    sibling.write_text("key")
    path = str(target)
    assert module.remove_copy(path, uid, check_mode=True) == "would_remove"
    assert target.exists()
    assert module.remove_copy(path, uid) == "removed"
    assert not target.exists() and sibling.read_text() == "key"
    assert private.is_dir()
    assert module.remove_copy(path, uid) == "absent"
    shutil.rmtree(private.parent)
    assert module.remove_copy(path, uid) == "absent"


def test_unlink_module_refuses_links_directories_hard_links_and_foreign_owners(
    tmp_path,
) -> None:
    # Components are opened with O_NOFOLLOW, so the tree must not sit behind a link.
    tmp_path = tmp_path.resolve()
    module = _unlink_module()
    uid = os.getuid()
    outside = tmp_path / "outside.json"
    outside.write_text("outside")

    link = _reader_tree(tmp_path / "link") / "postgres-environment.json"
    link.symlink_to(outside)
    directory = _reader_tree(tmp_path / "directory") / "postgres-environment.json"
    directory.mkdir()
    hard = _plant(_reader_tree(tmp_path / "hard"))
    os.link(hard, hard.with_name("evidence-link.json"))
    foreign = _plant(_reader_tree(tmp_path / "foreign"))

    for path, owner_uid in (
        (link, uid),
        (directory, uid),
        (hard, uid),
        (foreign, uid + 1),
    ):
        with pytest.raises(module.UnsafeCopy):
            module.remove_copy(str(path), owner_uid)
    assert link.is_symlink() and outside.read_text() == "outside"
    assert directory.is_dir()
    assert hard.read_text() == "[]" and hard.stat().st_nlink == 2
    # uid + 1 does not own the home, so the refusal comes before the file check.
    assert foreign.read_text() == "[]"


def test_unlink_module_refuses_linked_foreign_or_shared_writable_parents(
    tmp_path,
) -> None:
    # Components are opened with O_NOFOLLOW, so the tree must not sit behind a link.
    tmp_path = tmp_path.resolve()
    module = _unlink_module()
    uid = os.getuid()
    elsewhere = _plant(_reader_tree(tmp_path / "elsewhere"))

    linked_private = tmp_path / "linked_private/var/lib/ditto-coding-custody"
    linked_private.mkdir(parents=True, mode=0o700)
    linked_private.chmod(0o700)
    (linked_private / "private").symlink_to(elsewhere.parent)

    linked_home = tmp_path / "linked_home/var/lib"
    linked_home.mkdir(parents=True)
    (linked_home / "ditto-coding-custody").symlink_to(elsewhere.parent.parent)

    writable = _reader_tree(tmp_path / "writable")
    _plant(writable)
    writable.chmod(0o770)

    file_parent = tmp_path / "file_parent/var/lib/ditto-coding-custody"
    file_parent.mkdir(parents=True)
    file_parent.chmod(0o700)
    (file_parent / "private").write_text("not a directory")

    cases = [
        (linked_private / "private/postgres-environment.json", uid),
        (linked_home / "ditto-coding-custody/private/postgres-environment.json", uid),
        (writable / "postgres-environment.json", uid),
        (file_parent / "private/postgres-environment.json", uid),
        (elsewhere, uid + 1),
    ]
    for path, owner_uid in cases:
        with pytest.raises(module.UnsafeCopy):
            module.remove_copy(str(path), owner_uid)
    assert elsewhere.read_text() == "[]"
    assert (writable / "postgres-environment.json").read_text() == "[]"


def test_unlink_module_checks_the_owner_and_mode_of_both_reader_directories(
    tmp_path, monkeypatch
) -> None:
    # An unprivileged test cannot chown, so report another owner through fstat
    # for exactly one pinned directory at a time: the home, then private.
    tmp_path = tmp_path.resolve()
    module = _unlink_module()
    uid = os.getuid()
    target = _plant(_reader_tree(tmp_path))
    real_fstat = os.fstat
    for foreign_call in (0, 1):
        calls: list[int] = []

        def fstat(
            fd: int, foreign_call: int = foreign_call, calls: list[int] = calls
        ) -> os.stat_result:
            info = real_fstat(fd)
            calls.append(fd)
            if len(calls) - 1 != foreign_call:
                return info
            fields = list(info)
            fields[stat.ST_UID] = uid + 1
            return os.stat_result(fields)

        monkeypatch.setattr(module.os, "fstat", fstat)
        with pytest.raises(module.UnsafeCopy, match="not owned by the reader"):
            module.remove_copy(str(target), uid)
        monkeypatch.setattr(module.os, "fstat", real_fstat)
        assert len(calls) == foreign_call + 1
    # A group-writable home is refused as well as a group-writable private.
    target.parent.parent.chmod(0o770)
    with pytest.raises(module.UnsafeCopy, match="reader home directory is writable"):
        module.remove_copy(str(target), uid)
    target.parent.parent.chmod(0o700)
    assert module.remove_copy(str(target), uid) == "removed"


def test_unlink_module_refuses_every_path_but_an_exact_copy() -> None:
    module = _unlink_module()
    for path in (
        "var/lib/ditto-coding-custody/private/postgres-environment.json",
        "/var/lib/ditto-coding-custody/private/../private/postgres-environment.json",
        "/var/lib//ditto-coding-custody/private/postgres-environment.json",
        "/var/lib/ditto-coding-custody/private/postgres-environment.json/",
        "/var/lib/ditto-coding-custody/keys/postgres-environment.json",
        "/var/lib/ditto-coding-custody/private/custody-key.pem",
        "/private/postgres-environment.json",
    ):
        with pytest.raises(module.UnsafeCopy):
            module.split_target(path)
    assert module.split_target(CUSTODY) == [
        "var",
        "lib",
        "ditto-coding-custody",
        "private",
    ]


def test_unlink_module_removes_through_the_pinned_directory_after_a_parent_swap(
    tmp_path,
) -> None:
    # Components are opened with O_NOFOLLOW, so the tree must not sit behind a link.
    tmp_path = tmp_path.resolve()
    module = _unlink_module()
    uid = os.getuid()
    private = _reader_tree(tmp_path)
    target = _plant(private)
    victim = _plant(_reader_tree(tmp_path / "victim", home="ditto-coding-hosted"))
    fd = module.open_private_directory(str(target), uid)
    try:
        # The reader swaps its private directory for a symlink to another
        # directory holding a same-named file after the parents were pinned.
        moved = private.with_name("private-moved")
        private.rename(moved)
        private.symlink_to(victim.parent)
        # A path-based unlink would now resolve into the victim directory.
        assert os.path.realpath(target) == str(victim)
        assert module.unlink_in(fd, uid) == "removed"
    finally:
        os.close(fd)
    assert victim.read_text() == "[]"
    assert not (moved / "postgres-environment.json").exists()


def test_unlink_module_reports_absent_when_the_private_dir_is_renamed_away(
    tmp_path,
) -> None:
    # The report and the vanished guard rely on this: if a reader renames private
    # away after the copy was seen present, the module opens no directory and
    # returns "absent", so the report never claims a removal that did not happen.
    tmp_path = tmp_path.resolve()
    module = _unlink_module()
    uid = os.getuid()
    private = _reader_tree(tmp_path)
    target = _plant(private)
    private.rename(private.with_name("private-moved"))
    assert module.remove_copy(str(target), uid) == "absent"
    assert (
        tmp_path
        / "var/lib/ditto-coding-custody/private-moved/postgres-environment.json"
    ).read_text() == "[]"


def test_unlink_module_never_removes_a_directory_or_link_target_swapped_in_late(
    tmp_path, monkeypatch
) -> None:
    # Components are opened with O_NOFOLLOW, so the tree must not sit behind a link.
    tmp_path = tmp_path.resolve()
    module = _unlink_module()
    uid = os.getuid()
    outside = tmp_path / "outside.json"
    outside.write_text("outside")
    real_lstat = module.lstat_entry

    def swap_after_inspection(replacement: str):
        def inspect(dir_fd: int) -> os.stat_result:
            info = real_lstat(dir_fd)
            os.unlink("postgres-environment.json", dir_fd=dir_fd)
            if replacement == "directory":
                os.mkdir("postgres-environment.json", dir_fd=dir_fd)
            else:
                os.symlink(outside, "postgres-environment.json", dir_fd=dir_fd)
            monkeypatch.setattr(module, "lstat_entry", real_lstat)
            return info

        return inspect

    directory_case = _plant(_reader_tree(tmp_path / "directory"))
    monkeypatch.setattr(module, "lstat_entry", swap_after_inspection("directory"))
    with pytest.raises(module.UnsafeCopy):
        module.remove_copy(str(directory_case), uid)
    assert directory_case.is_dir()

    link_case = _plant(_reader_tree(tmp_path / "link"))
    monkeypatch.setattr(module, "lstat_entry", swap_after_inspection("symlink"))
    # unlinkat removes the reader's own symlink entry, never the file it names.
    assert module.remove_copy(str(link_case), uid) == "removed"
    assert not link_case.is_symlink() and outside.read_text() == "outside"


def test_unlink_module_source_pins_directories_and_never_follows_links() -> None:
    source = _flat(MODULE_PATH.read_text())
    assert (
        "_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC"
        in source
    )
    assert "os.open(component, _DIRECTORY_FLAGS, dir_fd=fd)" in source
    assert "os.unlink(NAME, dir_fd=dir_fd)" in source
    assert "os.stat(NAME, dir_fd=dir_fd, follow_symlinks=False)" in source
    assert "supports_check_mode=True" in source


# --------------------------------------------------------------------------- #
# Local rehearsal (gated): run the real role, main.yml, remove.yml and its     #
# module, through ansible-core 2.21.2 against temporary trees, under the       #
# repo's ansible.cfg and -v --diff, and prove no forged input removes a copy   #
# or leaks the controller password.                                            #
# --------------------------------------------------------------------------- #

# A stand-in, never a real credential. It holds characters JSON, YAML and repr
# escape, plus a canary no escaping changes, so every printed form is found.
CANARY = "Kq7vCanaryZ3w9"
REHEARSAL_PASSWORD = f'rehearsal-only "stand-in"=pass\\word/42 {CANARY}'
PASSWORD_LOOKUP = 'lookup("env", "DITTO_CODING_PG_PASSWORD")'
STOPPED_UNITS = (
    "ditto-coding-hosted-worker.service loaded failed failed Worker\n"
    "ditto-coding-custody@0.service loaded inactive dead Custody\n"
    "ditto-coding-custody@1.service not-found inactive dead ditto-coding-custody@1\n"
)
LIVE_UNITS = {
    "live_custody": "ditto-coding-custody@0.service loaded active running Custody",
    "live_worker": (
        "ditto-coding-hosted-worker.service loaded deactivating stop-sigterm Worker"
    ),
    "activating": "ditto-coding-hosted-worker.service loaded activating start Worker",
    "reloading": "ditto-coding-custody@0.service loaded reloading reload Custody",
    "refreshing": (
        "ditto-coding-custody@2.service loaded refreshing refresh-extensions Custody"
    ),
    "maintenance": "ditto-coding-custody@3.service loaded maintenance cleaning Custody",
    "unknown_state": "ditto-coding-hosted-worker.service loaded quiescent idle Worker",
    "unparseable": "● ditto-coding-custody@0.service loaded inactive dead Custody",
}

# The rehearsal needs uvx, network access and coreutils, so root pytest shards
# skip it and run only the tests above. The infra-ci Ansible job sets the gate;
# with it set nothing else can skip, so a missing tool fails.
rehearsal = pytest.mark.skipif(
    os.environ.get(REHEARSAL_GATE) != "1",
    reason=f"set {REHEARSAL_GATE}=1 to run the ansible-core rehearsal",
)


def _document() -> str:
    """The bytes the materialization role writes, with the stand-in password."""
    return json.dumps(
        [
            "POSTGRES_HOST=10.30.0.5",
            "POSTGRES_PORT=5432",
            "POSTGRES_USER=ditto",
            "POSTGRES_DB=ditto_platform_prod",
            "POSTGRES_COMMAND_TIMEOUT=30",
            "POSTGRES_POOL_MIN_SIZE=1",
            "POSTGRES_POOL_MAX_SIZE=4",
            f"POSTGRES_PASSWORD={REHEARSAL_PASSWORD}",
        ]
    )


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


def _rewrite(node: Any, *, local_identity: bool) -> Any:
    """Point the role at a temporary tree: paths, owners, unit listings and,
    unless forged identity is under test, the identity comparison."""
    if isinstance(node, dict):
        out = {
            key: (
                value
                if key == "ansible.builtin.assert"
                else _rewrite(value, local_identity=local_identity)
            )
            for key, value in node.items()
        }
        command = out.get("ansible.builtin.command")
        if command and command.get("argv", [None])[0] == "/usr/bin/systemctl":
            units = (
                "{{ rehearsal_units_after_removal | default(rehearsal_units) }}"
                if out.get("register") == f"{PREFIX}units_after_removal"
                else "{{ rehearsal_units }}"
            )
            out["ansible.builtin.command"] = {"argv": ["/usr/bin/printf", "%s", units]}
        if out.get("name") == REPORT:
            out["register"] = "rehearsal_report"
        if "ansible.builtin.assert" in out and local_identity:
            that = [
                re.sub(
                    rf"^({PREFIX}identity\.ansible_facts\.(ansible_\w+)) == '[^']+'$",
                    r"\1 == rehearsal_probe.ansible_facts.\2",
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
    if isinstance(node, str) and node in OWNERS:
        return "{{ rehearsal_owners['" + node + "'] }}"
    if isinstance(node, str):
        return node.replace("/var/lib/", "{{ rehearsal_root }}/var/lib/")
    return node


def _build_role(dst: Path, *, local_identity: bool) -> None:
    role = dst / "roles/coding_hosted_postgres_environment_cleanup"
    (role / "tasks").mkdir(parents=True)
    shutil.copytree(ROLE / "defaults", role / "defaults")
    shutil.copytree(
        ROLE / "library",
        role / "library",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (role / "tasks/main.yml").write_text(MAIN)
    tasks = _rewrite(copy.deepcopy(_remove()), local_identity=local_identity)
    rendered = json.dumps(tasks)
    assert rendered.count("/var/lib/") == rendered.count(
        "{{ rehearsal_root }}/var/lib/"
    )
    assert "systemctl" not in rendered
    assert not any(f'"{owner}"' in rendered for owner in OWNERS)
    assert _task(PRESET, tasks) == _task(PRESET)
    assert _task(FREEZE_INPUTS, tasks) == _task(FREEZE_INPUTS)
    (role / "tasks/remove.yml").write_text(yaml.safe_dump(tasks, sort_keys=False))


def _record(outcome: str, content: str) -> dict:
    # The harness never prints what it records, so output holds only the role's.
    return {
        "ansible.builtin.copy": {"dest": outcome, "content": content},
        "check_mode": False,
        "no_log": True,
        "diff": False,
    }


def _play(rehearsal_pass: str, serial: int | None = None) -> dict:
    outcome = "{{ rehearsal_root }}/outcome-" + rehearsal_pass + ".json"
    play: dict[str, Any] = {
        "name": f"Rehearse cleanup ({rehearsal_pass})",
        "hosts": "all",
        "strategy": "free",
        "gather_facts": False,
        "become": False,
        "vars": {"ansible_python_interpreter": "{{ ansible_playbook_python }}"},
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
                            "name": "coding_hosted_postgres_environment_cleanup"
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
                            "[ansible_failed_result.msg | default('')] + "
                            "(ansible_failed_result.results | default([]) "
                            "| selectattr('failed', 'defined') "
                            "| selectattr('failed') "
                            "| map(attribute='msg') | list)} | to_json }}",
                        ),
                    }
                ],
            },
        ],
    }
    if serial is not None:
        play["serial"] = serial
    return play


REVIEWED_HOST = "ditto-coding-hosted-v2"


def _run(
    tmp_path: Path,
    name: str,
    hosts: dict[str, dict],
    *flags: str,
    rehearsal_pass: str = "first",
    local_identity: bool = True,
    residual: str | None = None,
    outside: dict[str, dict] | None = None,
    host_name: str | None = None,
    serial: int | None = None,
    marker: str | None = OPERATION,
) -> str:
    work = tmp_path / name
    work.mkdir()
    _build_role(work, local_identity=local_identity)
    # A single in-group host is named for the reviewed host, so the play targets
    # exactly it and ansible_play_batch == [REVIEWED_HOST]; multi-host and
    # outside cases keep their names to prove the batch check refuses them.
    if host_name is not None:
        hosts = {host_name: next(iter(hosts.values()))}
    elif len(hosts) == 1 and not outside:
        hosts = {REVIEWED_HOST: next(iter(hosts.values()))}
    # Every rehearsal host is in the real group, so only identity can refuse it.
    inventory: dict[str, Any] = {
        "all": {"children": {"role_coding_hosted": {"hosts": hosts}}}
    }
    if outside:
        # Hosts outside role_coding_hosted that the rehearsal play still targets.
        inventory["all"]["hosts"] = outside
    (work / "inventory.yml").write_text(yaml.safe_dump(inventory))
    (work / "play.yml").write_text(
        yaml.safe_dump([_play(rehearsal_pass, serial)], sort_keys=False)
    )
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
        # The operator never exports it for cleanup; the rehearsal does, so the
        # lookup cases have a real value to try to exfiltrate.
        "DITTO_CODING_PG_PASSWORD": REHEARSAL_PASSWORD,
    }
    if marker is not None:
        environment[MARKER_ENV] = marker
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
    leaked = [
        line
        for line in output.splitlines()
        if any(form in line for form in _leak_forms())
    ]
    if residual is None:
        assert leaked == [], (name, [line[:160] for line in leaked])
    else:
        # The documented residual: ansible-core prints a raised templating error
        # through the task result's preserved exception field, even under no_log.
        assert leaked, name
        assert all(residual in line for line in leaked), (name, leaked)
    return output


def _outcome(root: Path, rehearsal_pass: str = "first") -> dict | None:
    path = root / f"outcome-{rehearsal_pass}.json"
    return json.loads(path.read_text()) if path.exists() else None


def _tree(root: Path) -> tuple[Path, Path]:
    custody = _plant(_reader_tree(root, "ditto-coding-custody"), _document())
    hosted = _plant(_reader_tree(root, "ditto-coding-hosted"), _document())
    (custody.parent / "custody-key.pem").write_text("key")
    (hosted.parent / "evidence-receipt.json").write_text("{}")
    return custody, hosted


def _kept(pair: tuple[Path, Path]) -> bool:
    return all(path.read_text() == _document() for path in pair)


def _paths(root: Path, items: list[str]) -> str:
    return json.dumps([f"{root}{item}" for item in items])


def _report(root: Path, verb: str, removed: list[str], absent: list[str]) -> str:
    return (
        f"Native Coding PostgreSQL environment cleanup; source_revision={REVISION}; "
        f"{verb}={_paths(root, removed)}; already_absent={_paths(root, absent)}; "
        "directories_kept=true; services_stopped=false; password_read=false."
    )


def _expected_refusal(task: str) -> str:
    message = _flat(_task(task)["ansible.builtin.assert"]["fail_msg"])
    return message.replace(f"{{{{ {FROZEN_REVISION} }}}}", REVISION)


def _assert_refused(root: Path, task: str, rehearsal_pass: str = "first") -> None:
    outcome = _outcome(root, rehearsal_pass)
    assert outcome is not None and outcome.get("task") == task, (root.name, outcome)
    assert _expected_refusal(task) in [_flat(item) for item in outcome["messages"]], (
        root.name,
        outcome,
    )


def _assert_dormant(root: Path, pair: tuple[Path, Path]) -> None:
    assert _outcome(root) == {"report": None}, root.name
    assert _kept(pair), root.name


def _hosts(roots: dict[str, Path], owners: dict[str, str]) -> dict[str, dict]:
    for root in roots.values():
        root.mkdir(parents=True)
    return {
        name: {
            "ansible_connection": "local",
            f"{PREFIX}enabled": True,
            f"{PREFIX}confirmation": CONFIRMATION,
            f"{PREFIX}source_revision": REVISION,
            "rehearsal_root": str(root),
            "rehearsal_units": STOPPED_UNITS,
            "rehearsal_owners": owners,
        }
        for name, root in roots.items()
    }


def _local_owners() -> dict[str, str]:
    return dict.fromkeys(OWNERS, pwd.getpwuid(os.getuid()).pw_name)


@rehearsal
def test_rehearsal_removes_only_the_exact_copies_and_refuses_unsafe_state(
    tmp_path,
) -> None:
    # Components are opened with O_NOFOLLOW, so the tree must not sit behind a link.
    tmp_path = tmp_path.resolve()
    # Permission bits drive the partial-removal case; root would bypass them.
    assert os.geteuid() != 0, "run the rehearsal unprivileged"
    owners = _local_owners()
    names = [
        "absent",
        "present",
        "no_units",
        *LIVE_UNITS,
        "started_during_removal",
        "symlink",
        "hardlink",
        "directory",
        "other_owner",
        "shared_parent",
        "linked_parent",
        "linked_home",
        "preset_result",
        "preset_frozen_input",
        "undocumented_input",
        "materialization_names",
        "preset_item",
        "partial",
    ]
    roots = {name: tmp_path / "hosts" / name for name in names}
    hosts = _hosts(roots, owners)
    hosts["no_units"]["rehearsal_units"] = ""
    for name, line in LIVE_UNITS.items():
        hosts[name]["rehearsal_units"] = STOPPED_UNITS + line + "\n"
    hosts["started_during_removal"]["rehearsal_units_after_removal"] = (
        STOPPED_UNITS + LIVE_UNITS["live_custody"] + "\n"
    )
    hosts["other_owner"]["rehearsal_owners"] = {
        **owners,
        "ditto-coding-hosted": "rehearsal-other-reader",
    }
    # Inventory values are refused exactly like extra vars.
    hosts["preset_result"][f"{PREFIX}copies"] = {"results": []}
    hosts["preset_frozen_input"][FROZEN_REVISION] = REVISION
    hosts["undocumented_input"][f"{PREFIX}paths"] = [str(tmp_path / "elsewhere")]
    hosts["preset_item"]["item"] = {
        "path": str(tmp_path / "elsewhere"),
        "owner": "root",
    }
    # The materialization role's inputs and result names never trip this guard.
    hosts["materialization_names"] |= {
        f"{MATERIALIZE_PREFIX}enabled": True,
        f"{MATERIALIZE_PREFIX}host": "10.30.0.5",
        f"{MATERIALIZE_PREFIX}units": {"stdout_lines": []},
    }

    present = _tree(roots["present"])
    no_units = _tree(roots["no_units"])
    materialization_names = _tree(roots["materialization_names"])
    started = _tree(roots["started_during_removal"])
    kept = {
        name: _tree(roots[name])
        for name in (
            *LIVE_UNITS,
            "other_owner",
            "shared_parent",
            "preset_result",
            "preset_frozen_input",
            "undocumented_input",
            "preset_item",
        )
    }
    kept["shared_parent"][1].parent.chmod(0o770)
    outside = roots["symlink"] / "outside/unrelated.json"
    outside.parent.mkdir(parents=True)
    outside.write_text("outside")
    symlink_custody = _reader_tree(roots["symlink"]) / "postgres-environment.json"
    symlink_custody.symlink_to(outside)
    symlink_hosted = _plant(
        _reader_tree(roots["symlink"], "ditto-coding-hosted"), _document()
    )
    _, linked = _tree(roots["hardlink"])
    second_link = linked.parent / "evidence-link.json"
    os.link(linked, second_link)
    directory = _reader_tree(roots["directory"], "ditto-coding-hosted") / (
        "postgres-environment.json"
    )
    directory.mkdir()
    (directory / "attempt-receipt.json").write_text("{}")
    parent_custody = _plant(_reader_tree(roots["linked_parent"]), _document())
    elsewhere = _plant(_reader_tree(roots["linked_parent"] / "elsewhere"), _document())
    hosted_home = _reader_tree(roots["linked_parent"], "ditto-coding-hosted").parent
    (hosted_home / "private").rmdir()
    (hosted_home / "private").symlink_to(elsewhere.parent)
    home_hosted = _plant(
        _reader_tree(roots["linked_home"], "ditto-coding-hosted"), _document()
    )
    real_home = _reader_tree(roots["linked_home"] / "real").parent
    home_copy = _plant(real_home / "private", _document())
    (roots["linked_home"] / "var/lib/ditto-coding-custody").symlink_to(real_home)
    partial = _tree(roots["partial"])
    # The custody unlink succeeds; the hosted unlink then fails with EACCES.
    partial[1].parent.chmod(0o500)

    try:
        # Each case runs as its own single-host play named for the reviewed host,
        # so the batch identity check holds while every setup above is preserved.
        for case, hostvars in hosts.items():
            _run(tmp_path, f"run_{case}", {case: hostvars})
    finally:
        partial[1].parent.chmod(0o700)
        kept["shared_parent"][1].parent.chmod(0o700)

    assert _outcome(roots["absent"]) == {
        "report": _report(roots["absent"], "removed", [], COPIES)
    }
    assert not (roots["absent"] / "var").exists()
    for name, pair in (
        ("present", present),
        ("no_units", no_units),
        ("materialization_names", materialization_names),
    ):
        assert _outcome(roots[name]) == {
            "report": _report(roots[name], "removed", COPIES, [])
        }
        for path in pair:
            assert not path.exists() and not path.is_symlink()
            assert path.parent.is_dir() and path.parent.stat().st_mode & 0o777 == 0o700
    assert (present[0].parent / "custody-key.pem").read_text() == "key"
    assert (present[1].parent / "evidence-receipt.json").read_text() == "{}"

    # A unit that started during removal is reported loudly after the unlink.
    _assert_refused(roots["started_during_removal"], LIVE_AFTER)
    assert not any(path.exists() for path in started)

    refusals = {
        **dict.fromkeys(LIVE_UNITS, LIVE),
        "symlink": COPY_CHECK,
        "hardlink": COPY_CHECK,
        "directory": COPY_CHECK,
        "other_owner": PARENT_CHECK,
        "shared_parent": PARENT_CHECK,
        "linked_parent": PARENT_CHECK,
        "linked_home": PARENT_CHECK,
        "preset_result": PRESET,
        "preset_frozen_input": PRESET,
        "undocumented_input": PRESET,
        "preset_item": PRESET,
    }
    for name, task in refusals.items():
        _assert_refused(roots[name], task)
    for pair in kept.values():
        assert _kept(pair)
    assert symlink_custody.is_symlink() and outside.read_text() == "outside"
    assert symlink_hosted.read_text() == _document()
    assert linked.read_text() == _document() and second_link.stat().st_nlink == 2
    assert (directory / "attempt-receipt.json").read_text() == "{}"
    assert parent_custody.read_text() == _document()
    assert elsewhere.read_text() == _document()
    assert home_hosted.read_text() == _document()
    assert home_copy.read_text() == _document()

    outcome = _outcome(roots["partial"])
    assert outcome is not None and outcome["task"] == PARTIAL, outcome
    assert _flat(outcome["messages"][0]) == (
        "Native Coding PostgreSQL environment cleanup failed during removal; "
        f"source_revision={REVISION}; "
        f"removed={_paths(roots['partial'], [CUSTODY])}; "
        f"not_removed={_paths(roots['partial'], [HOSTED])}; "
        "reinspect both paths and reconcile by hand before re-running."
    )
    assert not partial[0].exists() and partial[1].read_text() == _document()

    # Registered results persist for a whole playbook run, so the idempotent
    # re-run is a separate invocation, exactly as an operator would re-run it.
    _run(tmp_path, "second", {"present": hosts["present"]}, rehearsal_pass="second")
    assert _outcome(roots["present"], "second") == {
        "report": _report(roots["present"], "removed", [], COPIES)
    }


@rehearsal
def test_rehearsal_lazy_and_lookup_inputs_are_frozen_once_without_leaking(
    tmp_path,
) -> None:
    # Components are opened with O_NOFOLLOW, so the tree must not sit behind a link.
    tmp_path = tmp_path.resolve()
    owners = _local_owners()
    names = [
        "lazy_enabled",
        "enabled_password",
        "enabled_undefined_lookup",
        "enabled_string_true",
        "lazy_revision_refused",
        "lazy_revision_frozen",
        "revision_undefined_lookup",
        "revision_password",
        "confirmation_undefined_lookup",
        "revision_newline",
    ]
    roots = {name: tmp_path / "hosts" / name for name in names}
    hosts = _hosts(roots, owners)
    pairs = {name: _tree(root) for name, root in roots.items()}
    # A block-level gate was re-evaluated per task and loop item: this flag was
    # false for every guard and true inside the removal loops.
    hosts["lazy_enabled"][f"{PREFIX}enabled"] = "{{ item is defined }}"
    # A flag that renders to the password: the bool filter would print it in a
    # deprecation warning; sameas refuses it silently.
    hosts["enabled_password"][f"{PREFIX}enabled"] = f"{{{{ {PASSWORD_LOOKUP} }}}}"
    hosts["enabled_undefined_lookup"][f"{PREFIX}enabled"] = (
        f"{{{{ {{}}[{PASSWORD_LOOKUP}] }}}}"
    )
    hosts["enabled_string_true"][f"{PREFIX}enabled"] = "true"
    hosts["lazy_revision_refused"][f"{PREFIX}source_revision"] = (
        f"{{{{ '{REVISION}' if item is defined else 'not-a-revision' }}}}"
    )
    hosts["lazy_revision_frozen"][f"{PREFIX}source_revision"] = (
        f"{{{{ 'not-a-revision' if item is defined else '{REVISION}' }}}}"
    )
    # The confirmed leak: this revision printed the password three times.
    hosts["revision_undefined_lookup"][f"{PREFIX}source_revision"] = (
        f"{{{{ {{}}[{PASSWORD_LOOKUP}] }}}}"
    )
    hosts["revision_password"][f"{PREFIX}source_revision"] = (
        f"{{{{ {PASSWORD_LOOKUP} }}}}"
    )
    hosts["confirmation_undefined_lookup"][f"{PREFIX}confirmation"] = (
        f"{{{{ {{}}[{PASSWORD_LOOKUP}] }}}}"
    )
    hosts["revision_newline"][f"{PREFIX}source_revision"] = REVISION + "\n"

    for case, hostvars in hosts.items():
        _run(tmp_path, f"lazy_{case}", {case: hostvars})

    for name in (
        "lazy_enabled",
        "enabled_password",
        "enabled_undefined_lookup",
        "enabled_string_true",
    ):
        _assert_dormant(roots[name], pairs[name])
    # Frozen once with no loop item, the lazy revision stays the valid value.
    assert _outcome(roots["lazy_revision_frozen"]) == {
        "report": _report(roots["lazy_revision_frozen"], "removed", COPIES, [])
    }
    for name in (
        "lazy_revision_refused",
        "revision_undefined_lookup",
        "revision_password",
        "confirmation_undefined_lookup",
        "revision_newline",
    ):
        _assert_refused(roots[name], HOST)
        assert _kept(pairs[name]), name


@rehearsal
def test_rehearsal_extra_vars_cannot_preset_forge_or_lazily_open_the_gate(
    tmp_path,
) -> None:
    # Components are opened with O_NOFOLLOW, so the tree must not sit behind a link.
    tmp_path = tmp_path.resolve()
    owners = _local_owners()
    runs: list[tuple[str, dict[str, Any], str | None]] = [
        # The review's exact override, then one complete enough to satisfy the
        # unit allow-list if the preset guard were missing.
        ("units_stdout", {f"{PREFIX}units": {"stdout": ""}}, PRESET),
        ("units_lines", {f"{PREFIX}units": {"stdout": "", "stdout_lines": []}}, PRESET),
        # The confirmed lazy-gate repro; the removal must stay dormant.
        ("lazy_gate", {f"{PREFIX}enabled": "{{ item is defined }}"}, None),
        # The confirmed template-error leak through source_revision.
        (
            "lookup_revision",
            {f"{PREFIX}source_revision": f"{{{{ {{}}[{PASSWORD_LOOKUP}] }}}}"},
            HOST,
        ),
        # Presetting the frozen gate reached via -e is caught by the raw-enabled
        # assert inside the include, which re-checks the raw flag.
        ("forged_gate", {FROZEN_GATE: True}, RAW_GATE),
    ]
    for name, extra_vars, refused_at in runs:
        root = tmp_path / "hosts" / name
        hosts = _hosts({name: root}, owners)
        if name in ("units_stdout", "units_lines"):
            hosts[name]["rehearsal_units"] = LIVE_UNITS["live_custody"] + "\n"
        if name == "forged_gate":
            hosts[name][f"{PREFIX}enabled"] = False
        pair = _tree(root)
        _run(tmp_path, name, hosts, "-e", json.dumps(extra_vars))
        if refused_at is None:
            _assert_dormant(root, pair)
        else:
            _assert_refused(root, refused_at)
            assert _kept(pair), name

    # Forged facts that match the dedicated host must not pass the probed check.
    assert socket.gethostname().split(".")[0] != "ditto-coding-hosted-v2"
    identity = {
        "hostname": "ditto-coding-hosted-v2",
        "architecture": "x86_64",
        "distribution": "Debian",
        "distribution_major_version": "13",
    }
    forged_facts = {
        **identity,
        **{f"ansible_{key}": value for key, value in identity.items()},
    }
    root = tmp_path / "hosts/forged_host"
    hosts = _hosts({"forged_host": root}, owners)
    pair = _tree(root)
    _run(
        tmp_path,
        "identity",
        hosts,
        "-e",
        json.dumps({"ansible_facts": forged_facts}),
        local_identity=False,
    )
    _assert_refused(root, HOST)
    assert _kept(pair)


@rehearsal
def test_rehearsal_a_run_without_the_guard_marker_removes_nothing(tmp_path) -> None:
    # Components are opened with O_NOFOLLOW, so the tree must not sit behind a link.
    tmp_path = tmp_path.resolve()
    owners = _local_owners()
    for name, marker in {"no_marker": None, "wrong_marker": "other-op"}.items():
        root = tmp_path / "hosts" / name
        hosts = _hosts({name: root}, owners)
        pair = _tree(root)
        _run(tmp_path, name, hosts, marker=marker)
        _assert_refused(root, GUARD_MARKER)
        assert _kept(pair), name


@rehearsal
def test_rehearsal_start_at_task_cannot_skip_the_guards(tmp_path) -> None:
    # Components are opened with O_NOFOLLOW, so the tree must not sit behind a link.
    tmp_path = tmp_path.resolve()
    owners = _local_owners()
    # Starting at the copy inspection would skip the preset, identity and
    # live-unit guards yet still reach the unlink in a statically visible task
    # list, so that host also lists a live unit.
    starts = (
        ("unlink", UNLINK),
        ("copy_stat", COPY_STAT),
        ("preset", PRESET),
        ("include", INCLUDE),
    )
    for name, task in starts:
        root = tmp_path / "hosts" / name
        hosts = _hosts({name: root}, owners)
        hosts[name]["rehearsal_units"] = (
            STOPPED_UNITS + LIVE_UNITS["live_custody"] + "\n"
        )
        pair = _tree(root)
        _run(tmp_path, name, hosts, "--start-at-task", task)
        assert _kept(pair), name
        if task == INCLUDE:
            # Starting at the include skips the freeze: the undefined frozen
            # gate fails the host instead of removing anything.
            outcome = _outcome(root)
            assert outcome is not None and outcome["task"] == INCLUDE, outcome
        else:
            # Tasks inside the dynamically included file are invisible to
            # --start-at-task, so nothing in the play ran at all.
            assert _outcome(root) is None, name


@rehearsal
def test_rehearsal_extra_vars_cannot_forge_group_membership(tmp_path) -> None:
    # Components are opened with O_NOFOLLOW, so the tree must not sit behind a link.
    tmp_path = tmp_path.resolve()
    # inventory_hostname and group_names are host variables extra vars override.
    # Forging both for a host outside role_coding_hosted must not pass the group
    # check, which reads only groups and ansible_play_hosts_all.
    owners = _local_owners()
    roots = {name: tmp_path / "hosts" / name for name in ("inside", "outside")}
    hosts = _hosts(roots, owners)
    pairs = {name: _tree(root) for name, root in roots.items()}
    _run(
        tmp_path,
        "forged_group",
        {"inside": hosts["inside"]},
        "-e",
        json.dumps(
            {"inventory_hostname": "inside", "group_names": ["role_coding_hosted"]}
        ),
        outside={"outside": hosts["outside"]},
    )
    for name, root in roots.items():
        _assert_refused(root, HOST)
        assert _kept(pairs[name]), name


@rehearsal
def test_rehearsal_injected_invocations_print_nothing_sensitive(tmp_path) -> None:
    # Components are opened with O_NOFOLLOW, so the tree must not sit behind a link.
    tmp_path = tmp_path.resolve()
    # -e ansible_inject_invocation=true returns every module's arguments in its
    # result and -vvv prints them; nothing the role passes carries the password,
    # the planted document or a digest of either.
    root = tmp_path / "hosts/invocation"
    hosts = _hosts({"invocation": root}, _local_owners())
    pair = _tree(root)
    _run(
        tmp_path,
        "invocation",
        hosts,
        "-vvv",
        "-e",
        json.dumps({"ansible_inject_invocation": True}),
    )
    assert _outcome(root) == {"report": _report(root, "removed", COPIES, [])}
    assert not any(path.exists() for path in pair)


@rehearsal
def test_rehearsal_a_rogue_inventory_host_is_refused_without_limit(tmp_path) -> None:
    # Components are opened with O_NOFOLLOW, so the tree must not sit behind a link.
    tmp_path = tmp_path.resolve()
    # A labelled rogue VM that reports the reviewed identity but is a different
    # inventory host is refused by the batch check, which reads real names.
    root = tmp_path / "hosts/rogue"
    hosts = _hosts({"rogue": root}, _local_owners())
    pair = _tree(root)
    _run(tmp_path, "rogue", {"rogue": hosts["rogue"]}, host_name="rogue-vm")
    _assert_refused(root, HOST)
    assert _kept(pair)
    # With serial: 1 each batch holds one host, so the batch pin alone passes on
    # the reviewed host while a rogue host is still in the play; the
    # ansible_play_hosts_all pin refuses both.
    roots = {name: tmp_path / "hosts" / f"serial_{name}" for name in ("named", "rogue")}
    serial_hosts = _hosts(roots, _local_owners())
    pairs = {name: _tree(root) for name, root in roots.items()}
    _run(
        tmp_path,
        "serial",
        {REVIEWED_HOST: serial_hosts["named"], "rogue-vm": serial_hosts["rogue"]},
        serial=1,
    )
    for name, root in roots.items():
        _assert_refused(root, HOST)
        assert _kept(pairs[name]), name


@rehearsal
def test_rehearsal_start_at_a_main_task_cannot_open_the_gate_with_enabled_false(
    tmp_path,
) -> None:
    # Components are opened with O_NOFOLLOW, so the tree must not sit behind a link.
    tmp_path = tmp_path.resolve()
    # --start-at-task at a main.yml task with the frozen gate and registers preset
    # must not remove: starting at the freeze recomputes it false, and starting at
    # the include is caught by the raw-enabled assert.
    presets = json.dumps(
        {
            FROZEN_GATE: True,
            f"{PREFIX}copies": {"results": []},
            f"{PREFIX}unlinked": {"results": []},
        }
    )
    main_tasks = [task["name"] for task in yaml.safe_load(MAIN)]
    for task in main_tasks:
        root = tmp_path / "hosts" / f"start_{task[:8]}"
        hosts = _hosts({"h": root}, _local_owners())
        hosts["h"][f"{PREFIX}enabled"] = False
        pair = _tree(root)
        _run(
            tmp_path,
            f"startm_{task[:8]}",
            {"h": hosts["h"]},
            "--start-at-task",
            task,
            "-e",
            presets,
        )
        assert _kept(pair), task
        outcome = _outcome(root)
        if task == INCLUDE:
            assert outcome is not None and outcome["task"] == RAW_GATE, outcome


@rehearsal
def test_rehearsal_check_mode_reports_without_removing(tmp_path) -> None:
    # Components are opened with O_NOFOLLOW, so the tree must not sit behind a link.
    tmp_path = tmp_path.resolve()
    root = tmp_path / "hosts/dry_run"
    hosts = _hosts({"dry_run": root}, _local_owners())
    pair = _tree(root)
    _run(tmp_path, "check", hosts, "--check")
    assert _outcome(root) == {"report": _report(root, "would_remove", COPIES, [])}
    assert _kept(pair)


@rehearsal
def test_rehearsal_raising_templates_fail_closed_and_leak_only_through_core(
    tmp_path,
) -> None:
    # Components are opened with O_NOFOLLOW, so the tree must not sit behind a link.
    tmp_path = tmp_path.resolve()
    # The documented residual. A template that raises, rather than rendering
    # undefined, is not neutralised by default(..., true), and ansible-core 2.21.2
    # prints the raised message through the result's preserved exception field
    # even under no_log. The role still fails closed at the freeze and never
    # prints the value itself.
    owners = _local_owners()
    names = ["enabled_file_lookup", "revision_file_lookup"]
    roots = {name: tmp_path / "hosts" / name for name in names}
    hosts = _hosts(roots, owners)
    pairs = {name: _tree(root) for name, root in roots.items()}
    raising = f"{{{{ lookup('file', {PASSWORD_LOOKUP}) }}}}"
    hosts["enabled_file_lookup"][f"{PREFIX}enabled"] = raising
    hosts["revision_file_lookup"][f"{PREFIX}source_revision"] = raising
    _run(
        tmp_path,
        "residual",
        hosts,
        residual="The lookup plugin 'file' failed: Unable to access the file",
    )
    for name, task in (
        ("enabled_file_lookup", FREEZE_GATE),
        ("revision_file_lookup", FREEZE_INPUTS),
    ):
        outcome = _outcome(roots[name])
        assert outcome is not None and outcome["task"] == task, (name, outcome)
        assert _kept(pairs[name]), name
