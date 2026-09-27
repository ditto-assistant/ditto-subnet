"""Tests for the shared guarded entry point of the native Coding secret roles.

This file is shared, byte for byte, by every branch that ships the guard
(infra/scripts/coding-hosted-guarded-run.py). Operation-specific assertions live
in each role's own test file; the checks here are generic and iterate over
whatever specs infra/ansible/guarded-runs/ holds.
"""

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parents[2]
GUARD = ROOT / "infra/scripts/coding-hosted-guarded-run.py"
LOCK = ROOT / "infra/scripts/coding-hosted-guarded-run.py.lock"
SPECS = ROOT / "infra/ansible/guarded-runs"
REHEARSAL_GATE = "DITTO_ANSIBLE_REHEARSAL"
CANARY = "Zq8GuardCanary4v"
REVISION_ZERO = "0" * 40
MARKER = "DITTO_CODING_HOSTED_GUARDED_RUN"


def _load_guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("coding_hosted_guarded_run", GUARD)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    # Never leave infra/scripts/__pycache__ behind: the guard refuses a checkout
    # with any untracked or ignored file under infra/scripts.
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


guard: Any = _load_guard()


# ---------------------------------------------------------------------------
# Synthetic repositories
# ---------------------------------------------------------------------------

SYNTHETIC_SPEC: dict[str, Any] = {
    "schema": "ditto-coding-hosted-guarded-run/v1",
    "operation": "rehearsal-probe",
    "playbook": "playbooks/gcp-coding-hosted-rehearsal.yml",
    "limit": "ditto-coding-hosted-v2",
    "enabled_var": "coding_hosted_rehearsal_enabled",
    "confirmation_var": "coding_hosted_rehearsal_confirmation",
    "confirmation": "REHEARSE GUARDED RUN",
    "revision_var": "coding_hosted_rehearsal_source_revision",
    "nonsecret_env_vars": [
        {
            "env": "DITTO_CODING_REHEARSAL_HOST",
            "var": "coding_hosted_rehearsal_host",
            "pattern": "10[.]30[.]0[.][0-9]{1,3}",
            "max_length": 15,
        }
    ],
    "secret_env": [
        {
            "name": "DITTO_CODING_REHEARSAL_SECRET",
            "charset": "single_line",
            "prefix": "",
            "min_length": 1,
            "max_length": 256,
        },
        {
            "name": "DITTO_CODING_REHEARSAL_TOKEN",
            "charset": "printable_ascii",
            "prefix": "tok_",
            "min_length": 5,
            "max_length": 64,
        },
    ],
    "distinct_secret_values": True,
    "forbidden_env": ["DITTO_CODING_REHEARSAL_FORBIDDEN"],
    "forbidden_env_prefixes": ["DITTO_CODING_REHEARSAL_BANNED_"],
}

REHEARSAL_PLAYBOOK = [
    {
        "name": "Record what the guarded run handed to ansible",
        "hosts": "role_coding_hosted",
        "gather_facts": False,
        "tasks": [
            {
                "name": "Record the extra vars, marker and environment shape",
                "ansible.builtin.copy": {
                    "dest": "{{ lookup('ansible.builtin.env', 'HOME') }}/outcome.json",
                    "mode": "0600",
                    "content": (
                        "{{ {"
                        "'enabled': coding_hosted_rehearsal_enabled is sameas true, "
                        "'confirmation': coding_hosted_rehearsal_confirmation, "
                        "'revision': coding_hosted_rehearsal_source_revision, "
                        "'host': coding_hosted_rehearsal_host, "
                        "'marker': lookup('ansible.builtin.env', '" + MARKER + "'), "
                        "'secret_length': lookup('ansible.builtin.env', "
                        "'DITTO_CODING_REHEARSAL_SECRET') | length, "
                        "'dropped': lookup('ansible.builtin.env', "
                        "'DITTO_REHEARSAL_DROPPED'), "
                        "'config': lookup('ansible.builtin.env', 'ANSIBLE_CONFIG'), "
                        "'keep_remote_files': lookup('ansible.builtin.config', "
                        "'DEFAULT_KEEP_REMOTE_FILES'), "
                        "'verbosity': lookup('ansible.builtin.config', "
                        "'DEFAULT_VERBOSITY'), "
                        "'play_hosts': ansible_play_hosts_all"
                        "} | to_json }}"
                    ),
                },
            }
        ],
    }
]

