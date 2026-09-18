"""The redacted first-run log spot-check reports counts, never credentials.

Structural tests parse the default-off role and its guarded spec. Module tests
run the real spot-check functions against synthetic credential files, synthetic
journal exports, rotated text logs and Ansible temp trees, and prove each check
class counts what it should while the result carries no value, needle or
digest. The rehearsal, gated by DITTO_ANSIBLE_REHEARSAL=1, runs the enabled
tasks and the real module through ansible-core 2.21.2 under -vvv.
"""

import base64
import copy
import gzip
import hashlib
import importlib.util
import json
import os
import pwd
import re
import subprocess
import sys
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/coding_hosted_worker_credentials_journal_check"
MAIN = (ROLE / "tasks/main.yml").read_text()
CHECK = (ROLE / "tasks/check.yml").read_text()
MODULE_PATH = ROLE / "library/coding_hosted_worker_credentials_journal_check.py"
PLAYBOOK = (
    ROOT
    / "infra/ansible/playbooks/gcp-coding-hosted-worker-credentials-journal-check.yml"
)
FIXTURE = (
    ROOT / "infra/ansible/tests/coding-hosted-worker-credentials-journal-check.yml"
)
SPEC = ROOT / "infra/ansible/guarded-runs/worker-credentials-journal-check.json"
WRITE_ROLE = ROOT / "infra/ansible/roles/coding_hosted_worker_credentials"
PREFIX = "coding_hosted_worker_credentials_journal_check_"
OPERATION = "worker-credentials-journal-check"
CONFIRMATION = "CHECK NATIVE CODING WORKER CREDENTIAL JOURNAL"
MARKER_ENV = "DITTO_CODING_HOSTED_GUARDED_RUN"
REVISION = "0123456789abcdef0123456789abcdef01234567"
REHEARSAL_GATE = "DITTO_ANSIBLE_REHEARSAL"
INPUTS = {
    f"{PREFIX}enabled": False,
    f"{PREFIX}confirmation": "",
    f"{PREFIX}source_revision": "",
}