REHEARSAL_INVENTORY = {
    "all": {
        "children": {
            "role_coding_hosted": {
                "hosts": {
                    "ditto-coding-hosted-v2": {
                        "ansible_connection": "local",
                        "ansible_python_interpreter": "{{ ansible_playbook_python }}",
                    },
                    "ditto-coding-hosted-rogue": {
                        "ansible_connection": "local",
                        "ansible_python_interpreter": "{{ ansible_playbook_python }}",
                    },
                }
            }
        }
    }
}


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        env={
            "HOME": str(repo),
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "rehearsal",
            "GIT_AUTHOR_EMAIL": "rehearsal@example.invalid",
            "GIT_COMMITTER_NAME": "rehearsal",
            "GIT_COMMITTER_EMAIL": "rehearsal@example.invalid",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _synthetic_repo(tmp_path: Path, spec: dict | None = None) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    ansible = repo / "infra/ansible"
    (repo / "infra/scripts").mkdir(parents=True)
    shutil.copy(GUARD, repo / "infra/scripts" / GUARD.name)
    shutil.copy(LOCK, repo / "infra/scripts" / LOCK.name)
    (ansible / "inventory").mkdir(parents=True)
    (ansible / "playbooks").mkdir()
    (ansible / "guarded-runs").mkdir()
    (ansible / "roles/placeholder/tasks").mkdir(parents=True)
    (ansible / "roles/placeholder/tasks/main.yml").write_text("---\n[]\n")
    (ansible / "ansible.cfg").write_text(
        "[defaults]\nroles_path = roles\nretry_files_enabled = False\n"
    )
    (ansible / "inventory/gcp.yml").write_text(yaml.safe_dump(REHEARSAL_INVENTORY))
    (ansible / SYNTHETIC_SPEC["playbook"]).write_text(
        yaml.safe_dump(REHEARSAL_PLAYBOOK, sort_keys=False)
    )
    (ansible / "guarded-runs/rehearsal-probe.json").write_text(
        json.dumps(spec or SYNTHETIC_SPEC, indent=2) + "\n"
    )
    (repo / "README").write_text("synthetic\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "synthetic")
    return repo, _git(repo, "rev-parse", "HEAD")


def _operator_env(home: Path, **overrides: str) -> dict[str, str]:
    env = {
        "HOME": str(home),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "DITTO_CODING_REHEARSAL_HOST": "10.30.0.5",
        "DITTO_CODING_REHEARSAL_SECRET": f'stand-in "secret" \\ {CANARY}',
        "DITTO_CODING_REHEARSAL_TOKEN": f"tok_{CANARY}",
        "DITTO_REHEARSAL_DROPPED": f"dropped-{CANARY}",
    }
    env.update(overrides)
    return env


class _Recorder:
    def __init__(self) -> None:
        self.invocations: list[Any] = []

    def __call__(self, invocation: Any) -> int:
        self.invocations.append(invocation)
        return 0


def _run(repo: Path, argv: list[str], env: dict[str, str]) -> tuple[int, str, Any]:
    lines: list[str] = []
    recorder = _Recorder()
    code = guard.run(
        argv,
        env,
        root=repo,
        runner=recorder,
        runtime=lambda _root: "/synthetic/python",
        out=lines.append,
    )
    output = "\n".join(lines)
    assert CANARY not in output, "a refusal or summary echoed an input value"
    return code, output, recorder


# ---------------------------------------------------------------------------
# The guard file is shared and pinned
# ---------------------------------------------------------------------------


def test_guard_is_a_locked_uv_script_pinning_ansible_core() -> None:
    text = GUARD.read_text()
    assert text.startswith("#!/usr/bin/env python3\n# /// script\n")
    metadata = text.split("# ///\n", 1)[0]
    assert '# requires-python = ">=3.12"\n' in metadata
    pins = re.findall(r'^#     "([a-z-]+)==([0-9.]+)",$', metadata, re.M)
    # ansible-core, plus the libraries google.cloud.gcp_compute needs to parse
    # the real inventory; every direct dependency is pinned exactly.
    assert [name for name, _ in pins] == ["ansible-core", "google-auth", "requests"]
    assert metadata.count("==") == 3 and pins[0][1] == "2.21.2"
    lock = LOCK.read_text()
    for name, version in pins:
        assert f'{{ name = "{name}", specifier = "=={version}" }}' in lock
    assert re.search(r'name = "ansible-core"\nversion = "2\.21\.2"', lock)
    assert guard.ANSIBLE_CORE_VERSION == "2.21.2"
    # Every release of the shared guard updates this pin in the shared test, so
    # branches that carry it cannot drift apart unnoticed.
    assert hashlib.sha256(GUARD.read_bytes()).hexdigest() == GUARD_SHA256


def test_script_directory_is_dropped_from_sys_path_before_other_imports(
    tmp_path: Path,
) -> None:
    # An untracked json.py or ansible/ beside the script would otherwise shadow
    # the standard library or ansible before the checkout is verified, even
    # when the checkout is reached through a symlinked directory.
    lines = [
        line
        for line in GUARD.read_text().split('"""', 2)[2].splitlines()
        if line.startswith(("import ", "from ", "sys.path", "if __name__"))
    ]
    assert lines[:3] == [
        "import sys",
        'if __name__ == "__main__" and not sys.flags.safe_path and sys.path:',
        'sys.path[:] = [entry for entry in sys.path if entry not in ("", ".")]',
    ]
    real = tmp_path / "real/infra/scripts"
    real.mkdir(parents=True)
    head = GUARD.read_text().split("import hashlib", 1)[0]
    (real / "probe.py").write_text(head + "import json\nprint(json.__file__)\n")
    (real / "json.py").write_text("raise SystemExit('shadowed')\n")
    (tmp_path / "link").symlink_to(tmp_path / "real")
    for script in (real / "probe.py", tmp_path / "link/infra/scripts/probe.py"):
        completed = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            check=False,
            env={"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1"},
        )
        assert completed.returncode == 0, completed.stderr
        assert "shadowed" not in completed.stdout + completed.stderr
        assert str(real) not in completed.stdout


GUARD_SHA256 = "a8ff028ecf7846fdda440f4a90dae187822dc5ca476e7c9c463ac199e30e7297"


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["rehearsal-probe"],
        ["rehearsal-probe", REVISION_ZERO, "-e", "x=1"],
        ["rehearsal-probe", REVISION_ZERO, "--start-at-task", "Write"],
        ["rehearsal-probe", REVISION_ZERO, "-v"],
        ["rehearsal-probe", REVISION_ZERO, "--step"],
        ["rehearsal-probe", REVISION_ZERO, "--tags", "write"],
        ["rehearsal-probe", REVISION_ZERO, "--skip-tags", "guard"],
        ["rehearsal-probe", REVISION_ZERO, "--check"],
        ["rehearsal-probe", REVISION_ZERO, "--limit", "all"],
        ["rehearsal-probe", REVISION_ZERO, "-i", "other.yml"],
        ["-e", REVISION_ZERO],
        ["rehearsal-probe", "--step"],
        ["--help", REVISION_ZERO],
        ["../../etc/passwd", REVISION_ZERO],
        ["Rehearsal-Probe", REVISION_ZERO],
        ["rehearsal-probe", REVISION_ZERO.upper().replace("0", "A")],
        ["rehearsal-probe", REVISION_ZERO + "\n"],
        ["rehearsal-probe", REVISION_ZERO[:39]],
        ["rehearsal-probe", "HEAD"],
    ],
)
def test_only_an_operation_and_a_reviewed_revision_are_accepted(
    tmp_path: Path, argv: list[str]
) -> None:
    repo, _ = _synthetic_repo(tmp_path)
    code, output, recorder = _run(repo, argv, _operator_env(tmp_path))
    assert code == 2
    assert recorder.invocations == []
    assert "refused; nothing was run" in output


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ANSIBLE_CONFIG", "/tmp/evil.cfg"),
        ("ANSIBLE_KEEP_REMOTE_FILES", "1"),
        ("ANSIBLE_DEBUG", "1"),
        ("ANSIBLE_VERBOSITY", "4"),
        ("ANSIBLE_LOG_PATH", "/tmp/ansible.log"),
        ("ANSIBLE_CALLBACK_PLUGINS", "/tmp/cb"),
        ("ANSIBLE_CALLBACKS_ENABLED", "tree"),
        ("ANSIBLE_STDOUT_CALLBACK", "json"),
        ("ANSIBLE_STRATEGY", "debug"),
        ("ANSIBLE_STRATEGY_PLUGINS", "/tmp/strategy"),
        ("ANSIBLE_LOOKUP_PLUGINS", "/tmp/lookup"),
        ("ANSIBLE_LIBRARY", "/tmp/modules"),
        ("ANSIBLE_REMOTE_TEMP", "/tmp/remote"),
        ("ANSIBLE_PIPELINING", "0"),
        ("ANSIBLE_INVENTORY", "/tmp/inventory.yml"),
        ("ANSIBLE_ENABLE_TASK_DEBUGGER", "1"),
        ("ANSIBLE_NO_LOG", "0"),
        ("_ANSIBLE_SSH_ASKPASS_SHM", "x"),
        ("PYTHONPATH", "/tmp/evil"),
        ("PYTHONSTARTUP", "/tmp/evil.py"),
        ("PYTHONHOME", "/tmp"),
        ("PYTHONINSPECT", "1"),
        ("PYTHONWARNINGS", "always"),
        ("LD_PRELOAD", "/tmp/evil.so"),
        ("LD_LIBRARY_PATH", "/tmp"),
        ("DYLD_INSERT_LIBRARIES", "/tmp/evil.dylib"),
        (MARKER, "rehearsal-probe"),
        ("OPENSSL_CONF", "/tmp/evil.cnf"),
        ("OPENSSL_MODULES", "/tmp"),
        ("GCONV_PATH", "/tmp"),
        ("GLIBC_TUNABLES", "glibc.malloc.check=3"),
        ("UV_PYTHON", "/tmp/evil-python"),
        ("UV_PYTHON_INSTALL_MIRROR", "https://mirror.invalid"),
        ("UV_NO_VERIFY_HASHES", "1"),
        ("CLOUDSDK_PYTHON", "/tmp/evil-python"),
        ("CLOUDSDK_PYTHON_SITEPACKAGES", "1"),
        ("SSL_CERT_FILE", "/tmp/evil-ca.pem"),
        ("PATH", ".:/usr/bin:/bin"),
        ("PATH", "/usr/bin::/bin"),
        ("PATH", "bin:/usr/bin"),
        ("PATH", ""),
        ("HOME", "relative"),
        ("DITTO_CODING_REHEARSAL_FORBIDDEN", ""),
        ("DITTO_CODING_REHEARSAL_BANNED_KEY", "x"),
    ],
)
def test_dangerous_environment_is_refused_before_ansible(
    tmp_path: Path, name: str, value: str
) -> None:
    repo, revision = _synthetic_repo(tmp_path)
    env = _operator_env(tmp_path, **{name: value})
    code, output, recorder = _run(repo, ["rehearsal-probe", revision], env)
    assert code == 2
    assert recorder.invocations == []
    assert name in output


def test_harmless_python_settings_are_accepted(tmp_path: Path) -> None:
    repo, revision = _synthetic_repo(tmp_path)
    env = _operator_env(
        tmp_path,
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUNBUFFERED="1",
        UV_PYTHON_INSTALL_DIR="/opt/uv/python",
        UV_CACHE_DIR="/opt/uv/cache",
    )
    code, _, recorder = _run(repo, ["rehearsal-probe", revision], env)
    assert code == 0
    assert len(recorder.invocations) == 1


RAISING_TEMPLATE = "{{ lookup('file', lookup('env', 'DITTO_CODING_REHEARSAL_TOKEN')) }}"


@pytest.mark.parametrize(
    ("value", "failure"),
    [
        (None, "missing"),
        ("", "empty"),
        (RAISING_TEMPLATE, "template_syntax"),
        (f"prefix {{{{ {CANARY} }}}}", "template_syntax"),
        (f"{{% raw %}}{CANARY}{{% endraw %}}", "template_syntax"),
        (f"{{# {CANARY} #}}", "template_syntax"),
        (f"#jinja2: variable_start_string:'[['\n[[ {CANARY} ]]", "template_syntax"),
        (f"x #JINJA2:{CANARY}", "template_syntax"),
        (f"{{{{{CANARY}", "template_syntax"),
        (f"{CANARY}\n", "control_character"),
        (f"{CANARY}\r", "control_character"),
        (f"{CANARY}\x7f", "control_character"),
        (f"{CANARY}\x85tail", "control_character"),
        (f"{CANARY}\u2028tail", "control_character"),
        (f"{CANARY}\u2029tail", "control_character"),
        (f"{CANARY}\u200btail", "control_character"),
        (f" {CANARY}", "surrounding_whitespace"),
        (f"{CANARY} ", "surrounding_whitespace"),
        (f"{CANARY}\udcff", "invalid_encoding"),
        (CANARY * 20, "too_long"),
    ],
)
def test_single_line_secret_failures_name_only_the_class(
    tmp_path: Path, value: str | None, failure: str
) -> None:
    repo, revision = _synthetic_repo(tmp_path)
    env = _operator_env(tmp_path)
    if value is None:
        del env["DITTO_CODING_REHEARSAL_SECRET"]
    else:
        env["DITTO_CODING_REHEARSAL_SECRET"] = value
    code, output, recorder = _run(repo, ["rehearsal-probe", revision], env)
    assert code == 2
    assert recorder.invocations == []
    assert f"input: DITTO_CODING_REHEARSAL_SECRET: {failure}" in output
    assert "lookup" not in output