PRESET = "Refuse a preset gate, capture, result or credential-named variable"
GATE_FREEZE = "Freeze the enabled gate once"
DORMANT = "Explain the dormant worker credential log spot-check"
INCLUDE = "Spot-check the host logs behind the enabled gate"
PRESET_INCLUDE = (
    "Refuse a preset internal name, a preset gate or a credential-named variable"
)
GUARD_MARKER = "Require the guarded entry point marker, an accident guard only"
BATCH = "Require the run to target exactly the one dedicated host"
PIPELINING = (
    "Require SSH pipelining on and no kept remote files before reading credentials"
)
CHECK_MODE = "Refuse check mode for an enabled spot-check"
FREEZE_INPUTS = "Freeze the confirmation and source revision once"
GATE = "Require the exact confirmation and source revision as frozen literals"
IDENTITY = "Probe this machine's identity into a result extra vars cannot preset"
HOST = "Require the dedicated host"
SCAN = (
    "Count credential names and value fingerprints in the host logs "
    "without returning any"
)
SCAN_CHECK = "Require the spot-check module to have scanned every source"
REPORT = "Report only per-class counts and whether the host logs are clean"
CLEAN = (
    "Refuse if a forbidden name, value fingerprint or leftover Ansible temp "
    "entry appeared"
)
MODULE_ARGS = {
    "private_dir": "/var/lib/ditto-coding-hosted/private",
    "owner": "ditto-coding-hosted",
    "journal_argv": ["/usr/bin/journalctl", "--no-pager", "--quiet", "--output=export"],
    "log_dir": "/var/log",
    "log_prefixes": [
        "syslog",
        "messages",
        "auth.log",
        "daemon.log",
        "user.log",
        "kern.log",
        "debug",
        "sudo",
    ],
    "temp_dirs": ["/root/.ansible/tmp", "/tmp", "/var/tmp"],
    "home_parent": "/home",
}
CLASSES = [
    "value_raw",
    "value_encoded",
    "value_digest",
    "env_name",
    "runtime_key_name",
    "file_name",
    "input_name",
    "module_temp_file",
]


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("journal_check_module", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Never leave a __pycache__ in the role library: the guarded entry point
    # refuses any untracked file under infra/ansible.
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


journal: Any = _load_module()


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


# ─── Structure ──────────────────────────────────────────────────────────────


def test_role_is_default_off_behind_a_frozen_gate_and_dynamic_include() -> None:
    assert yaml.safe_load((ROLE / "defaults/main.yml").read_text()) == INPUTS
    main = _docs(MAIN)
    assert [t["name"] for t in main] == [PRESET, GATE_FREEZE, DORMANT, INCLUDE]
    that = main[0]["ansible.builtin.assert"]["that"]
    assert _flat(that[0]).endswith(
        "| sort == [" + ", ".join(f"'{n}'" for n in sorted(INPUTS)) + "]"
    )
    assert "(?i)^(DITTO_CODING_WORKER_|DITTO_CODING_HIPPIUS_)" in _flat(that[1])
    freeze = main[1]
    assert freeze["no_log"] is True
    assert _flat(freeze["ansible.builtin.set_fact"][f"{PREFIX}gate"]) == (
        f"{{{{ ({PREFIX}enabled | default(false, true)) is sameas true }}}}"
    )
    assert main[3]["ansible.builtin.include_tasks"] == "check.yml"
    assert main[3]["when"] == f"{PREFIX}gate"
    assert main[2]["when"] == f"not {PREFIX}gate"
    for text in (MAIN, CHECK):
        assert not re.search(r"\|\s*bool\b", text)
        assert "import_tasks" not in text


def test_check_task_order_puts_every_guard_before_the_scan() -> None:
    assert [t["name"] for t in _docs(CHECK)] == [
        PRESET_INCLUDE,
        GUARD_MARKER,
        BATCH,
        PIPELINING,
        CHECK_MODE,
        FREEZE_INPUTS,
        GATE,
        IDENTITY,
        HOST,
        SCAN,
        SCAN_CHECK,
        REPORT,
        CLEAN,
    ]
    tasks = _docs(CHECK)
    that = _task(PRESET_INCLUDE, tasks)["ansible.builtin.assert"]["that"]
    assert that[0] == f"({PREFIX}enabled | default(false, true)) is sameas true"
    assert f"'{PREFIX}gate'" in _flat(that[1])
    assert _task(BATCH, tasks)["ansible.builtin.assert"]["that"] == [
        "ansible_play_batch == ['ditto-coding-hosted-v2']",
        "ansible_play_hosts_all == ['ditto-coding-hosted-v2']",
    ]
    assert _task(GUARD_MARKER, tasks)["ansible.builtin.assert"]["that"] == [
        f"lookup('ansible.builtin.env', '{MARKER_ENV}') == '{OPERATION}'"
    ]
    pipelining = _task(PIPELINING, tasks)["ansible.builtin.assert"]["that"]
    write_pipelining = [
        t
        for t in _docs((WRITE_ROLE / "tasks/materialize.yml").read_text())
        if "DEFAULT_KEEP_REMOTE_FILES" in json.dumps(t)
    ][0]["ansible.builtin.assert"]["that"]
    assert pipelining == write_pipelining
    gate = _task(GATE, tasks)["ansible.builtin.assert"]["that"]
    assert f"{PREFIX}gate_confirmation == '{CONFIRMATION}'" in gate
    assert f"{PREFIX}gate_source_revision | length == 40" in gate
    assert _task(FREEZE_INPUTS, tasks)["no_log"] is True


def test_scan_task_is_no_log_with_literal_arguments_and_reports_only_counts() -> None:
    tasks = _docs(CHECK)
    scan = _task(SCAN, tasks)
    assert scan["no_log"] is True
    assert scan["failed_when"] is False
    assert scan["register"] == f"{PREFIX}helper"
    assert scan["coding_hosted_worker_credentials_journal_check"] == MODULE_ARGS
    assert "{{" not in json.dumps(
        scan["coding_hosted_worker_credentials_journal_check"]
    )
    report = _flat(_task(REPORT, tasks)["ansible.builtin.debug"]["msg"])
    rendered = re.findall(r"{{(.*?)}}", report)
    assert rendered, report
    for expression in rendered:
        assert expression.strip() == f"{PREFIX}gate_source_revision" or (
            expression.strip().startswith(f"{PREFIX}helper.")
            and expression.strip().endswith("| to_json")
        ), expression
    for field in (".clean", ".totals", ".counts", ".entries", ".files", ".scanned"):
        assert field in report
    assert _task(CLEAN, tasks)["ansible.builtin.assert"]["that"] == [
        f"{PREFIX}helper.clean is sameas true"
    ]
    for task in _walk(tasks):
        assertion = task.get("ansible.builtin.assert")
        if assertion and task["name"] != SCAN_CHECK:
            assert "{{" not in assertion.get("fail_msg", ""), task["name"]
    check = _flat(_task(SCAN_CHECK, tasks)["ansible.builtin.assert"]["fail_msg"])
    assert f"{PREFIX}helper.msg | default(" in check
    for forbidden in ("slurp", "fetch", "ansible.builtin.copy", "shell", "command"):
        assert forbidden not in CHECK, forbidden


def test_module_returns_only_counts_and_booleans_by_construction() -> None:
    src = MODULE_PATH.read_text()
    assert "O_NOFOLLOW" in src and 'os.open("/"' in src
    assert "exit_json(**result)" in src
    assert "no_log" not in src.split("argument_spec", 1)[1].split("}", 1)[0]
    # Failures name a class or a fixed state, never an exception message.
    assert 'msg=f"Failed: {type(error).__name__}' in src
    assert "str(error)" not in src and "repr(error)" not in src
    # Nothing is written: no temp file and no builtin open() for writing.
    assert "tempfile" not in src and not re.search(r"(?<![\w.])open\(", src)
    assert '"wb"' not in src and "os.O_WRONLY" not in src and "O_CREAT" not in src


def test_spec_playbook_fixture_and_ci_registration() -> None:
    assert json.loads(SPEC.read_text()) == {
        "schema": "ditto-coding-hosted-guarded-run/v1",
        "operation": OPERATION,
        "playbook": "playbooks/gcp-coding-hosted-worker-credentials-journal-check.yml",
        "limit": "ditto-coding-hosted-v2",
        "enabled_var": f"{PREFIX}enabled",
        "confirmation_var": f"{PREFIX}confirmation",
        "confirmation": CONFIRMATION,
        "revision_var": f"{PREFIX}source_revision",
        "nonsecret_env_vars": [],
        "secret_env": [],
        "distinct_secret_values": False,
        "forbidden_env": ["DITTO_CODING_HIPPIUS_PRIVATE_INPUT_CURATOR_SECRET_KEY"],
        "forbidden_env_prefixes": ["DITTO_CODING_WORKER_"],
    }
    (play,) = yaml.safe_load(PLAYBOOK.read_text())
    assert play["hosts"] == "role_coding_hosted"
    assert play["become"] is True and play["gather_facts"] is False
    assert play["vars"] == {"ansible_pipelining": True}
    assert play["roles"] == ["coding_hosted_worker_credentials_journal_check"]
    assert "infra/scripts/coding-hosted-guarded-run.py" in PLAYBOOK.read_text()
    (fixture,) = yaml.safe_load(FIXTURE.read_text())
    assert fixture["hosts"] == "localhost" and fixture["become"] is False
    assert fixture["roles"] == ["coding_hosted_worker_credentials_journal_check"]
    workflow = (ROOT / ".github/workflows/infra-ci.yml").read_text()
    assert "playbooks/gcp-coding-hosted-worker-credentials-journal-check.yml" in (
        workflow
    )
    assert "tests/coding-hosted-worker-credentials-journal-check.yml" in workflow
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


def test_docs_describe_the_spot_check() -> None:
    docs = _flat(
        (ROOT / "infra/docs/coding-hosted-worker-credentials-v2.md").read_text()
    )
    for phrase in (
        OPERATION,
        CONFIRMATION,
        "--output=export",
        "base64",
        "JSON-escaped",
        "SHA-512",
        "never a matching line",
        "clean=true",
        "revoke and rotate",
        "ansible_temp_entries",
    ):
        assert phrase in docs, phrase


# ─── Module ─────────────────────────────────────────────────────────────────

VALUES = {
    "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_READER_ACCESS_KEY": "hip_readerAccess0001",
    "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_READER_SECRET_KEY": 'reader"Sec\\ret/0002+',
    "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_CURATOR_ACCESS_KEY": "hip_curatorAccess003",
    "DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_ACCESS_KEY": "hip_evidenceAccess04",
    "DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_SECRET_KEY": "evidenceSecret%0005&",
    "access_key": "GOOG1EimageAccess0006",
    "secret_key": "imageSecret/0007=abc",
    "provider": "sk-or-v1-providerKey0008",
}


def _documents() -> dict[str, bytes]:
    hippius = {
        "DITTO_CODING_HIPPIUS_ENDPOINT_URL": "https://s3.hippius.com",
        "DITTO_CODING_HIPPIUS_REGION": "decentralized",
        **{k: v for k, v in VALUES.items() if k.startswith("DITTO_")},
    }
    image = {
        "endpoint_url": "https://storage.googleapis.com",
        "bucket": "ditto-platform-agents-prod",
        "region": "auto",
        "access_key": VALUES["access_key"],
        "secret_key": VALUES["secret_key"],
    }
    return {
        "hippius-environment.json": json.dumps(hippius, sort_keys=True).encode(),
        "image-storage.json": json.dumps(image, sort_keys=True).encode(),
        "provider-key": VALUES["provider"].encode(),
    }


def _tree(tmp_path: Path) -> dict[str, Any]:
    tmp_path = tmp_path.resolve()
    private = tmp_path / "var/lib/ditto-coding-hosted/private"
    private.mkdir(parents=True)
    private.chmod(0o700)
    for name, content in _documents().items():
        (private / name).write_bytes(content)
        (private / name).chmod(0o600)
    for directory in ("log", "root-tmp", "tmp", "home"):
        (tmp_path / directory).mkdir()
    export = tmp_path / "journal.export"
    export.write_bytes(_journal_entry(b"systemd[1]: Started session 1 of user op."))
    return {
        "private_dir": str(private),
        "owner": pwd.getpwuid(os.getuid()).pw_name,
        "journal_argv": ["/bin/cat", str(export)],
        "log_dir": str(tmp_path / "log"),
        "log_prefixes": MODULE_ARGS["log_prefixes"],
        "temp_dirs": [str(tmp_path / "root-tmp"), str(tmp_path / "tmp")],
        "home_parent": str(tmp_path / "home"),
    }


def _journal_entry(message: bytes, cmdline: bytes = b"/usr/bin/sshd") -> bytes:
    size = len(message).to_bytes(8, "little")
    return (
        b"__CURSOR=s=1;i=1\n_TRANSPORT=syslog\n_CMDLINE="
        + cmdline
        + b"\nMESSAGE\n"
        + size
        + message
        + b"\n\n"
    )


def _spot_check(params: dict[str, Any]) -> dict[str, Any]:
    result = journal.spot_check(params, os.getuid())
    _assert_redacted(result)
    return result


def _all_needles() -> list[bytes]:
    documents = _documents()
    values = journal.extract_values(documents)
    needles, _ = journal.build_needles(documents, values)
    return [n for group in needles.values() for n in group]


def _assert_redacted(result: dict[str, Any]) -> None:
    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                assert isinstance(key, str)
                walk(item)
        else:
            assert isinstance(value, bool | int), value

    walk(result)
    text = json.dumps(result)
    for needle in _all_needles():
        assert needle.decode("utf-8", "replace") not in text


def _encodings(value: str) -> dict[str, bytes]:
    raw = value.encode()
    return {
        "json": json.dumps(value)[1:-1].encode(),
        "json_double": json.dumps(json.dumps(value)[1:-1])[1:-1].encode(),
        "url": urllib.parse.quote(value, safe="").encode(),
        "hex": raw.hex().encode(),
        "b64_0": base64.b64encode(raw),
        "b64_1": base64.b64encode(b"x" + raw),
        "b64_2": base64.b64encode(b"xy" + raw),
        "b64_url": base64.urlsafe_b64encode(b"?" + raw),
    }


def test_clean_logs_report_clean_with_every_count_zero(tmp_path) -> None:
    result = _spot_check(_tree(tmp_path))
    assert result["clean"] is True
    assert result["sources"]["journal"]["scanned"] is True
    assert result["totals"] == dict.fromkeys(CLASSES, 0)
    assert result["found"] == dict.fromkeys(CLASSES, False)
    assert result["values_checked"] == 8
    assert result["values_below_minimum_length"] == 0


@pytest.mark.parametrize(
    ("planted", "expected"),
    [
        (VALUES["provider"].encode(), "value_raw"),
        (b"cmd --key=" + VALUES["secret_key"].encode() + b" x", "value_raw"),
        *[
            (form, "value_encoded")
            for value in (
                VALUES["DITTO_CODING_HIPPIUS_PRIVATE_INPUT_READER_SECRET_KEY"],
                VALUES["DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_SECRET_KEY"],
            )
            for form in _encodings(value).values()
            if form != value.encode()
        ],
        (
            hashlib.sha256(VALUES["access_key"].encode()).hexdigest().encode(),
            "value_digest",
        ),
        (hashlib.md5(VALUES["provider"].encode()).hexdigest().encode(), "value_digest"),
        (
            hashlib.sha512(_documents()["image-storage.json"]).hexdigest().encode(),
            "value_digest",
        ),
        (b"export DITTO_CODING_WORKER_PROVIDER_KEY=", "env_name"),
        (
            b"missing DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_SECRET_KEY",
            "runtime_key_name",
        ),
        (b"stat /var/lib/ditto-coding-hosted/private/image-storage.json", "file_name"),
        (b"coding_hosted_worker_credentials_documents=...", "input_name"),
        (b"MATERIALIZE NATIVE CODING WORKER CREDENTIALS", "input_name"),
        (
            b"python3 /home/op/.ansible/tmp/"
            b"AnsiballZ_coding_hosted_worker_credentials_write.py",
            "module_temp_file",
        ),
    ],
)
def test_each_class_is_counted_in_the_journal_message_or_command_line(
    tmp_path, planted: bytes, expected: str
) -> None:
    params = _tree(tmp_path)
    export = Path(params["journal_argv"][1])
    for entry in (_journal_entry(b"prefix " + planted), _journal_entry(b"ok", planted)):
        export.write_bytes(_journal_entry(b"before") + entry + _journal_entry(b"after"))
        result = _spot_check(params)
        assert result["clean"] is False
        assert result["sources"]["journal"]["counts"][expected] >= 1, expected
        assert result["found"][expected] is True


def test_matches_across_chunk_boundaries_are_counted_once() -> None:
    documents = _documents()
    needles, _ = journal.build_needles(documents, journal.extract_values(documents))
    stream = (
        b"a" * 1000
        + VALUES["provider"].encode()
        + b"b" * 7
        + VALUES["provider"].encode()
        + b"DITTO_CODING_WORKER_PROVIDER_KEY"
    )
    whole = journal.Scanner(needles)
    whole.feed(stream)
    for size in (1, 3, 7, 1024):
        split = journal.Scanner(needles)
        for start in range(0, len(stream), size):
            split.feed(stream[start : start + size])
        assert split.counts == whole.counts, size
    assert whole.counts["value_raw"] == 2
    assert whole.counts["env_name"] == 1


def test_rotated_text_logs_and_ansible_temp_leftovers_are_scanned(tmp_path) -> None:
    params = _tree(tmp_path)
    log_dir = Path(params["log_dir"])
    with gzip.open(log_dir / "syslog.2.gz", "wb") as handle:
        handle.write(b"Sep 15 host sudo: " + VALUES["secret_key"].encode())
    (log_dir / "auth.log").write_bytes(b"sshd: accepted key\n")
    (log_dir / "unrelated.log").write_bytes(VALUES["provider"].encode())
    result = _spot_check(params)
    assert result["sources"]["text_logs"]["files"] == 2
    assert result["sources"]["text_logs"]["counts"]["value_raw"] == 1
    assert result["clean"] is False

    params = _tree(tmp_path / "temp")
    leftover = Path(params["temp_dirs"][0]) / "ansible-tmp-1-2-3"
    leftover.mkdir()
    (leftover / "AnsiballZ_coding_hosted_worker_credentials_write.py").write_bytes(
        b"ANSIBALLZ_PARAMS = " + json.dumps(VALUES["provider"]).encode()
    )
    (Path(params["temp_dirs"][1]) / "systemd-private-x").mkdir()
    home_tmp = Path(params["home_parent"]) / "operator/.ansible/tmp"
    home_tmp.mkdir(parents=True)
    (home_tmp / "ansible-tmp-4-5-6").mkdir()
    result = _spot_check(params)
    temp = result["sources"]["ansible_temp"]
    assert temp["entries"] == 2
    assert temp["files"] == 1
    assert temp["counts"]["value_raw"] == 1
    assert result["clean"] is False


def test_an_unreadable_journal_is_never_clean(tmp_path) -> None:
    params = _tree(tmp_path)
    params["journal_argv"] = ["/bin/false"]
    result = _spot_check(params)
    assert result["sources"]["journal"]["scanned"] is False
    assert result["clean"] is False
    params["journal_argv"] = ["cat", "relative"]
    with pytest.raises(journal.Unsafe):
        journal.spot_check(params, os.getuid())


def test_linked_missing_or_foreign_credential_files_are_refused(tmp_path) -> None:
    params = _tree(tmp_path)
    private = Path(params["private_dir"])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "provider-key").write_bytes(b"planted-value-000")
    (private / "provider-key").unlink()
    (private / "provider-key").symlink_to(elsewhere / "provider-key")
    with pytest.raises(journal.Unsafe, match="provider-key is a symlink"):
        journal.spot_check(params, os.getuid())
    (private / "provider-key").unlink()
    with pytest.raises(journal.Unsafe, match="provider-key is absent"):
        journal.spot_check(params, os.getuid())
    (private / "provider-key").write_bytes(b"x" * 20)
    with pytest.raises(journal.Unsafe, match="not owned by the worker"):
        journal.spot_check(params, os.getuid() + 1)
    moved = private.parent / "private-real"
    private.rename(moved)
    private.symlink_to(moved)
    with pytest.raises(journal.Unsafe, match="symlink"):
        journal.spot_check(params, os.getuid())


def test_refusal_messages_never_carry_a_value(tmp_path) -> None:
    params = _tree(tmp_path)
    private = Path(params["private_dir"])
    (private / "image-storage.json").write_bytes(
        b'{"access_key": "' + VALUES["access_key"].encode() + b'"}'
    )
    with pytest.raises(journal.Unsafe) as refused:
        journal.spot_check(params, os.getuid())
    assert VALUES["access_key"] not in str(refused.value)


def test_short_values_are_counted_not_searched(tmp_path) -> None:
    params = _tree(tmp_path)
    (Path(params["private_dir"]) / "provider-key").write_bytes(b"short")
    result = _spot_check(params)
    assert result["values_below_minimum_length"] == 1
    assert result["clean"] is False


# ─── Rehearsal ──────────────────────────────────────────────────────────────

rehearsal = pytest.mark.skipif(
    os.environ.get(REHEARSAL_GATE) != "1",
    reason=f"set {REHEARSAL_GATE}=1 to run the ansible-core rehearsal",
)


def _rehearsal_tasks(params: dict[str, Any]) -> list[dict]:
    main = _docs(MAIN)
    tasks = [
        copy.deepcopy(main[0]),
        copy.deepcopy(main[1]),
        *copy.deepcopy(_docs(CHECK)),
    ]
    host = _task(HOST, tasks)["ansible.builtin.assert"]
    host["that"] = [
        re.sub(
            r"(\S+\.ansible_facts\.(ansible_\w+)) == '[^']+'",
            r"\1 == rehearsal_local_identity.ansible_facts.\2",
            line,
        )
        for line in host["that"]
    ]
    _task(SCAN, tasks)["coding_hosted_worker_credentials_journal_check"] = params
    return tasks