@pytest.mark.parametrize(
    ("value", "failure"),
    [
        (f"tok_{CANARY} x", "not_printable_ascii_without_whitespace"),
        (f"tok_{CANARY}é", "not_printable_ascii_without_whitespace"),
        (f"tok_{{{{{CANARY}}}}}", "template_syntax"),
        (f"key_{CANARY}", "missing_prefix"),
        ("tok_", "too_short"),
        (f"tok_{CANARY * 5}", "too_long"),
    ],
)
def test_printable_ascii_secret_failures_name_only_the_class(
    tmp_path: Path, value: str, failure: str
) -> None:
    repo, revision = _synthetic_repo(tmp_path)
    env = _operator_env(tmp_path, DITTO_CODING_REHEARSAL_TOKEN=value)
    code, output, recorder = _run(repo, ["rehearsal-probe", revision], env)
    assert code == 2
    assert recorder.invocations == []
    assert f"input: DITTO_CODING_REHEARSAL_TOKEN: {failure}" in output


@pytest.mark.parametrize(
    ("value", "failure"),
    [
        ("10.30.0.5 ", "pattern_mismatch"),
        ("10.30.0.5\n", "pattern_mismatch"),
        ("{{ lookup('env', 'X') }}", "template_syntax"),
        ("10.30.0.1234567890", "too_long"),
        ("192.168.0.1", "pattern_mismatch"),
    ],
)
def test_nonsecret_inputs_must_fully_match_their_pattern(
    tmp_path: Path, value: str, failure: str
) -> None:
    repo, revision = _synthetic_repo(tmp_path)
    env = _operator_env(tmp_path, DITTO_CODING_REHEARSAL_HOST=value)
    code, output, recorder = _run(repo, ["rehearsal-probe", revision], env)
    assert code == 2
    assert recorder.invocations == []
    assert f"input: DITTO_CODING_REHEARSAL_HOST: {failure}" in output


def test_duplicate_secret_values_are_refused_by_name(tmp_path: Path) -> None:
    spec = json.loads(json.dumps(SYNTHETIC_SPEC))
    spec["secret_env"][0]["charset"] = "printable_ascii"
    repo, revision = _synthetic_repo(tmp_path, spec)
    env = _operator_env(
        tmp_path,
        DITTO_CODING_REHEARSAL_SECRET=f"tok_{CANARY}",
        DITTO_CODING_REHEARSAL_TOKEN=f"tok_{CANARY}",
    )
    code, output, recorder = _run(repo, ["rehearsal-probe", revision], env)
    assert code == 2
    assert recorder.invocations == []
    assert "DITTO_CODING_REHEARSAL_TOKEN: duplicate_value" in output


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------


def _refused_checkout(repo: Path, revision: str, home: Path, needle: str) -> None:
    code, output, recorder = _run(
        repo, ["rehearsal-probe", revision], _operator_env(home)
    )
    assert code == 2, output
    assert recorder.invocations == []
    assert needle in output, output


def test_wrong_revision_is_refused(tmp_path: Path) -> None:
    repo, revision = _synthetic_repo(tmp_path)
    other = "f" * 40 if revision != "f" * 40 else "e" * 40
    _refused_checkout(repo, other, tmp_path, "HEAD is not the reviewed revision")
    (repo / "README").write_text("second\n")
    _git(repo, "commit", "-q", "-am", "second")
    _refused_checkout(repo, revision, tmp_path, "HEAD is not the reviewed revision")


def test_dirty_tracked_file_outside_ansible_is_refused(tmp_path: Path) -> None:
    repo, revision = _synthetic_repo(tmp_path)
    (repo / "README").write_text("dirty\n")
    _refused_checkout(repo, revision, tmp_path, "tracked files differ from HEAD")


def test_staged_change_is_refused(tmp_path: Path) -> None:
    repo, revision = _synthetic_repo(tmp_path)
    (repo / "README").write_text("staged\n")
    _git(repo, "add", "README")
    _refused_checkout(repo, revision, tmp_path, "tracked files differ from HEAD")


@pytest.mark.parametrize(
    "relative",
    [
        "infra/ansible/host_vars/ditto-coding-hosted-v2.yml",
        "infra/ansible/inventory/group_vars/all.yml",
        "infra/ansible/roles/placeholder/library/__pycache__/x.pyc",
        "infra/ansible/.guarded-run-no-plugins/lookup/env.py",
        "infra/ansible/guarded-runs/forged.json",
        "infra/scripts/json.py",
        "infra/scripts/ansible/__init__.py",
    ],
)
def test_untracked_or_ignored_files_under_ansible_are_refused(
    tmp_path: Path, relative: str
) -> None:
    repo, revision = _synthetic_repo(tmp_path)
    (repo / ".gitignore").write_text("__pycache__/\nhost_vars/\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore")
    revision = _git(repo, "rev-parse", "HEAD")
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ansible_keep_remote_files: true\n")
    _refused_checkout(repo, revision, tmp_path, f"untracked or ignored file {relative}")


def test_assume_unchanged_and_skip_worktree_edits_are_refused(tmp_path: Path) -> None:
    repo, revision = _synthetic_repo(tmp_path)
    cfg = repo / "infra/ansible/ansible.cfg"
    _git(repo, "update-index", "--assume-unchanged", "infra/ansible/ansible.cfg")
    cfg.write_text("[defaults]\nlog_path = /tmp/leak.log\n")
    assert _git(repo, "status", "--porcelain") == ""
    _refused_checkout(
        repo, revision, tmp_path, "infra/ansible/ansible.cfg differs from the reviewed"
    )
    _git(repo, "update-index", "--no-assume-unchanged", "infra/ansible/ansible.cfg")
    _git(repo, "checkout", "--", "infra/ansible/ansible.cfg")
    inventory = "infra/ansible/inventory/gcp.yml"
    _git(repo, "update-index", "--skip-worktree", inventory)
    (repo / inventory).write_text("all: {hosts: {evil: {}}}\n")
    _refused_checkout(repo, revision, tmp_path, f"{inventory} differs from")


def test_same_size_same_mtime_edit_is_still_refused(tmp_path: Path) -> None:
    repo, revision = _synthetic_repo(tmp_path)
    cfg = repo / "infra/ansible/ansible.cfg"
    before = cfg.stat()
    original = cfg.read_bytes()
    cfg.write_bytes(original.replace(b"False", b"Fals3"))
    os.utime(cfg, ns=(before.st_atime_ns, before.st_mtime_ns))
    _refused_checkout(repo, revision, tmp_path, "ansible.cfg differs from")


def test_symlink_and_mode_swaps_are_refused(tmp_path: Path) -> None:
    repo, revision = _synthetic_repo(tmp_path)
    inventory = repo / "infra/ansible/inventory/gcp.yml"
    copy = tmp_path / "copy.yml"
    shutil.copy(inventory, copy)
    inventory.unlink()
    inventory.symlink_to(copy)
    _refused_checkout(repo, revision, tmp_path, "inventory/gcp.yml differs from")
    inventory.unlink()
    shutil.copy(copy, inventory)
    inventory.chmod(0o755)
    _refused_checkout(repo, revision, tmp_path, "inventory/gcp.yml differs from")


def test_untracked_directories_even_empty_are_refused(tmp_path: Path) -> None:
    repo, revision = _synthetic_repo(tmp_path)
    (repo / "infra/ansible/playbooks/roles").mkdir()
    _refused_checkout(
        repo,
        revision,
        tmp_path,
        "untracked or ignored directory infra/ansible/playbooks/roles",
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_execute_only_directories_hiding_files_are_refused(tmp_path: Path) -> None:
    # os.walk silently skips a directory it cannot list, yet ansible can open a
    # known path inside it: a shadow role under playbooks/roles, for example.
    repo, revision = _synthetic_repo(tmp_path)
    hidden = repo / "infra/ansible/roles/placeholder/tasks"
    (hidden / "shadow.yml").write_text("- ansible.builtin.debug: {}\n")
    hidden.chmod(0o111)
    try:
        _refused_checkout(repo, revision, tmp_path, "is unreadable")
    finally:
        hidden.chmod(0o755)


def test_printed_paths_cannot_carry_terminal_escapes(tmp_path: Path) -> None:
    repo, revision = _synthetic_repo(tmp_path)
    (repo / "infra/ansible/evil\x1b[2Jname.yml").write_text("x\n")
    code, output, _ = _run(repo, ["rehearsal-probe", revision], _operator_env(tmp_path))
    assert code == 2
    assert "\x1b" not in output
    assert "evil\\x1b[2Jname.yml" in output


def test_missing_verified_file_is_refused(tmp_path: Path) -> None:
    repo, revision = _synthetic_repo(tmp_path)
    (repo / "infra/ansible/roles/placeholder/tasks/main.yml").unlink()
    _refused_checkout(repo, revision, tmp_path, "missing file")


def test_git_environment_cannot_redirect_the_checkout(tmp_path: Path) -> None:
    repo, revision = _synthetic_repo(tmp_path)
    decoy, decoy_revision = _synthetic_repo(tmp_path / "decoy")
    (repo / "infra/ansible/ansible.cfg").write_text("[defaults]\nlog_path=/tmp/x\n")
    env = _operator_env(
        tmp_path,
        GIT_DIR=str(decoy / ".git"),
        GIT_WORK_TREE=str(decoy),
        GIT_INDEX_FILE=str(decoy / ".git/index"),
    )
    code, output, recorder = _run(repo, ["rehearsal-probe", decoy_revision], env)
    assert code == 2
    assert recorder.invocations == []
    code, output, recorder = _run(repo, ["rehearsal-probe", revision], env)
    assert code == 2
    assert "differ" in output


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mutation", "needle"),
    [
        (lambda s: s.update(extra=1), "keys must be exactly"),
        (lambda s: s.update(operation="other"), "operation must equal"),
        (lambda s: s.update(playbook="../evil.yml"), "playbook path"),
        (lambda s: s.update(limit="all,ditto-coding-hosted-v2"), "limit"),
        (lambda s: s.update(limit="all"), None),
        (lambda s: s.update(confirmation="{{ x }}"), "confirmation"),
        (lambda s: s.update(enabled_var="ansible_facts"), "enabled_var"),
        (lambda s: s["nonsecret_env_vars"][0].update(env="PATH"), "DITTO_CODING_"),
        (lambda s: s["secret_env"][0].update(name="HOME"), "DITTO_CODING_"),
        (lambda s: s["secret_env"][0].update(charset="any"), "charset"),
        (lambda s: s["secret_env"][0].update(max_length=True), "max_length"),
        (
            lambda s: s.update(forbidden_env=["DITTO_CODING_REHEARSAL_SECRET"]),
            "also forbidden",
        ),
        (
            lambda s: s["nonsecret_env_vars"][0].update(
                var="coding_hosted_rehearsal_enabled"
            ),
            "extra var names must differ",
        ),
    ],
)
def test_malformed_specs_are_refused(tmp_path: Path, mutation, needle) -> None:
    spec = json.loads(json.dumps(SYNTHETIC_SPEC))
    mutation(spec)
    repo, revision = _synthetic_repo(tmp_path, spec)
    code, output, recorder = _run(
        repo, ["rehearsal-probe", revision], _operator_env(tmp_path)
    )
    if needle is None:
        # A single-host limit name is structurally valid; the play's own host
        # pin, not the guard, decides whether it is the reviewed host.
        assert code == 0
        return
    assert code == 2, output
    assert recorder.invocations == []
    assert needle in output