def _play(tasks: list[dict], root: Path) -> dict:
    outcome = f"{root}/outcome.json"
    return {
        "name": "Rehearse the spot-check",
        "hosts": "all",
        "connection": "local",
        "gather_facts": False,
        "become": False,
        "vars": {
            "ansible_python_interpreter": "{{ ansible_playbook_python }}",
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
                        "ansible.builtin.copy": {"dest": outcome, "content": "{}"},
                    },
                ],
                "rescue": [
                    {
                        "name": "Record refusal",
                        "ansible.builtin.copy": {
                            "dest": outcome,
                            "content": (
                                "{{ {'task': ansible_failed_task.name} | to_json }}"
                            ),
                        },
                    }
                ],
            },
        ],
    }


def _run(tmp_path: Path, name: str, params: dict[str, Any], marker: str | None) -> str:
    work = tmp_path / f"run-{name}"
    work.mkdir()
    root = tmp_path / f"outcome-{name}"
    root.mkdir()
    hostvars = {
        f"{PREFIX}enabled": True,
        f"{PREFIX}confirmation": CONFIRMATION,
        f"{PREFIX}source_revision": REVISION,
    }
    inventory = {
        "all": {
            "children": {
                "role_coding_hosted": {"hosts": {"ditto-coding-hosted-v2": hostvars}}
            }
        }
    }
    (work / "inventory.yml").write_text(yaml.safe_dump(inventory))
    play = _play(_rehearsal_tasks(params), root)
    (work / "play.yml").write_text(yaml.safe_dump([play], sort_keys=False))
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("ANSIBLE_") and not k.startswith("DITTO_CODING_")
    }
    env |= {
        "ANSIBLE_HOME": str(work / "h"),
        "ANSIBLE_LOCAL_TEMP": str(work / "t"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_RETRY_FILES_ENABLED": "0",
        "ANSIBLE_CALLBACK_RESULT_FORMAT": "yaml",
        "ANSIBLE_LIBRARY": str(ROLE / "library"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if marker is not None:
        env[MARKER_ENV] = marker
    completed = subprocess.run(
        [
            "uvx",
            "--from",
            "ansible-core==2.21.2",
            "ansible-playbook",
            "-i",
            "inventory.yml",
            "-vvv",
            "--diff",
            "play.yml",
        ],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    output = completed.stdout + completed.stderr
    assert "PLAY RECAP" in output, output[-4000:]
    _assert_output_redacted(output)
    return output


def _assert_output_redacted(output: str) -> None:
    for value in VALUES.values():
        assert value not in output
        for form in _encodings(value).values():
            assert form.decode() not in output
    for needle in _all_needles():
        text = needle.decode("utf-8", "replace")
        if re.fullmatch(r"[0-9a-f]{32,128}", text):
            assert text not in output, "a digest reached ansible output"


def _outcome(tmp_path: Path, name: str) -> dict:
    return json.loads((tmp_path / f"outcome-{name}/outcome.json").read_text())


@rehearsal
def test_rehearsal_reports_counts_and_refuses_only_on_findings(tmp_path) -> None:
    clean = _tree(tmp_path / "clean")
    output = _run(tmp_path, "clean", clean, OPERATION)
    assert _outcome(tmp_path, "clean") == {}
    assert "clean=true" in output
    assert '"value_raw": 0' in output

    leaked = _tree(tmp_path / "leaked")
    export = Path(leaked["journal_argv"][1])
    export.write_bytes(
        _journal_entry(b"x " + base64.b64encode(b"z" + VALUES["provider"].encode()))
        + _journal_entry(b"DITTO_CODING_WORKER_IMAGE_STORAGE_SECRET_KEY")
    )
    output = _run(tmp_path, "leaked", leaked, OPERATION)
    assert _outcome(tmp_path, "leaked") == {"task": CLEAN}
    assert "clean=false" in output
    assert '"value_encoded": 1' in output and '"env_name": 1' in output

    missing = _tree(tmp_path / "missing")
    (Path(missing["private_dir"]) / "provider-key").unlink()
    output = _run(tmp_path, "missing", missing, OPERATION)
    assert _outcome(tmp_path, "missing") == {"task": SCAN_CHECK}
    assert "Refused: provider-key is absent." in output

    for name, marker in (
        ("no_marker", None),
        ("wrong_marker", "worker-credentials-remove"),
    ):
        params = _tree(tmp_path / name)
        export = Path(params["journal_argv"][1])
        export.write_bytes(_journal_entry(VALUES["provider"].encode()))
        output = _run(tmp_path, name, params, marker)
        assert _outcome(tmp_path, name) == {"task": GUARD_MARKER}
        assert "clean=" not in output