def test_duplicate_spec_keys_are_refused(tmp_path: Path) -> None:
    repo, _ = _synthetic_repo(tmp_path)
    path = repo / "infra/ansible/guarded-runs/rehearsal-probe.json"
    text = path.read_text().replace(
        '"limit": "ditto-coding-hosted-v2",',
        '"limit": "ditto-coding-hosted-v2", "limit": "all",',
    )
    path.write_text(text)
    _git(repo, "commit", "-q", "-am", "dup")
    revision = _git(repo, "rev-parse", "HEAD")
    code, output, _ = _run(repo, ["rehearsal-probe", revision], _operator_env(tmp_path))
    assert code == 2
    assert "duplicate key" in output


def test_unknown_operation_is_refused(tmp_path: Path) -> None:
    repo, revision = _synthetic_repo(tmp_path)
    code, output, _ = _run(repo, ["no-such-op", revision], _operator_env(tmp_path))
    assert code == 2
    assert "no spec guarded-runs/no-such-op.json" in output


# ---------------------------------------------------------------------------
# Invocation
# ---------------------------------------------------------------------------


def test_invocation_is_fixed_and_carries_no_secret(tmp_path: Path) -> None:
    repo, revision = _synthetic_repo(tmp_path)
    env = _operator_env(
        tmp_path,
        GCP_OSLOGIN_USER="operator",
        CLOUDSDK_CONFIG="/home/operator/.config/gcloud",
        LC_ALL="C.UTF-8",
        SSH_AUTH_SOCK="/run/agent",
        GIT_DIR="/tmp/elsewhere",
        EDITOR="evil",
        GCP_AUTH_KIND="serviceaccount",
        CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK="1",
        PAGER="evil",
        UV="/usr/bin/uv",
        VIRTUAL_ENV="/tmp/venv",
    )
    code, output, recorder = _run(repo, ["rehearsal-probe", revision], env)
    assert code == 0, output
    (invocation,) = recorder.invocations
    ansible = repo / "infra/ansible"
    extra_vars = {
        "coding_hosted_rehearsal_confirmation": "REHEARSE GUARDED RUN",
        "coding_hosted_rehearsal_enabled": True,
        "coding_hosted_rehearsal_host": "10.30.0.5",
        "coding_hosted_rehearsal_source_revision": revision,
    }
    assert invocation.argv == [
        "/synthetic/python",
        "-I",
        "-m",
        "ansible.cli.playbook",
        "-i",
        str(ansible / "inventory/gcp.yml"),
        "--limit",
        "ditto-coding-hosted-v2",
        "-e",
        json.dumps(extra_vars, sort_keys=True),
        str(ansible / "playbooks/gcp-coding-hosted-rehearsal.yml"),
    ]
    assert json.loads(invocation.argv[-2]) == extra_vars
    assert invocation.argv[-2].startswith('{"')
    assert invocation.cwd == str(ansible)
    assert CANARY not in json.dumps(invocation.argv)
    assert CANARY not in json.dumps(invocation.extra_vars)
    child = invocation.env
    secrets = {"DITTO_CODING_REHEARSAL_SECRET", "DITTO_CODING_REHEARSAL_TOKEN"}
    for name in secrets:
        assert child[name] == env[name]
    leaked = [n for n, v in child.items() if CANARY in v and n not in secrets]
    assert leaked == []
    assert MARKER not in env and child[MARKER] == "rehearsal-probe"
    for dropped in (
        "GIT_DIR",
        "EDITOR",
        "PAGER",
        "UV",
        "VIRTUAL_ENV",
        "DITTO_REHEARSAL_DROPPED",
        "DITTO_CODING_REHEARSAL_HOST",
        "GCP_AUTH_KIND",
        "CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK",
    ):
        assert dropped not in child
    for kept in ("GCP_OSLOGIN_USER", "CLOUDSDK_CONFIG", "LC_ALL", "SSH_AUTH_SOCK"):
        assert child[kept] == env[kept]
    assert child["ANSIBLE_CONFIG"] == str(ansible / "ansible.cfg")
    assert child["ANSIBLE_ROLES_PATH"] == str(ansible / "roles")
    assert child["ANSIBLE_KEEP_REMOTE_FILES"] == "False"
    assert child["ANSIBLE_DEBUG"] == "False"
    assert child["ANSIBLE_VERBOSITY"] == "0"
    assert "ANSIBLE_PIPELINING" not in child
    for name in guard.PLUGIN_PATH_ENV:
        assert child[name] == str(ansible / ".guarded-run-no-plugins")
    assert {n for n in child if n.startswith("ANSIBLE_")} == {
        *guard.PLUGIN_PATH_ENV,
        "ANSIBLE_CONFIG",
        "ANSIBLE_ROLES_PATH",
        "ANSIBLE_KEEP_REMOTE_FILES",
        "ANSIBLE_DEBUG",
        "ANSIBLE_VERBOSITY",
        "ANSIBLE_DISPLAY_ARGS_TO_STDOUT",
        "ANSIBLE_RETRY_FILES_ENABLED",
        "ANSIBLE_INVENTORY_UNPARSED_FAILED",
        "ANSIBLE_INVENTORY_ANY_UNPARSED_IS_FAILED",
        "ANSIBLE_HOST_PATTERN_MISMATCH",
    }
    assert child["ANSIBLE_HOST_PATTERN_MISMATCH"] == "error"
    assert child["ANSIBLE_INVENTORY_UNPARSED_FAILED"] == "True"
    assert "values not shown" in output


def test_top_level_errors_print_only_the_exception_class(monkeypatch, capsys) -> None:
    def explode(*_args, **_kwargs):
        raise ValueError(f"boom {CANARY}")

    monkeypatch.setattr(guard, "run", explode)
    assert guard.main() == 3
    captured = capsys.readouterr()
    assert "internal error (ValueError)" in captured.err
    assert CANARY not in captured.out + captured.err


def test_runtime_check_refuses_an_interpreter_outside_a_virtual_environment(
    monkeypatch,
) -> None:
    monkeypatch.setattr(guard.sys, "prefix", guard.sys.base_prefix)
    with pytest.raises(guard.Refusal):
        guard.verify_ansible_runtime(ROOT)


class _Distribution:
    def __init__(self, name: str, version: str) -> None:
        self.metadata = {"Name": name}
        self.version = version


def _locked_distributions() -> list[_Distribution]:
    import tomllib

    lock = tomllib.loads(LOCK.read_text())
    return [_Distribution(p["name"], p["version"]) for p in lock["package"]]


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (
            lambda d: d + [_Distribution("six", "1.17.0")],
            "six is not in the script lock",
        ),
        (
            lambda d: [
                _Distribution("Jinja2", "3.0.0")
                if x.metadata["Name"] == "jinja2"
                else x
                for x in d
            ],
            "jinja2 is not the locked version",
        ),
        (
            lambda d: d + [_Distribution("PyYAML", "1.0")],
            "pyyaml is installed twice",
        ),
    ],
)
def test_runtime_check_refuses_anything_but_the_locked_distributions(
    monkeypatch, mutate, needle
) -> None:
    import importlib.metadata

    monkeypatch.setattr(guard.sys, "prefix", "/synthetic/venv")
    monkeypatch.setattr(guard.sys, "base_prefix", "/synthetic/base")
    distributions = mutate(_locked_distributions())
    monkeypatch.setattr(importlib.metadata, "distributions", lambda: distributions)
    with pytest.raises(guard.Refusal) as refused:
        guard.verify_ansible_runtime(ROOT)
    assert any(needle in reason for reason in refused.value.reasons)


@pytest.mark.parametrize(
    ("name", "shadow"),
    [("PYTHONPATH", "hashlib.py"), ("OPENSSL_CONF", None), ("LD_PRELOAD", None)],
)
def test_code_loading_environment_is_refused_before_any_further_import(
    tmp_path: Path, name: str, shadow: str | None
) -> None:
    # Run the real script file: a shadowing hashlib on PYTHONPATH must never be
    # imported, because the refusal happens right after sys and os.
    evil = tmp_path / "evil"
    evil.mkdir()
    witness = tmp_path / "imported"
    if shadow:
        (evil / shadow).write_text(f"open({str(witness)!r}, 'w').close()\n")
    value = str(evil) if shadow else str(evil / "missing")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
        name: value,
    }
    completed = subprocess.run(
        [sys.executable, str(GUARD), "rehearsal-probe", REVISION_ZERO],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2, completed.stderr
    assert f"environment: {name} must be unset" in completed.stderr
    assert not witness.exists()


# ---------------------------------------------------------------------------
# Every real spec is consistent with its playbook and role
# ---------------------------------------------------------------------------

ENV_LOOKUP = re.compile(
    r"lookup\(\s*'(?:ansible\.builtin\.)?env'\s*,\s*'([A-Z][A-Z0-9_]*)'\s*\)"
)


def _real_specs() -> list[Path]:
    return sorted(SPECS.glob("*.json"))


def test_the_repository_ships_at_least_one_guarded_operation() -> None:
    assert _real_specs(), "no guarded-runs spec found"


@pytest.mark.parametrize("path", _real_specs(), ids=lambda p: p.stem)
def test_real_spec_matches_its_playbook_role_and_marker(path: Path) -> None:
    spec = guard.load_spec(ROOT / "infra/ansible", path.stem)
    playbook_path = ROOT / "infra/ansible" / spec.playbook
    plays = yaml.safe_load(playbook_path.read_text())
    assert spec.limit == "ditto-coding-hosted-v2"
    roles = [r for play in plays for r in play.get("roles", [])]
    assert roles, "a guarded playbook must run its role"
    role_texts = []
    for role in roles:
        role_dir = ROOT / "infra/ansible/roles" / role
        defaults = yaml.safe_load((role_dir / "defaults/main.yml").read_text())
        for var in (spec.enabled_var, spec.confirmation_var, spec.revision_var):
            assert var in defaults
        assert defaults[spec.enabled_var] is False
        for item in spec.nonsecret_env_vars:
            assert item.var in defaults
        role_texts.append(
            "\n".join(p.read_text() for p in sorted((role_dir / "tasks").glob("*.yml")))
        )
    text = "\n".join(role_texts)
    assert f"== '{spec.confirmation}'" in text
    marker = f"lookup('ansible.builtin.env', '{MARKER}') == '{spec.operation}'"
    assert marker in text, "the role must require the guard's marker"
    read = set(ENV_LOOKUP.findall(text)) - {MARKER}
    declared = {s.name for s in spec.secret_env}
    assert declared <= read, "every declared secret is read by the role"
    unexplained = {
        name
        for name in read - declared
        if name not in spec.forbidden_env
        and not name.startswith(spec.forbidden_env_prefixes)
    }
    assert unexplained == set(), "the role reads an env input the spec omits"


# ---------------------------------------------------------------------------
# Rehearsal: the real script under uv, ansible-core 2.21.2, a synthetic tree
# ---------------------------------------------------------------------------

rehearsal = pytest.mark.skipif(
    os.environ.get(REHEARSAL_GATE) != "1" or shutil.which("uv") is None,
    reason=f"set {REHEARSAL_GATE}=1 with uv on PATH to rehearse through ansible",
)


def _uv_run(repo: Path, home: Path, argv: list[str], env: dict[str, str]):
    uv = shutil.which("uv")
    assert uv is not None
    base = {
        k: v
        for k, v in os.environ.items()
        if k.startswith("UV_") or k in {"XDG_CACHE_HOME"}
    }
    base.setdefault("UV_CACHE_DIR", str(Path.home() / ".cache/uv"))
    return subprocess.run(
        [uv, "run", "--locked", "--script", str(repo / "infra/scripts" / GUARD.name)]
        + argv,
        cwd=home,
        env={**base, **env},
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )


@rehearsal
def test_rehearsal_runs_ansible_with_only_the_constructed_values(
    tmp_path: Path,
) -> None:
    repo, revision = _synthetic_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    evil = tmp_path / "evilbin"
    evil.mkdir()
    hijacked = tmp_path / "hijacked"
    for name in ("ansible-playbook", "ansible"):
        script = evil / name
        script.write_text(f"#!/bin/sh\ntouch {hijacked}\nexit 0\n")
        script.chmod(0o755)
    uv_dir = str(Path(shutil.which("uv") or "/").parent)
    env = _operator_env(home, PATH=f"{evil}:{uv_dir}:/usr/local/bin:/usr/bin:/bin")
    completed = _uv_run(repo, home, ["rehearsal-probe", revision], env)
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    assert not hijacked.exists(), "a PATH executable replaced the locked ansible"
    assert CANARY not in output
    outcome = json.loads((home / "outcome.json").read_text())
    assert outcome == {
        "enabled": True,
        "confirmation": "REHEARSE GUARDED RUN",
        "revision": revision,
        "host": "10.30.0.5",
        "marker": "rehearsal-probe",
        "secret_length": len(env["DITTO_CODING_REHEARSAL_SECRET"]),
        "dropped": "",
        "config": str(repo / "infra/ansible/ansible.cfg"),
        "keep_remote_files": False,
        "verbosity": 0,
        "play_hosts": ["ditto-coding-hosted-v2"],
    }
    assert CANARY not in json.dumps(outcome)


@rehearsal
def test_rehearsal_an_inventory_without_the_host_fails_the_run(tmp_path: Path) -> None:
    # ansible exits 0 when --limit matches nothing; the guard makes that fatal,
    # so a run whose inventory cannot see the reviewed host never looks done.
    repo, _ = _synthetic_repo(tmp_path)
    inventory = json.loads(json.dumps(REHEARSAL_INVENTORY))
    hosts = inventory["all"]["children"]["role_coding_hosted"]["hosts"]
    del hosts["ditto-coding-hosted-v2"]
    (repo / "infra/ansible/inventory/gcp.yml").write_text(yaml.safe_dump(inventory))
    _git(repo, "commit", "-q", "-am", "no reviewed host")
    revision = _git(repo, "rev-parse", "HEAD")
    home = tmp_path / "home"
    home.mkdir()
    uv_dir = str(Path(shutil.which("uv") or "/").parent)
    env = _operator_env(home, PATH=f"{uv_dir}:/usr/local/bin:/usr/bin:/bin")
    completed = _uv_run(repo, home, ["rehearsal-probe", revision], env)
    assert completed.returncode != 0, completed.stdout + completed.stderr
    assert not (home / "outcome.json").exists()


@rehearsal
def test_rehearsal_raising_template_input_never_reaches_ansible(
    tmp_path: Path,
) -> None:
    repo, revision = _synthetic_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    uv_dir = str(Path(shutil.which("uv") or "/").parent)
    path = f"{uv_dir}:/usr/local/bin:/usr/bin:/bin"
    # The residual the roles pin: an input that raises while rendering prints
    # its error text even under no_log. Through the guard it is refused as
    # template syntax before ansible starts, and nothing is echoed.
    template = (
        "{{ lookup('file', lookup('env', 'DITTO_CODING_REHEARSAL_TOKEN')) }}" + CANARY
    )
    for overrides in (
        {"DITTO_CODING_REHEARSAL_SECRET": template},
        {"DITTO_CODING_REHEARSAL_HOST": "{{ lookup('file', '/etc/hostname') }}"},
        {"ANSIBLE_CONFIG": str(home / "evil.cfg")},
    ):
        env = _operator_env(home, PATH=path, **overrides)
        completed = _uv_run(repo, home, ["rehearsal-probe", revision], env)
        output = completed.stdout + completed.stderr
        assert completed.returncode == 2, output
        assert "refused; nothing was run" in output
        assert CANARY not in output
        assert "PLAY" not in output
        assert not (home / "outcome.json").exists()
    env = _operator_env(home, PATH=path)
    uv = shutil.which("uv")
    assert uv is not None
    injected = subprocess.run(
        [uv, "run", "--locked", "--with", "six", "--script"]
        + [str(repo / "infra/scripts" / GUARD.name), "rehearsal-probe", revision],
        cwd=home,
        env={**{k: v for k, v in os.environ.items() if k.startswith("UV_")}, **env},
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert injected.returncode == 2, injected.stdout + injected.stderr
    assert "runtime: six is not in the script lock" in injected.stderr
    assert not (home / "outcome.json").exists()
    for extra in (["-e", "coding_hosted_rehearsal_enabled=true"], ["-vvv"]):
        completed = _uv_run(repo, home, ["rehearsal-probe", revision, *extra], env)
        assert completed.returncode == 2
        assert not (home / "outcome.json").exists()
